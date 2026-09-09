# -*- coding: utf-8 -*-
"""dsh-switch 核心层：配置管理 / SSH 巡检 / 隧道转发 / 一键部署引擎。

本模块不依赖任何 UI，方便独立测试（selftest）与复用。
UI 层（dsh_switch.pyw）只做表单与线程调度，所有服务器操作走这里。

配置来源：exe/脚本同目录的 dsh-switch.json，由程序表单自动生成，
         不需要用户手动编辑。**默认不含任何服务器信息。**
"""
import json
import os
import re
import select
import socket
import sys
import threading
import time

import paramiko

# ---------------------------------------------------------------- 配置 ----
# 空白默认值：不给任何预置服务器（开源分发要求，凭据只存在于用户本机 json）
DEFAULT_CONFIG = {
    "host": "",                # 服务器地址
    "ssh_port": 22,            # SSH 端口
    "user": "root",            # SSH 用户名
    "auth": "password",        # 认证方式: password | key
    "password": "",            # 密码（仅 remember_password=True 时落盘）
    "key_path": "",            # 私钥文件路径
    "key_passphrase": "",      # 私钥口令（可空）
    "remember_password": False,
    "container": "dsh",        # docker 容器名
    "port": 3080,              # 唯一端口：服务器 dsh 监听 + 本地隧道（保持一致）
    "version_channel": "stable",  # stable=npm latest | alpha=最新预览版
}


def config_path():
    """配置文件路径：exe/脚本同目录 dsh-switch.json。"""
    base = os.path.dirname(os.path.abspath(sys_arg0()))
    return os.path.join(base, "dsh-switch.json")


def sys_arg0():
    import sys
    return sys.argv[0]


def load_config():
    """读配置；缺的字段用默认值补齐；兼容旧版字段名（local_port/remote_port -> port）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            old = json.load(f)
        # 旧版本字段迁移
        if "port" not in old:
            old["port"] = old.get("local_port") or old.get("remote_port") or 3080
        cfg.update({k: v for k, v in old.items() if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """写配置。密码仅在 remember_password=True 时落盘，否则置空保存。"""
    out = dict(cfg)
    if not out.get("remember_password"):
        out["password"] = ""
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def resource_path(name):
    """定位随包资源：PyInstaller onefile 在 sys._MEIPASS，源码运行在文件所在目录。"""
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
    return os.path.join(base, name)


_log_fn = None  # 由 UI 注入的日志函数（线程安全由 UI 侧负责）


def set_log_fn(fn):
    """UI 启动时注入日志函数；不注入则退化为 print。"""
    global _log_fn
    _log_fn = fn


def log(msg):
    line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
    if _log_fn is not None:
        _log_fn(line)
    else:
        print(line, flush=True)


# ================================================================ SSH 连接 ----
class ServerCtl(object):
    """到服务器的 SSH 连接封装：命令执行（普通/流式）、容器巡检、部署探测。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self._lock = threading.Lock()

    # ---- 连接 ----
    def _connect(self):
        """建立 SSH 连接（按配置选择密码/私钥认证）。"""
        cfg = self.cfg
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(timeout=10, banner_timeout=10,
                      allow_agent=False, look_for_keys=False)
        if cfg.get("auth") == "key":
            kwargs["key_filename"] = cfg["key_path"]
            if cfg.get("key_passphrase"):
                kwargs["passphrase"] = cfg["key_passphrase"]
        else:
            kwargs["password"] = cfg.get("password")
        cli.connect(cfg["host"], int(cfg["ssh_port"]), cfg["user"], **kwargs)
        return cli

    def ensure_client(self):
        """惰性建立/重连。"""
        if self._client is not None and self._client.get_transport() is not None \
                and self._client.get_transport().is_active():
            return self._client
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = self._connect()
        return self._client

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ---- 命令执行 ----
    def run(self, cmd, timeout=20):
        """执行远程命令，返回 stdout 文本；失败返回 None（不抛异常，调用方判断）。"""
        with self._lock:
            try:
                _, out, _ = self.ensure_client().exec_command(cmd, timeout=timeout)
                return out.read().decode("utf-8", "replace").strip()
            except Exception as e:
                self.last_error = str(e)
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                return None

    def run_stream(self, cmd, on_line, deadline_s=1200):
        """流式执行长命令（构建过程），每行输出回调 on_line(text)。

        返回 (exit_code, 全部输出文本)。超时返回 (None, 已收输出)。
        """
        with self._lock:
            try:
                self.ensure_client()
                lines = []
                start = time.time()
                _, out, _ = self._client.exec_command(cmd, timeout=None)
                out.channel.settimeout(deadline_s)
                while True:
                    line = out.readline()
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    lines.append(line)
                    try:
                        on_line(line)
                    except Exception:
                        pass
                    if time.time() - start > deadline_s:
                        return None, "\n".join(lines)
                code = out.channel.recv_exit_status()
                return code, "\n".join(lines)
            except Exception as e:
                self.last_error = str(e)
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                return None, ""

    def sftp_write(self, remote_path, content):
        """以文本方式上传一个文件（部署模板用）。"""
        with self._lock:
            sftp = self.ensure_client().open_sftp()
            try:
                with sftp.open(remote_path, "w") as f:
                    f.write(content)
            finally:
                sftp.close()

    def sftp_chmod(self, remote_path, mode):
        """设置远程文件权限（SFTP 通道）。"""
        with self._lock:
            sftp = self.ensure_client().open_sftp()
            try:
                sftp.chmod(remote_path, mode)
            finally:
                sftp.close()

    # ---- 容器巡检与操作 ----
    def container_status(self):
        """巡检容器：返回 dict(online: True/False/None, text, version)。"""
        name = self.cfg["container"]
        out = self.run("docker ps -a --filter name=^/{0}$ --format '{{{{.Status}}}}'".format(name))
        if out is None:
            return {"online": None, "text": "SSH 连接失败", "version": ""}
        if not out:
            return {"online": False, "text": "未部署", "version": ""}
        online = out.startswith("Up")
        version = ""
        if online:
            version = self.run(
                "docker exec {0} dsh --version 2>/dev/null".format(name)) or ""
        return {"online": online, "text": out, "version": version}

    def fetch_token(self):
        """从 docker logs 抓最新 token（容器重建后轮换，须现取）。"""
        out = self.run(
            "docker logs {0} 2>&1 | grep -o 'token=[A-Za-z0-9_-]*' | tail -1".format(self.cfg["container"]),
            timeout=30)
        if out and out.startswith("token="):
            return out[len("token="):]
        return None

    def container_action(self, action):
        """start / stop 容器。返回 (ok, 输出)。"""
        cmd = "docker {0} {1} 2>&1".format(action, self.cfg["container"])
        out = self.run(cmd, timeout=60)
        return (out is not None), (out or "")

    def has_container(self):
        """容器是否存在（任意状态，含 Exited）——用于「已部署」判定，
        与 is_deployed（要求 healthy）区分：停止的容器也必须能被启动。"""
        out = self.run(
            "docker ps -a --filter name=^/{0}$ --format '{{{{.Names}}}}'".format(self.cfg["container"]))
        return bool(out)

    def is_deployed(self):
        """启动时的自动探测：容器存在且 healthy 视为已部署。"""
        st = self.container_status()
        return bool(st["online"]) and "healthy" in st["text"]


# ================================================================ 隧道 ----
class Tunnel(object):
    """paramiko 端口转发器：本机 port <-> 服务器 127.0.0.1:port（同一端口）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.transport = None
        self.server_sock = None
        self.running = False

    @property
    def is_up(self):
        t = self.transport
        return self.running and t is not None and t.is_active()

    def start(self):
        """启动监听+转发。返回 None=成功，字符串=失败原因。"""
        if self.is_up:
            return None
        self.stop()
        port = int(self.cfg["port"])
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(16)
        except Exception as e:
            return "本机 {} 端口占用: {}".format(port, e)
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(timeout=10, banner_timeout=10,
                      allow_agent=False, look_for_keys=False)
            if self.cfg.get("auth") == "key":
                kw["key_filename"] = self.cfg["key_path"]
                if self.cfg.get("key_passphrase"):
                    kw["passphrase"] = self.cfg["key_passphrase"]
            else:
                kw["password"] = self.cfg.get("password")
            cli.connect(self.cfg["host"], int(self.cfg["ssh_port"]),
                        self.cfg["user"], **kw)
            trans = cli.get_transport()
            trans.set_keepalive(30)
        except Exception as e:
            srv.close()
            return "SSH 连接失败: {}".format(e)
        self.server_sock = srv
        self.transport = trans
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return None

    def _accept_loop(self):
        """每条本地连接开一条 direct-tcpip 通道并双向搬运。"""
        while self.running:
            try:
                local_sock, addr = self.server_sock.accept()
            except Exception:
                break
            try:
                chan = self.transport.open_channel(
                    "direct-tcpip", ("127.0.0.1", int(self.cfg["port"])), addr)
            except Exception as e:
                log("隧道通道建立失败: {}".format(e))
                try:
                    local_sock.close()
                except Exception:
                    pass
                continue
            local_sock.setblocking(True)
            chan.setblocking(True)
            threading.Thread(target=self._pump, args=(local_sock, chan),
                             daemon=True).start()

    @staticmethod
    def _pump(local_sock, chan):
        socks = [local_sock, chan]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 30)
                if not r:
                    continue
                if local_sock in r:
                    data = local_sock.recv(65536)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(65536)
                    if not data:
                        break
                    local_sock.sendall(data)
        except Exception:
            pass
        finally:
            for c in (chan, local_sock):
                try:
                    c.close()
                except Exception:
                    pass

    def stop(self):
        self.running = False
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None


# ================================================================ 部署引擎 ----
NPM_MIRROR = "https://registry.npmmirror.com"
NPM_OFFICIAL = "https://registry.npmjs.org"
BUILD_TIMEOUT = 1500        # 构建总超时（秒），arm64 实测约 6 分钟
HEALTH_TIMEOUT = 600        # 健康轮询超时（秒）


def _render(template, mapping):
    """简单的 __TOKEN__ 替换渲染（避免 format 与 YAML/Shell 花括号冲突）。"""
    text = template
    for k, v in mapping.items():
        text = text.replace("__{}__".format(k), str(v))
    return text


class Deployer(object):
    """一键部署引擎：探测 -> 生成 -> 上传 -> 构建 -> 健康确认。

    deploy() 阻塞执行（放进后台线程调用），通过回调向前端报进度：
      on_step("3/6 生成部署文件")     —— 步骤级
      on_line("docker 构建输出行")    —— 行级（构建日志实时回显）
    返回 (ok: bool, message: str)。
    """

    def __init__(self, cfg, ctl):
        self.cfg = cfg
        self.ctl = ctl

    # ---- 远程资源路径 ----
    def _deploy_dir(self):
        """服务器部署目录：$HOME/dsh-switch（首次探测后缓存在实例上）。"""
        if not hasattr(self, "_dir"):
            home = self.ctl.run("echo $HOME", timeout=10)
            if not home:
                raise RuntimeError("无法获取服务器 HOME 目录")
            self._dir = home + "/dsh-switch"
        return self._dir

    # ---- 版本解析 ----
    def resolve_version(self):
        """按版本通道解析要安装的 dsh 版本号。返回 (版本字符串, 错误或None)。"""
        if self.cfg.get("version_channel") == "alpha":
            # alpha 版不在 latest 标签里，必须从 registry dist-tags 现解析
            for reg in (NPM_OFFICIAL, NPM_MIRROR):
                out = self.ctl.run(
                    "curl -s -m 15 '{0}/@deepseek-ai%2Fdsh' | "
                    "grep -o '\"alpha\":\"[^\"]*\"' | head -1".format(reg), timeout=25)
                if out:
                    m = re.search(r'"alpha":"([^"]+)"', out)
                    if m:
                        return m.group(1), None
            return None, "无法从 npm registry 解析最新 alpha 版本号（检查服务器外网）"
        return "latest", None

    # ---- 环境探测 ----
    def probe(self):
        """部署前环境探测。返回 (问题列表, 信息列表)。问题非空则不继续部署。"""
        problems, infos = [], []
        arch = self.ctl.run("uname -m")
        if arch is None:
            return ["SSH 连接失败"], []
        infos.append("服务器架构: {}".format(arch))
        if arch not in ("x86_64", "aarch64", "arm64"):
            problems.append("不支持的架构: {}（需要 x86_64 或 arm64）".format(arch))

        if self.ctl.run("docker --version") is None:
            problems.append("服务器未安装 Docker（请先安装：https://docs.docker.com/engine/install/）")
        else:
            infos.append("Docker: {}".format(self.ctl.run("docker --version").split("\n")[0]))

        # compose 命令探测：优先 docker compose v2，回退 docker-compose v1
        compose = "docker compose"
        if self.ctl.run("docker compose version") is None:
            if self.ctl.run("docker-compose version") is not None:
                compose = "docker-compose"
            else:
                problems.append("未找到 Docker Compose（需要 v2 或 v1）")
        self.compose_cmd = compose
        infos.append("Compose 命令: {}".format(compose))

        # docker 权限（非 root 用户需在 docker 组）
        if self.ctl.run("docker ps") is None:
            problems.append("当前用户无 Docker 权限（非 root 用户请加入 docker 组: "
                            "sudo usermod -aG docker {}）".format(self.cfg["user"]))

        # 端口占用
        port = int(self.cfg["port"])
        occupied = self.ctl.run(
            "ss -ltn 2>/dev/null | grep -q ':{0} ' && echo yes || echo no".format(port))
        if occupied == "yes":
            problems.append("服务器端口 {} 已被占用".format(port))
        else:
            infos.append("端口 {} 空闲".format(port))

        # 同名容器
        name = self.cfg["container"]
        exist = self.ctl.run(
            "docker ps -a --filter name=^/{0}$ --format '{{{{.Names}}}}'".format(name))
        if exist:
            problems.append("已存在同名容器「{}」（如需覆盖请改容器名或先手动删除）".format(name))
        return problems, infos

    # ---- 模板渲染 ----
    def _load_template(self, name):
        """优先从打包资源里找模板；源码运行时回退到上级目录 templates/。"""
        for base in (os.path.join("templates", name),
                     os.path.join("..", "templates", name)):
            p = resource_path(base)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
        raise RuntimeError("找不到模板文件: " + name)

    def _render_all(self, version, registry):
        """渲染三个部署文件。"""
        deploy_dir = self._deploy_dir()
        common = {"DSH_VERSION": version, "NPM_REGISTRY": registry,
                  "CONTAINER": self.cfg["container"], "PORT": int(self.cfg["port"]),
                  "DEPLOY_DIR": deploy_dir}
        return {
            "Dockerfile": _render(self._load_template("Dockerfile.tpl"), common),
            "entrypoint.sh": _render(self._load_template("entrypoint.sh.tpl"), common),
            "docker-compose.yml": _render(self._load_template("docker-compose.yml.tpl"), common),
        }

    # ---- 部署主流程 ----
    def deploy(self, on_step, on_line):
        """执行完整部署。返回 (ok, message)。"""
        try:
            # 1/6 版本解析
            on_step("1/6 解析 dsh 版本")
            version, err = self.resolve_version()
            if err:
                return False, err
            on_line("目标版本: {}".format(version))

            # 2/6 环境探测
            on_step("2/6 环境探测")
            problems, infos = self.probe()
            for i in infos:
                on_line("[探测] " + i)
            if problems:
                for p in problems:
                    on_line("[问题] " + p)
                return False, "环境探测未通过（见日志）"

            # 3/6 建目录
            on_step("3/6 创建服务器目录")
            d = self._deploy_dir()
            # 数据目录必须可被容器内 node 用户(uid 1000)写入：
            # root 部署时 chown 给 1000；非 root 用户创建的目录本身归自己，
            # 若 uid 恰为 1000 则同样可用，否则给出提示。
            out = self.ctl.run(
                "mkdir -p '{0}' '{1}/data/home' '{1}/data/workspace' && "
                "(id -u | grep -q '^0$' && chown -R 1000:1000 '{1}/data' || true) && "
                "stat -c '%U:%G %a' '{1}/data/home'".format(d, d))
            if out is None:
                return False, "创建目录失败: " + getattr(self.ctl, "last_error", "")
            on_line("部署目录: {}".format(d))
            on_line("数据目录权限: {}（容器内用户 uid=1000）".format(out))

            # 4/6 生成并上传（默认国内镜像源；notarget 类错误自动切官方源重试）
            registry = NPM_MIRROR
            files = self._render_all(version, registry)
            for attempt in ("npmmirror", "npmjs"):
                on_step("4/6 上传部署文件（{}）".format(attempt))
                try:
                    for fn, content in files.items():
                        self.ctl.sftp_write("{}/{}".format(d, fn), content)
                    # entrypoint 需要执行位：在 SFTP 侧直接 chmod，
                    # COPY 会保留源文件权限（部分服务器的 overlayfs
                    # 在构建内 chmod 会报 EPERM，不能依赖 Dockerfile 里的 chmod）
                    self.ctl.sftp_chmod("{}/entrypoint.sh".format(d), 0o755)
                except Exception as e:
                    return False, "上传失败: {}".format(e)

                # 5/6 构建 + 启动（流式日志）
                on_step("5/6 构建镜像并启动（首次约 3~8 分钟）")
                code, _ = self.ctl.run_stream(
                    "cd '{0}' && {1} up -d --build 2>&1".format(d, self.compose_cmd),
                    on_line, deadline_s=BUILD_TIMEOUT)
                if code == 0:
                    break
                # npm 源同步滞后（如 alpha 子包）→ 切官方源重试一次
                if registry == NPM_MIRROR:
                    on_line("[!] 国内镜像源构建失败，自动切换 npm 官方源重试...")
                    registry = NPM_OFFICIAL
                    files = self._render_all(version, registry)
                else:
                    return False, "构建失败（完整日志见上方，可重试）"

            # 6/6 健康确认
            on_step("6/6 等待容器健康（最长 {} 秒）".format(HEALTH_TIMEOUT))
            name = self.cfg["container"]
            start = time.time()
            while time.time() - start < HEALTH_TIMEOUT:
                st = self.ctl.container_status()
                if st["online"] and "healthy" in st["text"]:
                    on_line("容器状态: {}（{}）".format(st["text"], st["version"]))
                    return True, "部署成功：{}（{}）".format(name, st["version"])
                time.sleep(5)
                on_line("等待健康... 当前: {}".format(st["text"]))
            return False, "健康检查超时（容器可能仍在构建内初始化，稍后用「刷新状态」查看）"
        except Exception as e:
            return False, "部署异常: {}".format(e)

# -*- coding: utf-8 -*-
"""dsh-switch —— 一键把 DeepSeek Harness 部署到你的服务器，本地 SSH 隧道即开即用。

页面结构（Notebook 双页签 + 底部共享日志框）：
  「部署」页：服务器配置表单（地址/SSH端口/用户/密码或密钥/容器名/端口/版本通道）
             + [保存配置][测试连接][开始部署]；部署成功后按钮隐藏。
  「控制台」页：容器状态灯、隧道状态灯、启动/停止容器、隧道开关、打开 DSH、刷新。
  底部：日志框（巡检/隧道/部署全过程输出）。

行为约定：
  * 配置由表单自动写入同目录 dsh-switch.json（密码仅勾选「记住密码」才落盘）。
  * 程序启动时自动探测：已有 healthy 容器则直接进入「已部署」状态。
  * 点窗口 X 收进系统托盘；托盘右键「退出」才真正结束程序（并断开隧道）。
  * Windows 命名互斥体保证单实例。

打包：
  venv38/Scripts/pyinstaller.exe --onefile --noconsole --icon assets/dsh.ico ^
      --add-data "assets/dsh-icon-512.png;assets" ^
      --add-data "templates;templates" ^
      --name dsh-switch src/dsh_switch.pyw

开源说明：凭据只保存在用户本机配置文件，程序不做任何网络上报；
        图标素材取自 deepseek-ai/deepseek-harness（MIT）官方仓库。
"""
import os
import socket
import sys
import threading
import time
import webbrowser

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import dshcore as core  # 核心层：配置/SSH/隧道/部署引擎

try:
    import pystray
    from PIL import Image
except ImportError:  # 无 pystray 时退化为无托盘模式，程序仍可用
    pystray = None
    Image = None

APP = None            # 主窗口引用（selftest 模式下为 None）
_MUTEX = None         # 单实例互斥体句柄（须全程持有防 GC）


def bridge_log(line):
    """core 层日志桥接到 UI 日志框。"""
    if APP is not None:
        APP.log_line(line)
    else:
        print(line, flush=True)


core.set_log_fn(bridge_log)


# ============================================================ 单实例 ----
def acquire_single_instance():
    """Windows 命名互斥体：已有实例在跑则返回 False（错误码 183）。"""
    global _MUTEX
    _MUTEX = __import__("ctypes").windll.kernel32.CreateMutexW(
        None, False, "dsh-switch-mutex")
    return __import__("ctypes").windll.kernel32.GetLastError() != 183


# ============================================================ 主窗口 ----
class App(tk.Tk):

    POLL_MS = 10 * 1000  # 容器巡检周期（毫秒）

    def __init__(self):
        tk.Tk.__init__(self)
        self.cfg = core.load_config()
        # 配置被机器特征码判定为「来自其他机器」而重置时，明确告知用户
        if core.last_config_reset:
            messagebox.showwarning("dsh-switch", core.last_config_reset, parent=self)
        self.ctl = core.ServerCtl(self.cfg)
        self.tunnel = core.Tunnel(self.cfg)
        self.deployer = core.Deployer(self.cfg, self.ctl)
        self.deploy_state = "probing"   # probing | undeployed | deploying | deployed | failed
        self.deiconify_needed = False

        self.title("dsh-switch - DeepSeek Harness 一键部署与隧道")
        self.resizable(False, False)

        # ---- 图标（托盘 + 标题栏）----
        self._icon_image = None
        try:
            self._icon_image = Image.open(core.resource_path(
                os.path.join("assets", "dsh-icon-512.png")))
            self._tk_icon = tk.PhotoImage(file=core.resource_path(
                os.path.join("assets", "dsh-icon-512.png")))
            self.iconphoto(True, self._tk_icon)
        except Exception:
            pass

        # ---- 布局：上页签 + 下日志 ----
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_deploy = ttk.Frame(nb, padding=10)
        self.tab_console = ttk.Frame(nb, padding=10)
        nb.add(self.tab_deploy, text=" 部署 ")
        nb.add(self.tab_console, text=" 控制台 ")
        self._build_deploy_tab()
        self._build_console_tab()

        self.txt_log = scrolledtext.ScrolledText(
            self, width=88, height=9, state="disabled", font=("Consolas", 9))
        self.txt_log.pack(fill="both", padx=8, pady=(0, 8))

        # ---- 托盘 / 关闭行为 ----
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._tray = None
        self._init_tray()

        # ---- 启动节奏 ----
        self.after(500, self._startup_probe)     # 探测已部署状态
        self.after(self.POLL_MS, self._poll_loop)

    # ================================================== 部署页 ----
    def _build_deploy_tab(self):
        f = self.tab_deploy
        g = ttk.Frame(f)
        g.pack(fill="x")
        W = 30  # 输入框宽度

        def row(i, label, widget, extra=None):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w", pady=2)
            widget.grid(row=i, column=1, sticky="w", pady=2)
            if extra is not None:
                extra.grid(row=i, column=2, sticky="w", padx=(6, 0))

        self.var_host = tk.StringVar(value=self.cfg["host"])
        self.var_ssh_port = tk.StringVar(value=str(self.cfg["ssh_port"]))
        self.var_user = tk.StringVar(value=self.cfg["user"])
        self.var_auth = tk.StringVar(value=self.cfg.get("auth", "password"))
        self.var_password = tk.StringVar(value=self.cfg.get("password", ""))
        self.var_key_path = tk.StringVar(value=self.cfg.get("key_path", ""))
        self.var_key_pass = tk.StringVar(value=self.cfg.get("key_passphrase", ""))
        self.var_remember = tk.BooleanVar(value=self.cfg.get("remember_password", False))
        self.var_container = tk.StringVar(value=self.cfg.get("container", "dsh"))
        self.var_port = tk.StringVar(value=str(self.cfg.get("port", 3080)))
        self.var_channel = tk.StringVar(
            value="稳定版 (latest)" if self.cfg.get("version_channel") == "stable"
            else "预览版 (alpha)")

        row(0, "服务器地址", ttk.Entry(g, textvariable=self.var_host, width=W))
        row(1, "SSH 端口", ttk.Entry(g, textvariable=self.var_ssh_port, width=8))
        row(2, "SSH 用户名", ttk.Entry(g, textvariable=self.var_user, width=W))

        # 认证方式单选
        af = ttk.Frame(g)
        ttk.Radiobutton(af, text="密码", variable=self.var_auth, value="password",
                        command=self._refresh_auth_rows).pack(side="left")
        ttk.Radiobutton(af, text="私钥", variable=self.var_auth, value="key",
                        command=self._refresh_auth_rows).pack(side="left", padx=(10, 0))
        row(3, "认证方式", af)

        # 密码行 / 私钥行（按认证方式显隐）
        self.row_pw = ttk.Frame(g)
        ttk.Label(self.row_pw, text="密码").pack(side="left")
        e = ttk.Entry(self.row_pw, textvariable=self.var_password, width=W - 8,
                      show="*")
        e.pack(side="left", padx=(6, 12))
        ttk.Checkbutton(self.row_pw, text="记住密码（明文存本机 json）",
                        variable=self.var_remember).pack(side="left")
        g.grid_rowconfigure(4, weight=1)
        self.row_pw.grid(row=4, column=1, columnspan=2, sticky="w", pady=2)

        self.row_key = ttk.Frame(g)
        ttk.Label(self.row_key, text="私钥文件").pack(side="left")
        ttk.Entry(self.row_key, textvariable=self.var_key_path, width=W - 14).pack(
            side="left", padx=(6, 4))
        ttk.Button(self.row_key, text="浏览...", command=self._pick_key,
                   width=7).pack(side="left")
        ttk.Label(self.row_key, text="口令").pack(side="left", padx=(10, 0))
        ttk.Entry(self.row_key, textvariable=self.var_key_pass, width=12,
                  show="*").pack(side="left", padx=(6, 0))
        self.row_key.grid(row=5, column=1, columnspan=2, sticky="w", pady=2)

        row(6, "容器名", ttk.Entry(g, textvariable=self.var_container, width=14))
        row(7, "端口（服务器与隧道共用）",
            ttk.Entry(g, textvariable=self.var_port, width=8))
        row(8, "dsh 版本", ttk.Combobox(
            g, textvariable=self.var_channel, width=W - 4, state="readonly",
            values=["稳定版 (latest)", "预览版 (alpha)"]))

        # 按钮行
        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(12, 0))
        self.btn_save = ttk.Button(bf, text="保存配置", command=self.save_config,
                                   width=12)
        self.btn_save.pack(side="left", padx=(0, 8))
        self.btn_test = ttk.Button(bf, text="测试连接", command=self.test_connection,
                                   width=12)
        self.btn_test.pack(side="left", padx=8)
        self.btn_deploy = ttk.Button(bf, text="开始部署", command=self.start_deploy,
                                     width=14)
        self.btn_deploy.pack(side="left", padx=8)
        self.lbl_deploy = ttk.Label(f, text="", foreground="#666666")
        self.lbl_deploy.pack(fill="x", pady=(8, 0))

        self._refresh_auth_rows()

    def _pick_key(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="选择 SSH 私钥文件",
                                       initialdir=os.path.expanduser("~/.ssh"))
        if p:
            self.var_key_path.set(p)

    def _refresh_auth_rows(self):
        (self.row_pw if self.var_auth.get() == "password" else self.row_key)\
            .grid()
        (self.row_key if self.var_auth.get() == "password" else self.row_pw)\
            .grid_remove()

    # ---- 表单 <-> 配置 ----
    def collect_form(self):
        """把表单值写回 self.cfg；返回 None=通过，字符串=校验错误。"""
        host = self.var_host.get().strip()
        if not host:
            return "请填写服务器地址"
        try:
            ssh_port = int(self.var_ssh_port.get().strip())
            port = int(self.var_port.get().strip())
        except ValueError:
            return "端口必须是数字"
        if not (1 <= ssh_port <= 65535 and 1 <= port <= 65535):
            return "端口超出范围 (1-65535)"
        container = self.var_container.get().strip()
        if not re_fullmatch(container, r"[A-Za-z0-9][A-Za-z0-9_.-]*"):
            return "容器名只能包含字母、数字、点、横线、下划线"
        self.cfg.update({
            "host": host, "ssh_port": ssh_port, "user": self.var_user.get().strip(),
            "auth": self.var_auth.get(),
            "password": self.var_password.get(),
            "key_path": self.var_key_path.get().strip(),
            "key_passphrase": self.var_key_pass.get(),
            "remember_password": self.var_remember.get(),
            "container": container, "port": port,
            "version_channel": "alpha" if "alpha" in self.var_channel.get()
            else "stable",
        })
        # 热更新核心对象使用的配置
        self.ctl.cfg = self.cfg
        self.tunnel.cfg = self.cfg
        self.deployer.cfg = self.cfg
        return None

    def save_config(self):
        err = self.collect_form()
        if err:
            messagebox.showwarning("dsh-switch", err, parent=self)
            return
        try:
            core.save_config(self.cfg)
            core.log("配置已保存到 " + core.config_path())
        except Exception as e:
            messagebox.showerror("dsh-switch", "保存失败: {}".format(e), parent=self)

    def test_connection(self):
        """快速验证：SSH 能连上 + 服务器架构。"""
        err = self.collect_form()
        if err:
            messagebox.showwarning("dsh-switch", err, parent=self)
            return
        self.btn_test.configure(state="disabled", text="连接中...")
        def worker():
            arch = self.ctl.run("uname -m", timeout=15)
            if arch is None:
                msg = "连接失败：{}（检查地址/端口/用户/认证方式）".format(
                    getattr(self.ctl, "last_error", ""))
                self.after(0, lambda: (self.lbl_deploy.configure(text=msg, foreground="#c22"),
                                       self.btn_test.configure(state="normal", text="测试连接")))
            else:
                docker = self.ctl.run("docker --version", timeout=15)
                msg = "连接成功！架构 {}；Docker: {}".format(
                    arch, (docker or "未安装").split("\n")[0])
                color = "#22a94c" if docker else "#e0a02a"
                self.after(0, lambda: (self.lbl_deploy.configure(text=msg, foreground=color),
                                       self.btn_test.configure(state="normal", text="测试连接")))
        threading.Thread(target=worker, daemon=True).start()

    # ================================================== 部署 ----
    def start_deploy(self):
        if self.deploy_state == "deploying":
            return
        # 覆盖部署需二次确认（数据卷保留，但容器会重建）
        if self.deploy_state == "deployed":
            if not messagebox.askyesno(
                    "dsh-switch", "服务器上已有部署。重新部署将重建容器（会话/配置数据卷保留），继续？",
                    parent=self):
                return
        err = self.collect_form()
        if err:
            messagebox.showwarning("dsh-switch", err, parent=self)
            return
        # 部署前自动保存配置，避免断电丢配置
        try:
            core.save_config(self.cfg)
        except Exception:
            pass

        self._set_deploy_state("deploying")
        core.log("=" * 46)
        core.log("开始部署 dsh（版本通道: {}）".format(self.cfg["version_channel"]))

        def cb_step(text):
            self.after(0, lambda: self.lbl_deploy.configure(text=text))
            core.log(text)

        def cb_line(text):
            core.log("  " + text)

        def worker():
            ok, msg = self.deployer.deploy(cb_step, cb_line)
            core.log(("✅ " if ok else "❌ ") + msg)
            self.after(0, self._deploy_done, ok, msg)
        threading.Thread(target=worker, daemon=True).start()

    def _deploy_done(self, ok, msg):
        if ok:
            self._set_deploy_state("deployed")
        else:
            self._set_deploy_state("failed")
            messagebox.showerror("dsh-switch", "部署失败：{}\n\n可点击「重试部署」再次尝试。".format(msg),
                                 parent=self)

    def _set_deploy_state(self, state):
        """部署按钮状态机：
           undeployed=可部署 / deploying=禁用 / deployed=隐藏按钮 /
           failed=按钮变「重试部署」。"""
        self.deploy_state = state
        if state == "undeployed":
            self.btn_deploy.configure(text="开始部署", state="normal")
            self.btn_deploy.pack(side="left", padx=8, after=self.btn_test)
            self.lbl_deploy.configure(text="未检测到部署，填写配置后点击「开始部署」",
                                      foreground="#666666")
        elif state == "deploying":
            self.btn_deploy.configure(text="部署中...", state="disabled")
        elif state == "deployed":
            self.btn_deploy.pack_forget()          # 成功后隐藏部署按钮
            self.lbl_deploy.configure(text="", foreground="#666666")
        elif state == "failed":
            self.btn_deploy.configure(text="重试部署", state="normal")
            self.btn_deploy.pack(side="left", padx=8, after=self.btn_test)
        # 容器启停按钮只在已部署态可用（文案由巡检按实际状态刷新）
        deployed = state == "deployed"
        self.btn_container.configure(state="normal" if deployed else "disabled")

    def _startup_probe(self):
        """启动时自动探测已部署状态（有配置才探测）。"""
        if not self.cfg.get("host"):
            self._set_deploy_state("undeployed")
            return
        self._set_deploy_state("deploying")
        self.btn_deploy.configure(text="探测中...")
        def worker():
            deployed = False
            try:
                # 只要有容器（含已停止）就算已部署，否则停止的容器会进不去控制台按钮
                deployed = self.ctl.has_container()
            except Exception:
                pass
            self.after(0, lambda: self._set_deploy_state(
                "deployed" if deployed else "undeployed"))
            self.after(0, self.poll_now)
        threading.Thread(target=worker, daemon=True).start()

    # ================================================== 控制台页 ----
    def _build_console_tab(self):
        f = self.tab_console
        g = ttk.Frame(f)
        g.pack(fill="x")

        ttk.Label(g, text="容器:").grid(row=0, column=0, sticky="w")
        self.dot_container = tk.Canvas(g, width=14, height=14, highlightthickness=0)
        self.dot_container.grid(row=0, column=1, sticky="w")
        self.lbl_container = ttk.Label(g, text="检查中...", width=48)
        self.lbl_container.grid(row=0, column=2, sticky="w")

        ttk.Label(g, text="隧道:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.dot_tunnel = tk.Canvas(g, width=14, height=14, highlightthickness=0)
        self.dot_tunnel.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.lbl_tunnel = ttk.Label(g, text="已停止", width=48)
        self.lbl_tunnel.grid(row=1, column=2, sticky="w", pady=(6, 0))

        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(12, 0))
        # 启停合一：按钮文案随容器状态在「启动容器/停止容器」间切换
        self.btn_container = ttk.Button(bf, text="启动容器",
                                        command=self.toggle_container,
                                        width=11, state="disabled")
        self.btn_container.pack(side="left", padx=4)
        self.btn_tunnel = ttk.Button(bf, text="启动隧道", command=self.toggle_tunnel,
                                     width=11)
        self.btn_tunnel.pack(side="left", padx=4)
        self.btn_open = ttk.Button(bf, text="打开 DSH", command=self.open_dsh,
                                   width=11)
        self.btn_open.pack(side="left", padx=4)
        self.btn_refresh = ttk.Button(bf, text="刷新状态", command=self.poll_now,
                                      width=11)
        self.btn_refresh.pack(side="left", padx=4)

        self._draw_dot(self.dot_container, "gray")
        self._draw_dot(self.dot_tunnel, "gray")
        # 容器状态决定隧道/打开按钮可用性（首次巡检前不可用）
        self._container_online = False
        self._container_healthy = False
        self.btn_tunnel.configure(state="disabled")
        self.btn_open.configure(state="disabled")

    def toggle_container(self):
        """启停合一按钮：按巡检到的容器状态决定动作。
        停止容器时联动关闭隧道；动作期间按钮禁用防连点。"""
        if self.deploy_state != "deployed":
            return
        action = "stop" if self._container_online else "start"
        self.btn_container.configure(
            text="{}中...".format("停止" if action == "stop" else "启动"),
            state="disabled")
        core.log("{}容器 {}...".format("启动" if action == "start" else "停止",
                                       self.cfg["container"]))
        def worker():
            ok, out = self.ctl.container_action(action)
            core.log("{}：{}".format("完成" if ok else "失败", out or "(无输出)"))
            if action == "stop" and ok and self.tunnel.is_up:
                self.tunnel.stop()
                core.log("隧道已随容器停止而关闭")
                self.after(0, self._refresh_tunnel_dot)
            # 先恢复按钮可用（文案由随后的巡检按新状态刷新），再触发巡检
            self.after(0, lambda: self.btn_container.configure(state="normal"))
            self.after(0, self.poll_now)
        threading.Thread(target=worker, daemon=True).start()

    # ================================================== 通用小件 ----
    @staticmethod
    def _draw_dot(canvas, color):
        canvas.delete("all")
        canvas.create_oval(2, 2, 12, 12, fill=color, outline="")

    def log_line(self, line):
        self.after(0, self._append_log, line)

    def _append_log(self, line):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ================================================== 巡检 ----
    def _poll_loop(self):
        self.poll_now()
        self.after(self.POLL_MS, self._poll_loop)

    def poll_now(self):
        if not self.cfg.get("host"):
            self.after(0, lambda: (self._draw_dot(self.dot_container, "#c2c2c2"),
                                   self.lbl_container.configure(text="未配置服务器")))
            return
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self):
        st = self.ctl.container_status()
        color = {True: "#22a94c", False: "#c2c2c2", None: "#e0a02a"}[st["online"]]
        text = st["text"] + ("（{}）".format(st["version"]) if st["version"] else "")
        healthy = bool(st["online"]) and "healthy" in st["text"]
        self.after(0, self._update_container_ui, color, text, st["online"], healthy)

    def _update_container_ui(self, color, text, online, healthy):
        """刷新状态灯，并按容器状态联动按钮可用性：
           - 隧道按钮：容器在线即可用（隧道只是条通路，不要求 dsh 已就绪）
           - 打开 DSH：需容器 healthy（healthcheck 通过 = dsh 真正可服务）"""
        self._container_online = bool(online)
        self._container_healthy = bool(healthy)
        self._draw_dot(self.dot_container, color)
        self.lbl_container.configure(text=text)
        # 按钮联动
        if online:
            self.btn_tunnel.configure(state="normal")
            self.btn_open.configure(state="normal" if healthy else "disabled")
        else:
            self.btn_tunnel.configure(state="disabled")
            self.btn_open.configure(state="disabled")
            # 容器没了（被外部停止/崩溃）→ 顺手断开隧道，避免死链
            if self.tunnel.is_up:
                threading.Thread(target=self._tunnel_stop_worker, daemon=True).start()
        # 按钮文案提示等待原因
        if online and not healthy:
            self.btn_open.configure(text="dsh 启动中...")
        else:
            self.btn_open.configure(text="打开 DSH")
        # 启停合一按钮：仅在已部署且无动作进行中时跟随状态刷新文案
        if self.deploy_state == "deployed":
            if str(self.btn_container.cget("state")) != "disabled":
                self.btn_container.configure(
                    text="停止容器" if online else "启动容器")
        else:
            self.btn_container.configure(state="disabled")
        if self._tray is not None:
            try:
                self._tray.title = "dsh-switch - 容器: {}".format(text)
            except Exception:
                pass

    # ================================================== 隧道 ----
    def toggle_tunnel(self):
        if not self._container_online:
            core.log("容器未运行，隧道不可用（先启动容器）")
            return
        if self.tunnel.is_up:
            self.btn_tunnel.configure(state="disabled")
            threading.Thread(target=self._tunnel_stop_worker, daemon=True).start()
        else:
            self.btn_tunnel.configure(text="连接中...", state="disabled")
            threading.Thread(target=self._tunnel_start_worker, daemon=True).start()
        self.after(400, self._refresh_tunnel_dot)

    def _tunnel_start_worker(self):
        err = self.tunnel.start()
        if err:
            core.log("隧道启动失败: {}".format(err))
        else:
            core.log("隧道已启动: localhost:{} -> {}:{}".format(
                self.cfg["port"], self.cfg["host"], self.cfg["port"]))
        self.after(0, self._refresh_tunnel_dot)

    def _tunnel_stop_worker(self):
        self.tunnel.stop()
        core.log("隧道已停止")
        self.after(0, self._refresh_tunnel_dot)

    def _refresh_tunnel_dot(self):
        if self.tunnel.is_up:
            self._draw_dot(self.dot_tunnel, "#2f7fe0")
            self.lbl_tunnel.configure(
                text="运行中  localhost:{}".format(self.cfg["port"]))
            self.btn_tunnel.configure(text="停止隧道", state="normal")
        else:
            self._draw_dot(self.dot_tunnel, "#c2c2c2")
            self.lbl_tunnel.configure(text="已停止")
            # 容器离线时按钮保持禁用（由巡检联动统一管理）
            self.btn_tunnel.configure(text="启动隧道",
                                      state="normal" if self._container_online else "disabled")

    # ================================================== 打开 DSH ----
    def open_dsh(self):
        def worker():
            if not self._container_online:
                core.log("容器未运行，请先启动容器")
                return
            if not self.tunnel.is_up:
                core.log("隧道未启动，自动拉起...")
                err = self.tunnel.start()
                if err:
                    core.log("隧道启动失败: {}".format(err))
                    return
                self.after(0, self._refresh_tunnel_dot)
                time.sleep(0.5)
            # 等 dsh 就绪：容器刚启动时 health 还是 starting，页面打不开
            core.log("等待 dsh 就绪...")
            deadline = time.time() + 90
            ready = False
            while time.time() < deadline:
                try:
                    s = socket.create_connection(
                        ("127.0.0.1", int(self.cfg["port"])), timeout=3)
                    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                    data = s.recv(64)
                    s.close()
                    if data:            # 200/303/401 都算 dsh 已应答
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(3)
            if not ready:
                core.log("dsh 迟迟未就绪（90 秒超时），请检查容器状态后重试")
                return
            core.log("获取 dsh token...")
            token = self.ctl.fetch_token()
            if not token:
                core.log("未取到 token（容器可能没起来），请先检查容器状态")
                return
            url = "http://localhost:{}/?token={}".format(self.cfg["port"], token)
            core.log("打开 {}".format(url))
            webbrowser.open(url)
        threading.Thread(target=worker, daemon=True).start()

    # ================================================== 托盘 ----
    def _init_tray(self):
        if pystray is None or self._icon_image is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda *a: self.after(0, self._show_window),
                             default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("启动隧道", lambda *a: self.after(0, self._tray_toggle_tunnel)),
            pystray.MenuItem("停止隧道", lambda *a: self.after(0, self._tray_stop_tunnel)),
            pystray.MenuItem("打开 DSH", lambda *a: self.after(0, self.open_dsh)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *a: self.after(0, self._exit_all)),
        )
        self._tray = pystray.Icon("dsh-switch", self._icon_image,
                                  "dsh-switch - 启动中...", menu)
        threading.Thread(target=self._tray.run_detached, daemon=True).start()

    def _tray_toggle_tunnel(self):
        self._show_window_quiet()
        if not self.tunnel.is_up:
            self.toggle_tunnel()

    def _tray_stop_tunnel(self):
        if self.tunnel.is_up:
            self.toggle_tunnel()

    def hide_to_tray(self):
        self.withdraw()
        try:
            if self._tray is not None:
                self._tray.notify("程序已最小化到托盘，右键图标可退出")
                self.after(4000, lambda: self._tray.remove_notification())
        except Exception:
            pass
        core.log("窗口已收进托盘（程序继续运行）")

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _show_window_quiet(self):
        self.deiconify()
        self.lift()

    def _exit_all(self):
        core.log("退出：断开隧道并结束程序")
        self.tunnel.stop()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)


def re_fullmatch(s, pattern):
    import re as _re
    return _re.fullmatch(pattern, s) is not None


# ============================================================ 自检 ----
def selftest():
    """无 UI 自检：容器巡检 / token / 隧道启停（使用已保存的 dsh-switch.json）。

    所有探测步骤都兜异常：容器停止、上游拒连属正常状态，打印结论而非崩溃。
    """
    cfg = core.load_config()
    if not cfg.get("host"):
        print("no config, nothing to test")
        return 1
    ctl = core.ServerCtl(cfg)
    st = ctl.container_status()
    print("container:", st)
    token = ctl.fetch_token()
    print("token:", (token[:8] + "...") if token else None)
    t = core.Tunnel(cfg)
    err = t.start()
    print("tunnel start:", err or "ok")
    if err:
        return 1
    time.sleep(1)
    try:
        s = socket.create_connection(("127.0.0.1", int(cfg["port"])), timeout=5)
        s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        print("tunnel probe:", s.recv(64)[:32])
        s.close()
    except Exception as e:
        # 容器停止 / 上游拒连时走到这里，属预期状态而非崩溃
        print("tunnel probe failed (容器未启动? 被上游拒绝?):", e)
    t.stop()
    print("tunnel stopped:", not t.is_up)
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if not acquire_single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showwarning("dsh-switch", "dsh-switch 已经在运行中（请查看系统托盘鲸鱼图标）")
        r.destroy()
        sys.exit(0)
    APP = App()
    APP.mainloop()
    os._exit(0)

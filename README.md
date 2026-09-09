# dsh-switch

**一键把 [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) 部署到你的服务器，本地 SSH 隧道即开即用。**

一个 Windows 单文件小工具：填入服务器地址和 SSH 凭据 → 一键部署最新版 dsh（Docker）→ 一键开关 SSH 隧道 → 浏览器直接使用。全程无需在服务器上敲一行命令。

> DeepSeek Harness 是 DeepSeek 开源的 Agent 运行时（「一切皆插件」）。
> 本项目是独立的第三方部署/管理工具，与 DeepSeek 官方无隶属关系。

## ⚠️ 适用场景与安全建议

**建议部署在本地局域网服务器**（NAS、软路由、家庭服务器等），并在防火墙上确认 dsh 端口未对公网开放。

**不建议部署到公网服务器 / 云服务器**，原因：

- dsh Web 界面目前仅有 URL token 一层防护，**没有用户名密码和登录会话机制**，token 一旦泄露等于把 Agent 的文件读写和命令执行能力暴露出去
- 本工具的 SSH 隧道设计本身就是为「内网服务器 + 异地取用」准备的，公网场景请改用堡垒机/VPN 等成熟方案
- 若确有公网需求，必须自行加上前置认证（如反向代理 + Basic Auth / VPN），并自行评估风险

## 功能

- **一键部署**：环境自动探测（架构 / Docker / 端口 / 容器名冲突 / 权限）→ 生成部署文件 → 构建 → 健康检查，全程日志实时回显
- **版本可选**：稳定版（npm `latest`）/ 预览版（自动解析 npm `alpha` 标签的最新版本号）
- **SSH 隧道开关**：一键开合，本地端口直达服务器上只绑回环的 dsh（官方安全限制 `127.0.0.1` only，不暴露任何端口到局域网/公网）
- **打开 DSH**：自动拉起隧道 → 实时抓取最新 token（容器重建会轮换）→ 浏览器打开页面
- **容器管理**：状态巡检（在线/离线/版本号，10 秒轮询）、启动 / 停止容器
- **常驻托盘**：点关闭收进托盘，托盘右键退出（退出即断隧道）；托盘悬停显示容器状态
- **单实例**：重复启动自动拦截

## 快速开始

1. 从 [Releases](../../releases) 下载 `dsh-switch-vX.X.X-win32.exe`（单文件，免安装）
   - 国内下载加速（任选其一，在原链接前加代理前缀）：
     ```
     https://ghfast.top/https://github.com/dxfong/dsh-switch/releases/download/v1.0.0/dsh-switch-v1.0.0-win32.exe
     https://gh-proxy.com/https://github.com/dxfong/dsh-switch/releases/download/v1.0.0/dsh-switch-v1.0.0-win32.exe
     ```
   - 也可克隆加速：`git clone https://ghfast.top/https://github.com/dxfong/dsh-switch.git`
2. 首次打开在「部署」页填写：
   - 服务器地址、SSH 端口、用户名
   - 认证方式：密码 或 私钥文件
   - 容器名（默认 `dsh`）、端口（默认 3080，服务器与本地隧道共用）
   - dsh 版本：稳定版 / 预览版
3. 点「测试连接」确认 → 点「开始部署」（首次约 3~8 分钟，日志实时可见）
4. 看到「部署成功」后，切到「控制台」→「启动隧道」→「打开 DSH」

> 已有部署？程序启动时会自动探测并进入「已部署」状态，直接用控制台即可。

## 工作原理

```
本机浏览器 ──► localhost:PORT ──► SSH 隧道 ──► 服务器 127.0.0.1:PORT ──► dsh 容器 (host 网络)
```

- dsh 官方出于安全考虑只允许监听 `127.0.0.1`（防止把 Agent 的代码执行能力暴露到网络），
  因此服务器侧不开放任何对外端口，**SSH 隧道是唯一远程入口**，安全边界 = SSH 认证本身
- 容器以 `network_mode: host` + 非 root 用户 + 只读根文件系统 + capability 全卸载运行
- 数据（配置 / 会话 / 工作区）持久化在服务器 `<部署目录>/data/`，升级镜像不丢数据

## 安全与隐私

- 凭据只保存在**本机** `dsh-switch.json`（与 exe 同目录，由程序表单自动生成，无需手动编辑）
- 密码默认**不**写入磁盘（「记住密码」默认关闭，开启则以明文保存——请自行权衡）
- 程序不做任何遥测与上报；所有网络交互仅发生在你填写的服务器与 npm registry 之间
- 本工具需要你服务器的 SSH 凭据才能工作，请从本仓库源码自行构建以获得完全可信的版本

## 从源码构建

依赖：Windows + Python 3.8+（含 tkinter）

```bash
python -m venv venv
venv\Scripts\pip install paramiko pystray pillow pyinstaller
venv\Scripts\pyinstaller --onefile --noconsole --icon assets/dsh.ico ^
    --add-data "assets/dsh-icon-512.png;assets" ^
    --add-data "templates;templates" ^
    --name dsh-switch src/dsh_switch.pyw
# 产物：dist/dsh-switch.exe
```

无 UI 自检：`dsh-switch.exe --selftest`（需已配置 dsh-switch.json）

## FAQ

**页面能打开，但操作时报错 / API 403？**
dsh 有浏览器信任围栏（trusted-host）。本工具生成的模板已按你填的端口自动配置；
若手动改过端口或经域名/反代访问，需在 compose 的 `DSH_TRUSTED_HOST` 中追加对应主机名。

**我的服务器 SSH 是 dropbear（OpenWrt/iStoreOS），公钥登录被拒？**
dropbear 对公钥的兼容问题偶有发生。直接用密码认证即可，本工具的密码认证不受影响。

**部署时报 `notarget / No matching version found`？**
国内 npm 镜像源（npmmirror）对部分子包同步滞后。本工具已内置自动回退：镜像源构建失败会自动切官方源重试。

**端口 3080 被占了？**
换一个端口（如 33080），「服务器 dsh 监听」与「本地隧道」共用同一端口值，两边自动对齐。

**非 root 用户部署？**
需要把用户加入 docker 组：`sudo usermod -aG docker <用户名>` 后重新登录生效。

**dsh 的访问 token 在哪？**
容器首次启动自动生成，可随时在服务器执行 `docker logs <容器名> | grep token` 查看；
「打开 DSH」按钮每次都会自动获取最新 token，无需手动管理。

**支持升级吗？**
在「部署」页把版本切换到另一通道（或等上游发新版）后点「重新部署」即可——数据卷保留，
仅重建容器。建议升级前备份服务器上的 `<部署目录>/data` 目录。

## 已知限制

- 仅支持 Windows 客户端（macOS/Linux 欢迎提 PR）
- 单用户模型：dsh 本身无多用户认证，请勿将隧道/端口共享给不受信任的人
- dsh 目前处于 0.1 开发者预览阶段，上游接口可能变化；遇问题请附日志提 issue

## License

[MIT](LICENSE) 。图标素材取自 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（MIT）。

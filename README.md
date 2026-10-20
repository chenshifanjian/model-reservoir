# 模型蓄水池 · Model Reservoir

聚合多个 LLM 提供商额度，对外同时提供 **OpenAI 兼容接口**与 **Anthropic 兼容接口**，支持智能轮询、流式故障转移、无感容灾切换。

> 本仓库 fork 自 [zk-2025/model-gateway](https://github.com/zk-2025/model-gateway)（原作者已声明项目终止、转向收费版）。原仓库删除了全部源码，本 fork 从 git 历史恢复了完整源码，并做了 **Windows / macOS / Linux 三端兼容**改造。
>
> 许可证：**CC BY-NC 4.0**（署名 + 非商用），沿用原仓库。
> **2026-10-03 原作者（东野 / ywtc000）已书面授权**本 fork 随意二次开发并开源（微信原话："可以的，随便改吧"）；本仓库在授权范围内免费开源，原作者署名保留。

## 功能

- OpenAI 兼容：`/v1/models`、`/v1/chat/completions`（含 SSE 流式）
- **Anthropic 兼容**：`/v1/messages`（含 SSE），Claude Code / Cline 可直接指向本网关
- 多提供商聚合：智能轮询 + 滑动窗口质量分 + 流式故障转移/熔断重置
- **免费档配额账本**：按 (提供方, 模型) 与 (提供方账号) 记额度，429 按阶梯退避，
  文案点名「每日」时避让到 UTC 午夜；只排序不移除候选（`/api/quota` 可查）
- **X-Routed-Via / X-Fallback-Detail**：每次响应回答「实际谁回的、跳过了谁」
- 桌面控制台（pywebview 原生窗口）：提供商管理、模型管理、稳定性监控、调用日志、一键配置
- **组队**：建队生成 6 位码，朋友输码入队，你逐个上游勾选同步（端到端加密）
- **本地 API Key 可一键随机生成**：两步确认、立即生效（不用重启），旧 key 当场作废
- 单实例运行、开机自启、公告推送（三端各自原生实现）

## 组队

第五页「组队」。你建一个临时队伍、生成 **6 位组队码**，朋友装同版本软件输码入队，
你批准后可以**逐个上游**勾选同步给他 —— **默认不同步，勾了才给**。

- 同步是**端到端加密**的：协调服务器只存密文信箱，看不到密钥，也不转发对话
- 队长有权批准、移出成员、关闭队伍；队员用量只读统计
- 关队后码**立即失效**，队员那边同步来的上游自动置灰（模型全部禁用），记录保留、可自行删除
- 本地身份用 UUID，队内昵称可改；队伍默认 7 天有效期

> ⚠️ **同步即复制密钥到对方机器，无法远程收回。** 只能让对方删除，或你自己去上游换 Key。

### 关于协调服务器

**组队是完全可选的。** 没有服务器时，多上游聚合、双协议兼容接口、模型管理、
稳定性监控、调用日志、一键配置**全部正常工作** —— 只有组队这一个功能需要服务器。

协调服务器已经拆成独立开源项目，可自己搭建（512MB 内存的小机器就够，一条命令部署）：

**➡️ https://github.com/chenshifanjian/reservoir-team**

或者不自建，客户端「组队」页里的协调服务器输入框默认指向项目方维护的服务器，开箱即用。

服务端只当加密信箱，代码里没有解密能力（不 import `cryptography`）。
`protocol.py` 是两边共用的密钥派生实现，两份必须字节一致 —— **两边的 CI 都会做一致性检查**，
任一边改了而另一边没同步，CI 直接失败。

## 三端运行（源码模式）

通用前置：Python 3.11+。

```bash
git clone https://github.com/chenshifanjian/model-reservoir.git
cd model-reservoir
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Linux（Arch / 任意发行版）

```bash
# ① 原生窗口模式（推荐桌面用法）
# 注意：uv 建的 venv 看不到 pacman 装的 gi/WebKit2，
# 必须用系统 Python 建带 system-site-packages 的环境：
python3 -m venv --system-site-packages .venv-gui
.venv-gui/bin/pip install -r requirements.txt
GATEWAY_DATA_DIR=dist .venv-gui/bin/python app.py   # 弹原生窗口

# ② 无头模式（服务器 / 命令行）
GATEWAY_HEADLESS=1 python app.py     # 打开 http://127.0.0.1:8000
```

系统依赖：`webkit2gtk-4.1` + `python-gobject`（Arch 默认已带，缺则 `sudo pacman -S webkit2gtk-4.1 python-gobject`）。

> **Linux 默认关闭系统托盘**：pystray 的 GTK 后端会和 pywebview 抢 GLib 主循环导致段错误。
> 需要托盘时：`GATEWAY_TRAY=1 PYSTRAY_BACKEND=xorg ... python app.py`（需 Xwayland + python-xlib）。
> 无托盘时**关闭窗口即退出**，不留后台进程。

开机自启：设置页开关会写入 `~/.config/autostart/model-gateway.desktop`（XDG 标准，GNOME/KDE 等均支持）。

### macOS

```bash
python app.py                        # WKWebView 原生窗口（pywebview 自带）
# 或无头：GATEWAY_HEADLESS=1 python app.py
```

开机自启：开关会安装 `~/Library/LaunchAgents/com.modelgateway.client.plist`（launchd）。
打包产物数据目录：`~/Library/Application Support/ModelGateway`（.app 不可写）。

### Windows

```powershell
python app.py
```

开机自启写注册表 `HKCU\...\Run`；在线热更新（.exe 替换）为 Windows 独有功能。

**安装包（推荐）**：Release 里的 `ModelReservoir-v<版本>-windows-setup.exe`。
per-user 安装到 `%LOCALAPPDATA%\Programs\ModelReservoir`，不弹 UAC；
卸载只删程序，`config.json` / `providers.json` / 用量数据保留。
不要手动把它装到 `Program Files` —— 打包版的数据目录就是 exe 所在目录，那里不可写。

## 打包

```bash
# Linux / macOS
./scripts/build.sh

# Windows（PowerShell）
pyinstaller 模型蓄水池.spec
```

spec 文件按平台自动裁剪：Windows 走 win32 托盘，macOS 额外产出 `ModelReservoir.app`；
仓库默认**不**内置 WebView2 安装器（`MicrosoftEdgeWebview2Setup.exe` 存在时才会打进去），
因此 Windows 依赖系统已有的 WebView2 运行时（Win11 自带，Win10 一般随 Edge 装上）。
图标由 `scripts/make_icon.py` 生成到 `assets/`（`.ico` / `.icns` / `.png`）。

Linux：`build.sh` 优先用 `.venv-gui`（系统 Python + `--system-site-packages`）打包，spec 检测到本机 gi 类型库就把 Gtk/WebKit2 类型库打进包 → **产物自带原生窗口**（目标机需装 `webkit2gtk-4.1`）；没有 gi 环境则回退 headless 版（浏览器访问）。Linux 系统托盘默认关闭（pystray GTK 与 pywebview 抢主循环会崩）。

## 测试

```bash
python -m pytest tests/ -q    # 全部通过（含更新链、版本门禁、组队同步）
```

## 平台适配说明

| 能力 | Windows | macOS | Linux |
|---|---|---|---|
| 单实例锁 | `msvcrt` | `fcntl` | `fcntl` |
| 杀旧实例 | `netstat`+`taskkill` | `lsof` | `ss` |
| 开机自启 | 注册表 | LaunchAgent | XDG autostart |
| GUI | WebView2 | WKWebView | WebKitGTK（缺依赖自动回退浏览器） |
| 数据目录 | exe 同目录 | Application Support | exe 同目录 |

> 三端都支持用环境变量 `GATEWAY_DATA_DIR` 指定数据目录（打包版同样生效）——
> 便携模式、多开测试、把配置放到同步盘都靠它。
| 在线热更新 | ✅ | 手动更新 | 手动更新 |

环境变量：`GATEWAY_HEADLESS=1`（无头）、`GATEWAY_NO_BROWSER=1`（不自动开浏览器）、`GATEWAY_DATA_DIR`（数据目录）、`GATEWAY_AUTO_KILL=1`（启动时杀旧实例，测试用）。

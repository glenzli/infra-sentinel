# Windows 日常预览版

Windows 原生桌面版沿用 Tauri 界面和 Python Agent，共用计量、SQLite 历史、
Projection 和设置协议。首阶段针对 Windows x64；Linux 桌面发布排在 Windows 后。
现有正式 Release 的 macOS 支持范围不变。

从 [2.2.0-beta.1 预发布版](https://github.com/glenzli/infra-sentinel/releases/tag/v2.2.0-beta.1)
下载 `Infra-Sentinel-2.2.0-beta.1-windows-x64-unsigned-setup.exe` 和对应 `.sha256`。
这是 Windows 专用预览发布；macOS 稳定版继续使用 2.1.2。

## 首阶段范围

| 能力 | Windows 日常版 |
| --- | --- |
| 桌面、托盘、恢复窗口 | 支持；关闭窗口保留托盘，托盘菜单退出 |
| 重复启动 | 激活已有窗口，保持一个桌面实例 |
| AI 用量 | 沿用各来源解析器；读取本机已有记录，不自动读取 WSL |
| Codex 路径 | `%USERPROFILE%\.codex`，或启动应用时继承的 `CODEX_HOME` |
| DSH 路径 | `%USERPROFILE%\.dsh`，或 `DSH_HOME`；下述构建包含 Zstd 支持 |
| OpenCode | 自动发现 CLI / 数据库；设置可指定便携版的绝对路径 |
| CPU、内存、磁盘容量 | 原生 Win32 采集；其余系统指标按能力隐藏 |
| 上游状态 | 读取公开官方状态页；连接失败保持未知 |
| 远端 Linux / Xray | 通过 Windows OpenSSH 或设置中的 SSH 可执行文件及已有 Host 别名读取 |
| 本机 Mihomo 流量 | 后续阶段补回环 Controller 和 Secret；当前停用且不产生采集故障或零值指标 |
| Infra Protocol 本地设施 | 原生 Named Pipe；支持 PCP、Infer Runtime、Dev Mesh Observer 的既有协议，需服务端发布 Windows offer |
| Windows 系统通知 | 后续阶段；当前可在应用和诊断历史中查看异常 |

SSH 使用 `%USERPROFILE%\.ssh\config` 中现有的 Host 别名；先在终端验证同一
客户端能以密钥连接并已信任主机。应用不交互询问密码，不复制密钥，也不修改 SSH 配置。
真实客户端版本、已有日志和远端环境仍需在使用机器上核对；合成夹具不代表任意
第三方客户端版本或用户 VPS 已完成连接验证。

未配置远端主机时，Windows 日常版隐藏暂未支持的网络模块；配置 SSH 远端主机后
仍可查看远端网络观测。本机 Mihomo 的未支持状态不计入来源故障或健康告警。

Infra Protocol 使用 Known Folder API 定位当前用户的共享 Runtime；也可通过
`INFRA_PROTOCOL_RUNTIME_DIR` 指向真正共享的本机目录。程序检查目录与注册文件的
所有者和 ACL，拒绝远程路径、重解析点及其他用户的管道服务端。服务端仍需按协议
检查客户端 SID。连接、收发与必要的 EOF 共用请求时限，超时后取消并收尾原生 I/O。
应用不会创建、修复或删除第三方注册文件。

三个协议已通过真实 Windows 管道和跨进程打包 Agent 的合同夹具验收。本机目前没有
实际设施注册，尚未完成三个产品各自的 Windows 实机联调；服务端若仅提供 Unix
Socket 或运行在 WSL 中，不会因此自动获得 Windows Named Pipe 支持。

## 从源码构建

需要 Windows x64、Python 3.11+、Node.js LTS、Rust MSVC 工具链、Visual Studio
Build Tools 的 C++ 桌面开发组件和 Windows SDK。无需 Git Bash 或 WSL。

在项目根目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[dsh]" pyinstaller
.\bin\build-desktop-app.ps1
```

打包脚本优先使用项目 `.venv`，也可以用 `PYTHON3` 指定一个 Python 可执行文件的
完整路径。Agent 必须在目标系统和架构上构建。Rust 与前端 Tauri 保持 2.11 版本线。

构建产物：

```text
ui\src-tauri\target\release\bundle\nsis\Infra Sentinel_2.2.0-beta.1_x64-setup.exe
```

安装包按当前用户安装，并内含 Agent、默认配置和 WebView2 引导程序；最终用户
无需 Python、Node.js 或 Rust。缺少 WebView2 时引导程序需要联网。当前本地预览包
未做代码签名。稳定版发布、代码签名和 CI 发布矩阵属于后续发布工作。

发布用的规范文件名和 SHA-256 由以下入口生成，写入 `dist/`：

```powershell
.\bin\package-release.ps1
# 已完成本次版本构建时，可复用经版本校验的安装包：
.\bin\package-release.ps1 -SkipBuild
```

只生成桌面文件可运行 `.\bin\build-desktop-app.ps1 -NoBundle`。
直接运行时，将 `infra-sentinel-desktop.exe`、`infra-agent.exe` 和
`config.example.toml` 保留在同一目录。

## 本地数据与生命周期

默认配置和状态分别保存在：

```text
%LOCALAPPDATA%\Infra Sentinel\config.toml
%LOCALAPPDATA%\Infra Sentinel\state\
```

`INFRA_SENTINEL_STATE_DIR` 可覆盖状态目录，配置位于该目录的父目录。
Agent 和原生桥共享这个路径约定。设置保存成功后，Agent 完成当前采样并退出，
桌面监督器重新启动它；账本保留。桌面退出或异常终止后，Agent 会检测记录的
桌面进程并收尾；后台 curl、SSH、OpenCode 采样和 Agent 均不应弹出控制台。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Push-Location ui
npm.cmd run build
Push-Location src-tauri
cargo test --locked
Pop-Location
Pop-Location
.\.venv\Scripts\python.exe scripts\smoke_windows.py --agent ui\src-tauri\binaries\infra-agent-x86_64-pc-windows-msvc.exe
.\.venv\Scripts\python.exe scripts\smoke_windows.py --desktop ui\src-tauri\target\release\infra-sentinel-desktop.exe
```

Windows 原生用例检查真实 Win32 资源和不会终止目标的进程探测；POSIX 权限、
Unix Mihomo Controller、Linux page-size 等目标专属用例在 Windows 跳过。
集成验收使用 `.build/windows-smoke-*` 中独立的合成日志和状态，检查真实打包产物的
Projection、设置往返和重启后计数不重复，以及父进程退出后的 Agent 收尾；桌面验收
另检查首次配置创建和重复启动。它不读取个人 AI 历史、SSH 配置或真实本地设施，
但保留生产版公开状态页和价格目录的只读请求。

管道与 Codex 的完整数据链路可单独重复验收：

```powershell
.\.venv\Scripts\python.exe scripts\verify_windows_integration.py --agent ui\src-tauri\target\release\infra-agent.exe
# 显式选择真实 Codex 历史；仅读取 Token 元数据，不复制原始记录。
.\.venv\Scripts\python.exe scripts\verify_windows_integration.py --agent ui\src-tauri\target\release\infra-agent.exe --codex-home "$env:USERPROFILE\.codex"
```

这项验收使用三个真实 Windows 管道模拟合同，检查打包 Agent 的发现、请求、投影和
重启。真实 Codex 核对按持久账本记录的文件末尾位置独立重放同一前缀，允许客户端
继续追加日志；缺失或截断的前缀会使验收失败。再用生产界面的汇总函数核对今日、
7 天、30 天和全部历史的来源总量、模型组成与历史覆盖。报告和聚合数据写入独立的
`.build/windows-integration-*`，不改动用户的日志或正式应用状态。

真实桌面的托盘交互、缩放、休眠恢复和长时间运行仍应纳入每次发布的人工验收。

# Infra Sentinel

[English](README.md) | **中文**

Infra Sentinel 是一个本地优先的个人 AI Infra 可观测面板。它目前覆盖可计量资源、上游服务状态，以及参与接入的本地设施健康状态：

- **网络**：本机 Mihomo 流量、域名与代理路径归因、Linux VPS 账单量、Xray 用户逻辑流量；
- **AI 用量**：OpenCode 与 Codex 的本地 Token 记录、模型构成、消耗速率和 Agent 活动；
- **本机系统**：整机 CPU、内存压力与 Swap、磁盘容量、物理磁盘吞吐与 IOPS、温度压力；
- **上游服务**：低频、只读观测 OpenAI、Claude 与 DeepSeek 的官方 API 状态；
- **本地设施**：自动发现兼容的 runtime 与服务，按各自协议投影受限健康状态。

它还会通过 [Infra Protocol](https://github.com/glenzli/infra-protocol) 的发现合同自动发现参与接入的本地基础设施。每个设施都作为独立资源卡展示受限的只读详情；设施专属的诊断与操作仍留在原生 Console，并由系统浏览器打开。

菜单栏只表达整体健康状态。详细数据、趋势、来源差异和告警都在 App 中查看。

它不会抓包，不读取提示词、响应正文、URL 路径或项目文件，也不会杀进程、删除文件、断网或修改代理配置。

![Infra Sentinel 中文概览](assets/overview-zh.png)

![Infra Sentinel AI 用量](assets/ai-usage-zh.png)

> 截图使用匿名演示数据生成，不包含真实主机名、SSH 别名、IP、账户或本机路径。

## 它回答什么

Infra Sentinel 不把字节、Token 和告警硬凑成一个“总分”。每个资源模块保留自己的单位和统计口径，用来回答不同问题：

- 今天哪一类资源消耗最多？
- 消耗来自哪个 Agent、模型、域名、代理路径或 VPS？
- 当前增长速度是否异常？
- 本机观测值、代理逻辑量和 VPS 账单量为什么不同？
- 某个数据源是否失联，统计窗口是否完整？
- 本地异常发生时，上游 API 是否也有已经确认的公共事件？
- 哪些本地基础设施正常或降级，它的原生 Console 在哪里？
- Agent 集群是否正在把 Mac 推入内存、磁盘容量、I/O 或温度压力？

如果某项数据无法可靠获得，界面会显示未知、隐藏该模块或标记来源异常，不用推断值冒充账单。

## Network 模块

本机网络以 Mihomo / Clash Meta 兼容内核为事实来源：

- 自动发现当前用户可访问的 Mihomo Unix Socket；
- 每 5 秒形成一个持久化区间，区间内默认每 250ms 读取一次活跃连接；
- 使用 `uploadTotal + downloadTotal` 的相邻增量作为本机精确总量；
- 按站点主域和实际 `chains` 归因域名、代理、`DIRECT`、阻断与未知路径；
- 把无法在两个轮询点之间观察到的短连接保留为“未归因”，不静默丢失。

始终满足：

```text
已归因域名 + 未归因 = Mihomo 精确总增量
代理 + DIRECT + 阻断 + 未知 + 未归因 = Mihomo 精确总增量
```

远端主机是可选能力。每台主机独立保存基线、计费方向和每日用量阈值：

- 通过 `~/.ssh/config` 中的 Host 别名短时、只读访问 Linux VPS；
- 从 `/sys/class/net` 自动选择公网网卡并读取 RX/TX 字节与包数；
- 可选读取仅监听远端 `127.0.0.1:10085` 的 Xray StatsService；
- 多台 VPS 的账单量可以汇总，但倍率只在同一主机、同一覆盖范围内解释。

VPS 支持 `RX + TX` 双向计费或仅 `TX` 计费。Xray 用户逻辑流量与网卡账单量属于不同物理层，不会被误加成一个总量。

## AI 用量模块

AI 模块只读取客户端已经保存在本机的聚合元数据。每个 Provider 都通过同一份用量合同输出：今日量、可读历史总量、模型维度和来源特有诊断。

### OpenCode

- 优先以 SQLite 只读方式聚合 OpenCode Desktop 的 assistant 消息 Token 元数据；
- 可提供输入、输出、推理、缓存、模型、消息数及 Provider 报告的 cost；
- Desktop 库不可用时才尝试兼容的 `opencode stats --days 0 --models`；
- 使用持久化计数器 checkpoint，App 重启不会重复写入当天历史。

### Codex

- 以 SQLite 只读方式读取 `~/.codex/state_5.sqlite` 的主任务 Token 计数、模型和派生拓扑；
- 今日量从 Sentinel 当天首次建立的本机基线开始；
- 以每个主任务的正增量按模型归集，保证 Codex 总量与模型之和守恒；
- 展示主任务、子 Agent、近期活动和最大派生层级，但不读取任务标题或内容。

OpenCode 的自然日统计与 Codex 的本机基线窗口可能不同，界面会直接展示每个来源的窗口覆盖。AI 汇总用于本机趋势分析，不是 ChatGPT、Codex 或任意 API Provider 的账户账单。

## 本机系统模块

系统 Collector 消费平台无关的能力合同，不假设每个系统都能提供相同计数。当前经过验证的 macOS backend 会观测整机 CPU 使用率、原生内存压力、压缩内存与 Swap、磁盘剩余空间、物理磁盘字节与操作次数、保守的磁盘健康证据、温度压力。吞吐当前值跟随 Agent 正常采样刷新；历史状态值与区间计数每 5 分钟落盘；平台支持时，磁盘健康在启动时读取一次，之后每 6 小时刷新。

首批 Linux 与 Windows backend 已建立跨平台边界：Linux 使用整机 procfs/sysfs 计数；Windows 使用稳定的 Win32 CPU、内存和磁盘容量接口。每个 backend 会明确声明自身能力，UI 会隐藏不可用测量，而不是把它画成 0 或“正常”。目前打包并经过 Release 验证的桌面目标仍只有 macOS。

告警只采用可靠的压力信号：macOS 内存压力、磁盘空间低于 10% 或 5%，以及较高或严重温度压力。CPU 和磁盘高活动会展示趋势，但在没有持续压力合同前不会仅凭数值触发告警。第一版只看整机，按 Agent 的进程归因留给后续能力。

## 上游服务状态

Infra Sentinel 每 5 分钟只读获取一次 OpenAI、Claude 与 DeepSeek 的公开官方状态摘要，展示相关 API 组件、活动事件、官方更新时间，并提供进入供应商原生状态页的链接。整个过程不需要 API Key，也不会发起合成模型调用。

官方状态只用于辅助诊断，不保证特定账户、模型、层级或地区一定可用。网络或解析失败会明确显示为**未知**，不会被转换为供应商故障；只有官方 API 组件已经确认的异常与恢复才会形成告警和通知。

## 分析视图

Network、AI 用量与本机系统资源都使用同一套观测结构：

- 固定摘要：今日观测、历史总量或账单量、采集覆盖；
- 时间范围：今日、近 7 天、近 30 天、全部历史；
- 构成分析：按 Agent、模型、域名、路径或 VPS 展开；
- 速率趋势：按真实采样时间计算，不把补记流量伪装成实时峰值；
- 每日历史：堆叠柱状图保留总量与组成关系。

历史查询通过独立只读通道执行，不等待 5 秒网络采样周期；UI 只消费受限查询结果，不接触 SQLite 或任意文件。

## 本地设施发现

接入服务发布由 [Infra Protocol](https://github.com/glenzli/infra-protocol) 定义的短时、仅当前用户可读租约。
Sentinel 不扫描端口、不按进程名猜测，只对具体协议版本和 binding 做精确交集，然后连接所选服务。
Discovery 不携带指标、Console URL、通用请求信封，也不会赋予启停、配置或维护权限。

当前已验证的接入包括
[Paged Context Protocol (PCP)](https://github.com/glenzli/paged-context-protocol) 与
[Infer Runtime](https://github.com/glenzli/infer-runtime)。两套应用协议仍彼此独立；Infra Protocol 只统一发现。
Sentinel 为两者分别实现 adapter，只把有界状态、指标、问题、观测时间与可选的本机
**打开 Console** 链接投影到 UI。每个被发现的设施都是独立的一级模块与详情页。
未知协议会被忽略，不会被猜测为兼容。详见[设施发现](docs/facility-discovery.md)。

## 架构

```text
Mihomo / VPS / Xray / OpenCode / Codex / 平台系统 backend → Collectors → SQLite 指标 ┐
供应商官方状态源 → 低频状态观测器 ─────────────────────────────────┤
本地设施 → Infra Protocol discovery → Provider adapters ──────────┤
                                                                   ↓
                                                版本化 Projection + 命令
                                                                  ↓
                                                    Tauri UI / 通知
```

- Python Agent 是采样、计量、存储、策略和 Projection 的唯一所有者；
- SQLite WAL 使用稳定身份键写入区间 counter，防止重启或回填重复计数；
- Tauri WebView 只能读取版本化 Projection，并提交白名单命令；
- Rust bridge 不提供任意文件访问、Shell 或 SQL 能力；
- Collector 失败相互隔离，一个来源异常不会阻断其他资源；
- 设施发现与 Provider 协议 I/O 使用独立生命周期，不会阻塞资源采样。

平台专属行为被限制在窄 adapter 后面：系统资源 backend、Agent 单实例锁、原生通知、URL 打开和本地应用发现。Projection、指标存储、策略和 Web UI 保持平台无关，并且只展示 backend 明确声明的能力。

Projection 与发现合同使用日期化 schema，并要求精确的兼容版本。指标查询支持分钟、5 分钟、小时和天级聚合，查询范围和结果点数均有上限。

## 支持范围

当前正式支持：

- macOS 13 或更高版本；
- Mihomo / Clash Meta 兼容的只读 `/connections` API；
- Clash Verge 服务模式在受控 `/tmp/verge` 目录下创建的 Socket；
- 通过 OpenSSH Host alias 访问的 Linux VPS；
- 可选 Xray StatsService 用户统计；
- OpenCode Desktop 本地会话库或兼容 CLI；
- Codex 本地状态库；
- macOS 公共的主机、虚拟内存、IOKit 磁盘、文件系统容量与温度接口；
- OpenAI、Claude 与 DeepSeek 的公开官方状态源；
- 发布兼容 Infra Protocol discovery offer 的
  [PCP](https://github.com/glenzli/paged-context-protocol) 与
  [Infer Runtime](https://github.com/glenzli/infer-runtime) 本地设施。

当前不支持：

- sing-box、Surge 或任意 TCP Controller；
- 非 Linux 远端网卡统计；
- 非 Xray 服务端用户统计；
- ChatGPT/Codex 订阅额度和通用 API 账户余额；
- 屏幕时间、提示词分析或全盘文件扫描。
- Windows 或 Linux 的正式桌面安装包。首批系统 backend 已存在，但原生通知、Mihomo Controller transport、设施 Named Pipe transport、安装器和目标平台实机验证仍待完成。

接口偶然兼容不等于正式支持。

## 安装与构建

项目不要求 Apple Developer 证书。预编译 App 使用 ad-hoc 签名、未经 Apple 公证；macOS 首次启动时可能需要右键选择“打开”，或在“系统设置 → 隐私与安全性”中确认。

从源码构建需要：

- macOS 13+ 与 Xcode Command Line Tools；
- Rust 工具链；
- Node.js LTS 与 npm；
- Python 3.11+ 与 PyInstaller。

```sh
git clone git@github.com:glenzli/infra-sentinel.git
cd infra-sentinel
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

构建脚本会按 `package-lock.json` 安装前端依赖，将 Python Agent 打包为当前 Mac 架构的 sidecar，生成 Tauri `.app` 和 DMG，并完成 ad-hoc 签名验证。

维护者生成 Release ZIP 与 SHA-256：

```sh
./bin/package-release.sh
```

## 配置

首次启动会创建默认配置。之后从 App 的“设置”页面管理告警和远端主机：

```text
~/Library/Application Support/Infra Sentinel/config.toml
```

本机 Mihomo 自动发现，无需填写地址。远端主机只填写 `~/.ssh/config` 中的 Host 别名；App 不保存私钥、密码或真实主机地址。

“本地集成”设置通常保持为空。若便携版或非标准安装位置无法自动发现，可以在这里覆盖 SSH、OpenCode 程序、OpenCode Desktop 数据库和 Codex 数据库的绝对路径；这对用户自行选择安装目录的 Windows 环境尤其有用。空值始终表示按平台自动发现；这些路径不会被当作命令参数，也不会触发递归扫描。

```sshconfig
Host edge-a
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

若启用 Xray 用户统计，需要为每个客户端设置唯一 `email` 标签、开启用户上下行统计，并让 StatsService 只监听远端回环地址。具体字段由 Xray 自己的配置格式决定；Infra Sentinel 不会自动修改服务端配置。

配置合同使用 `YYYYMMDD.修订号`。默认配置不包含任何真实主机或账户。

## 本地数据与隐私

运行数据保存在：

```text
~/Library/Application Support/Infra Sentinel/state/
```

其中包括 SQLite 指标库、计数器 checkpoint、健康状态、受限命令结果、滚动日志和必要的旧网络 JSONL。状态可能包含聚合域名、Xray 客户端标签、模型名和用户设置的显示名称，但不会保存：

- 提示词、响应、消息正文或命令内容；
- URL 路径、查询参数、请求头或网络载荷；
- 项目路径、文件内容、Git 元数据或任务标题；
- SSH 私钥、密码、API Key 或认证凭据；
- 抓包数据；
- 文件名、文件系统路径、进程参数、窗口标题或单文件 I/O 活动。

所有采集、存储和分析默认只发生在本机。上游状态观测只会向供应商公开状态源发出匿名、只读的 HTTPS 请求。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
cd ui && npm run build
cd src-tauri && cargo test --offline
```

架构演进与新增指标的进入标准见 [ROADMAP.md](ROADMAP.md)，发布记录见 [CHANGELOG.md](CHANGELOG.md)。

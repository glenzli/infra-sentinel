# Infra Sentinel

**中文** · [English](#english)

Infra Sentinel 是运行在本机的个人 AI 基础设施观测工具。它把网络、AI Token、本机资源、上游状态和已接入设施放到同一个只读面板中，但不把它们压成一个没有解释力的“总分”。不同资源保留各自的单位、来源与统计口径。

![Infra Sentinel 中文概览](assets/overview-zh.png)

![Infra Sentinel AI 用量](assets/ai-usage-zh.png)

> 截图使用固定的匿名演示数据，不包含真实主机名、SSH 别名、IP、账户或本机路径。

## 当前覆盖范围

- **网络**：本机 Mihomo 流量、域名与代理路径归因、Linux VPS 网卡账单量、Xray 用户逻辑流量。
- **AI 用量**：OpenCode、Codex 与 Infer Runtime 的本机 Token 记录；按 Agent、模型、日历史和速率查看。
- **本机系统**：CPU、内存压力与 Swap、磁盘吞吐和 IOPS、容量、温度压力，以及尽力而为的 App 磁盘 I/O 归因。
- **上游服务**：OpenAI、Claude、DeepSeek 官方状态页的低频只读摘要。
- **运行设施**：通过 Infra Protocol 自动发现的本地服务，例如 PCP、Infer Runtime 和 Dev Mesh Observer。

菜单栏只显示总体状态；详细指标、告警原因、时间序列和来源差异在 App 内查看。设施卡只展示有界的只读投影，深入诊断仍由设施自己的 Console 负责。

## 观测边界

Infra Sentinel 不抓包，也不读取提示词、响应正文、URL 路径、项目文件或任务标题；不会结束进程、删除文件、断开网络，或修改代理与服务端配置。

当来源不可用、窗口未对齐或计数无法可靠解释时，界面会显示未知、等待采样或来源异常；不会用推断值冒充账单。

## 网络

本机网络以 Mihomo / Clash Meta 兼容内核的累计计数为准：

- 自动发现当前用户可访问的 Mihomo Unix Socket；
- 每 5 秒形成一个持久化区间，区间内默认每 250 ms 读取活跃连接；
- 以相邻 `uploadTotal + downloadTotal` 的增量作为本机精确总量；
- 按站点主域及实际 `chains` 区分域名、代理、`DIRECT`、阻断和未知路径；
- 未能在轮询点之间捕获的短连接会保留为“未归因”，不会被静默丢弃。

因此始终满足：

```text
已归因域名 + 未归因 = Mihomo 精确总增量
代理 + DIRECT + 阻断 + 未知 + 未归因 = Mihomo 精确总增量
```

远端主机是可选的。每台 VPS 单独维护基线、计费方向、阈值和覆盖范围：

- 通过 `~/.ssh/config` 里的 Host 别名短时、只读地读取 Linux 主机；
- 从 `/sys/class/net` 选择公网网卡并读取 RX/TX 字节与包数；
- 可选读取仅监听远端 `127.0.0.1:10085` 的 Xray StatsService；
- 多台 VPS 的账单量可以汇总；链路倍率只在同一主机、同一覆盖范围内解释。

VPS 的网卡账单量与 Xray 用户逻辑量处在不同物理层，界面会并列展示，不会相加成一个数字。每台 VPS 可选 `RX + TX` 双向计费或仅 `TX` 计费。

## AI Token

AI 模块只读取客户端或本地设施已经保存的聚合元数据。每个来源输出统一的今日量、可读历史、模型维度和来源状态；这些数据用于本机趋势观察，不是 ChatGPT、Codex 或 API 供应商的正式账单。

### OpenCode

- 只读聚合 OpenCode Desktop assistant 消息中的 Token 元数据；
- 可显示输入、输出、推理、缓存、模型、消息数和供应商返回的成本；
- Desktop 数据库不可用时才尝试兼容的 `opencode stats --days 0 --models`；
- 以持久化 checkpoint 防止重启后重复写入同一天的增量。

### Codex

- 只读读取 `~/.codex/state_5.sqlite` 中的主任务 Token 计数、模型与派生拓扑；
- 今日量从 Sentinel 当天建立的本机基线开始计算；
- 对可识别主任务的正增量按模型归集，保持总量与模型合计一致；
- 只显示主任务、子 Agent、近期活动和派生层级，不读取任务标题或内容。

OpenCode 的自然日统计与 Codex 的本机基线窗口可能不同，界面会明确标识来源和覆盖范围。Codex App Server 产生的用量并非总能在本地任务库中形成可归属任务；来源不明时不会按模型猜测。

### Infer Runtime

- 读取已发现 Infer Runtime 设施提供的当前主机自然日、已结算的文本 Token 聚合；
- 只接受明确的 `execution_origin` 事实（`codex` 或 `other`），不从模型名或 Provider 名推断来源；
- 每次刷新覆盖当天的 Runtime 快照，而不是把轮询结果重复累加；
- 零 Token 构建行、音频和视觉等非文本工作负载不进入 Token 面板，仍在 Infer Runtime Console 中查看。

Runtime 聚合属于本地运行观测。未发布所需日聚合的 Runtime 仍会出现在设施列表中，但不会进入 Token 总量。

## 本机资源

系统 Collector 通过平台能力合同采样，不假设每个系统都提供相同计数。经过验证的 macOS backend 可读取整机 CPU、原生内存压力、压缩内存、Swap、磁盘容量、物理磁盘读写字节与 IOPS、保守的磁盘健康证据和温度压力。

按 App 的磁盘 I/O 基于进程累计计数，属于诊断信息：短命进程、系统服务或没有读取权限的进程可能遗漏，且不会与物理设备计数完全相等。同一个 `.app` 内的 helper 会归入父 App；只保存有界 App 标签及读写计数，不保存文件名、路径、参数、PID、窗口标题或用户内容。

高频样本先保留在活动 15 分钟桶的内存中；桶结束后一次性落盘。历史数据最近 7 天保留 15 分钟粒度，第 8 至 90 天压缩为小时粒度，更早压缩为按日粒度。磁盘健康在启动时读取，之后最多每 6 小时刷新一次。

告警只依赖可解释的压力信号：macOS 内存压力、磁盘可用空间低于 10% 或 5%，以及高/严重温度压力。CPU 和磁盘活动会绘图，但不会仅凭一次峰值升级为事件。

Linux 与 Windows backend 已保留跨平台接口：Linux 使用整机 procfs/sysfs 计数，Windows 使用稳定的 Win32 CPU、内存与磁盘容量接口。当前只有 macOS 完成桌面打包与发布验证；不可用能力会在界面中省略，不会显示为 0 或“正常”。

## 上游服务与本地设施

每 5 分钟读取一次 OpenAI、Claude 和 DeepSeek 的公开官方状态摘要，展示 API 组件、活动事件、官方更新时间和原始状态页链接。整个过程不需要 API Key，也不会发送合成模型请求。传输或解析失败会显示为未知；只有官方已经确认的 API 异常与恢复才会产生状态事件。

本地设施通过 [Infra Protocol](https://github.com/glenzli/infra-protocol) 的当前用户注册信息发现。注册信息只提供候选入口，不代表存活：Sentinel 不扫描端口、不按进程名猜测，只对精确协议版本和 binding 求交集，再由服务自身的只读请求确认。

已验证的设施接入：

- [Paged Context Protocol (PCP)](https://github.com/glenzli/paged-context-protocol)
- [Infer Runtime](https://github.com/glenzli/infer-runtime)
- [Dev Mesh Observer](https://github.com/glenzli/dev-mesh)

它们的应用协议彼此独立；Infra Protocol 只规定发现。Sentinel 只投影有界状态、指标、问题、观测时间和可选的本机 Console 链接。未知协议会被忽略。详细合同见[设施发现说明](docs/facility-discovery.md)。

## 架构与数据

```text
Mihomo / VPS / Xray / OpenCode / Codex / 系统 backend → Collectors → SQLite 指标 ┐
供应商官方状态源 → 低频状态观测器 ─────────────────────────────────┤
本地设施 → Infra Protocol discovery → Provider adapters ──────────┤
                                                                   ↓
                                                版本化 Projection + 命令
                                                                  ↓
                                                    Tauri UI / 通知
```

- Python Agent 是采样、计量、存储、策略与 Projection 生成的唯一所有者。
- SQLite WAL 以稳定身份键保存 counter，防止重启或回填重复计数。
- Tauri WebView 只读取版本化 Projection，并提交白名单命令；Rust bridge 不暴露任意文件、Shell 或 SQL 访问。
- 单个 Collector、设施或状态源失败不会阻塞其他资源采样。
- Projection、指标存储、策略与 Web UI 保持平台无关；平台差异限制在窄 adapter 中。

生产代码位于 `src/infra_sentinel`，按应用生命周期、平台无关 core、指标、平台 adapter 与资源族组织。`bin/` 只放可执行入口和构建/发布脚本。架构边界见[架构说明](docs/architecture.zh-CN.md)，后续方向见 [ROADMAP.md](ROADMAP.md)。

## 支持范围

当前正式支持：

- macOS 13 或更高版本；
- Mihomo / Clash Meta 兼容的只读 `/connections` API，以及 Clash Verge 服务模式在受控 `/tmp/verge` 创建的 Socket；
- 通过 OpenSSH Host alias 访问的 Linux VPS；
- 可选 Xray StatsService 用户统计；
- OpenCode Desktop 本地会话库或兼容 CLI；
- Codex 本地状态库；
- macOS 公共的主机、虚拟内存、IOKit 磁盘、文件系统容量与温度接口；
- OpenAI、Claude 与 DeepSeek 的公开官方状态源；
- 发布兼容 Infra Protocol discovery offer 的上述本地设施。

当前不支持 sing-box、Surge 或任意 TCP Controller；非 Linux 远端网卡统计；Xray 以外的服务端用户统计；ChatGPT/Codex 订阅额度或通用 API 账户余额；提示词分析、屏幕时间或全盘文件扫描。Windows/Linux 的首批系统 backend 已存在，但尚未提供正式桌面安装包。

## 安装与构建

项目不要求 Apple Developer 证书。预编译 App 使用 ad-hoc 签名，未经过 Apple 公证；首次启动时，macOS 可能要求右键选择“打开”，或在“系统设置 → 隐私与安全性”中确认。

从源码构建需要 macOS 13+、Xcode Command Line Tools、Rust、Node.js LTS、npm、Python 3.11+ 与 PyInstaller：

```sh
git clone git@github.com:glenzli/infra-sentinel.git
cd infra-sentinel
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

构建脚本按 `package-lock.json` 安装前端依赖，打包当前 Mac 架构的 Python Agent sidecar，生成 Tauri `.app` 和 DMG，并验证 ad-hoc 签名。维护者可用 `./bin/package-release.sh` 生成 Release ZIP 与 SHA-256。

### 配置

首次启动会创建默认配置：

```text
~/Library/Application Support/Infra Sentinel/config.toml
```

Mihomo 在本机自动发现，无需填写地址。远端主机只填写已定义在 `~/.ssh/config` 中的 Host 别名；App 不保存私钥、密码或真实地址。

“本地集成”通常保持为空。只有自动发现无法覆盖便携版或非标准安装位置时，才填写 SSH、OpenCode 程序、OpenCode Desktop 数据库或 Codex 数据库的绝对路径。空值始终表示平台自动发现；这些路径不会作为命令参数，也不会触发递归扫描。

```sshconfig
Host edge-a
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

Xray 用户统计要求每个客户端使用唯一 `email` 标签，并启用用户上下行统计；StatsService 只应监听远端回环地址。字段格式由 Xray 决定，Infra Sentinel 不会修改服务端配置。配置合同使用 `YYYYMMDD.修订号`，默认配置不包含真实主机或账户。

### 文档截图

仓库截图来自固定的匿名 Projection，不是运行中的监控实例：

```sh
./scripts/capture-demo-screenshots.sh
# English: INFRA_SENTINEL_DEMO_LOCALE=en ./scripts/capture-demo-screenshots.sh
```

脚本创建临时状态目录，在其中写入静态 Projection，并以禁用采集的方式启动已构建 App。它不会启动 Agent，也不会连接 Mihomo、VPS、设施、上游 Provider 或本机用量数据库。关闭演示 App 后，临时数据会被删除。

## 本地数据与隐私

运行状态保存在：

```text
~/Library/Application Support/Infra Sentinel/state/
```

其中包括 SQLite 指标库、低频 counter checkpoint、健康状态、临时命令结果、滚动日志和受限保留的旧网络 JSONL。状态可能包含聚合域名、Xray 客户端标签、模型名和用户设定的显示名称；不会保存：

- 提示词、响应、消息正文或命令内容；
- URL 路径、查询参数、请求头或网络载荷；
- 项目路径、文件内容、Git 元数据或任务标题；
- SSH 私钥、密码、API Key 或认证凭据；
- 抓包数据、文件名、文件系统路径、进程参数、窗口标题或单文件 I/O 活动。

默认情况下，采集、存储和分析都在本机完成。上游状态观测只向供应商的公共状态源发出匿名、只读 HTTPS 请求。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
cd ui && npm run build
cd src-tauri && cargo test --offline
```

---

## English

[中文](#infra-sentinel)

Infra Sentinel is a local, read-only observability dashboard for personal AI infrastructure. It keeps network traffic, local AI Token records, host-resource pressure, public provider status, and compatible local facilities in separate, traceable measurement domains.

![Infra Sentinel overview](assets/overview-en.png)

![Infra Sentinel AI usage](assets/ai-usage-en.png)

It observes Mihomo/Clash Meta traffic, optional Linux VPS interface counters and Xray user counters, OpenCode/Codex/Infer Runtime Token metadata, macOS host resources, official OpenAI/Claude/DeepSeek status feeds, and facilities discovered through [Infra Protocol](https://github.com/glenzli/infra-protocol). PCP, Infer Runtime, and Dev Mesh Observer are verified integrations.

The application never captures packets or reads prompts, responses, URL paths, project files, task titles, credentials, or private keys. It does not terminate processes, modify proxy/server configuration, disconnect the network, or execute arbitrary shell commands.

The packaged desktop target is currently macOS 13+. Build it with:

```sh
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

The first launch creates `~/Library/Application Support/Infra Sentinel/config.toml`. Remote hosts are referenced only by an existing `~/.ssh/config` Host alias. The project uses ad-hoc signing and is not notarized; macOS may require **Open** from the context menu on first launch.

Documentation screenshots use a fixed anonymous Projection:

```sh
INFRA_SENTINEL_DEMO_LOCALE=en ./scripts/capture-demo-screenshots.sh
```

The demo starts no collector and contacts no local or remote service. For the full implementation boundary, configuration notes, privacy rules, and validation commands, use the Chinese documentation above.

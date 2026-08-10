# Infra Sentinel

[English](README.md) | **中文**

Infra Sentinel 是一个本地优先的个人 AI Infra 可观测面板。它目前覆盖两类可计量资源，以及参与接入的本地设施健康状态：

- **网络**：本机 Mihomo 流量、域名与代理路径归因、Linux VPS 账单量、Xray 用户逻辑流量；
- **AI 用量**：OpenCode 与 Codex 的本地 Token 记录、模型构成、消耗速率和 Agent 活动；
- **本地设施**：自动发现兼容的 runtime 与服务，按各自协议投影受限健康状态。

它还会通过 [Infra Protocol](https://github.com/glenzli/infra-protocol) 的发现合同自动发现参与接入的本地基础设施。每个设施都作为独立资源卡展示受限的只读详情；设施专属的诊断与操作仍留在原生 Console，并由系统浏览器打开。

菜单栏只表达整体健康状态。详细数据、趋势、来源差异和告警都在 App 中查看。

它不会抓包，不读取提示词、响应正文、URL 路径或项目文件，也不会杀进程、删除文件、断网或修改代理配置。

![Infra Sentinel Network 与 AI 资源模块](assets/overview-zh.png)

![Infra Sentinel AI 用量](assets/ai-usage-zh.png)

> 截图使用匿名演示数据生成，不包含真实主机名、SSH 别名、IP、账户或本机路径。

## 它回答什么

Infra Sentinel 不把字节、Token 和告警硬凑成一个“总分”。每个资源模块保留自己的单位和统计口径，用来回答不同问题：

- 今天哪一类资源消耗最多？
- 消耗来自哪个 Agent、模型、域名、代理路径或 VPS？
- 当前增长速度是否异常？
- 本机观测值、代理逻辑量和 VPS 账单量为什么不同？
- 某个数据源是否失联，统计窗口是否完整？
- 哪些本地基础设施正常或降级，它的原生 Console 在哪里？

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

## 分析视图

Network 和 AI 用量都使用同一套观测结构：

- 固定摘要：今日观测、历史总量或账单量、采集覆盖；
- 时间范围：今日、近 7 天、近 30 天、全部历史；
- 构成分析：按 Agent、模型、域名、路径或 VPS 展开；
- 速率趋势：按真实采样时间计算，不把补记流量伪装成实时峰值；
- 每日历史：堆叠柱状图保留总量与组成关系。

历史查询通过独立只读通道执行，不等待 5 秒网络采样周期；UI 只消费受限查询结果，不接触 SQLite 或任意文件。

## 本地设施发现

接入服务发布短时、仅当前用户可读的
[`infra.discovery.registration@20260810.1`](https://github.com/glenzli/infra-protocol) 租约。
Sentinel 不扫描端口、不按进程名猜测，只对具体协议版本和 binding 做精确交集，然后连接所选
服务。Discovery 不携带指标、Console URL、通用请求信封，也不会赋予启停、配置或维护权限。

当前已验证的接入包括
[Paged Context Protocol (PCP)](https://github.com/glenzli/paged-context-protocol) 的
`pcp.runtime.observer@20260810.1`，以及
[Infer Runtime](https://github.com/glenzli/infer-runtime) 的
`infer-runtime.status@20260810.1`。两套应用协议仍彼此独立；Infra Protocol 只统一发现。
Sentinel 为两者分别实现 adapter，只把有界状态、指标、问题、观测时间与可选的本机
**打开 Console** 链接投影到 UI。每个被发现的设施都是独立的一级模块与详情页。
未知协议会被忽略，不会被猜测为兼容。详见[设施发现](docs/facility-discovery.md)。

## 架构

```text
Mihomo / VPS / Xray / OpenCode / Codex → Collectors → SQLite 指标 ┐
本地设施 → Infra Protocol discovery → Provider adapters ─────────┤
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

当前本地 Projection 协议版本为 `20260810.1`，指标查询支持分钟、5 分钟、小时和天级聚合，查询范围和结果点数均有上限。

## 支持范围

当前正式支持：

- macOS 13 或更高版本；
- Mihomo / Clash Meta 兼容的只读 `/connections` API；
- Clash Verge 服务模式在受控 `/tmp/verge` 目录下创建的 Socket；
- 通过 OpenSSH Host alias 访问的 Linux VPS；
- 可选 Xray StatsService 用户统计；
- OpenCode Desktop 本地会话库或兼容 CLI；
- Codex 本地状态库；
- 发布兼容 Infra Protocol discovery offer 的
  [PCP](https://github.com/glenzli/paged-context-protocol) 与
  [Infer Runtime](https://github.com/glenzli/infer-runtime) 本地设施。

当前不支持：

- sing-box、Surge 或任意 TCP Controller；
- 非 Linux 远端网卡统计；
- 非 Xray 服务端用户统计；
- ChatGPT/Codex 订阅额度和通用 API 账户余额；
- 屏幕时间、提示词分析或全盘文件扫描。

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

```sshconfig
Host edge-a
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

若启用 Xray 用户统计，需要为每个客户端设置唯一 `email` 标签、开启用户上下行统计，并让 StatsService 只监听远端回环地址。具体字段由 Xray 自己的配置格式决定；Infra Sentinel 不会自动修改服务端配置。

配置合同使用 `YYYYMMDD.修订号`，当前版本为 `20260808.4`。默认配置不包含任何真实主机或账户。

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
- 抓包数据。

所有采集、存储和分析默认只发生在本机。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
cd ui && npm run build
cd src-tauri && cargo test --offline
```

架构演进与新增指标的进入标准见 [ROADMAP.md](ROADMAP.md)，发布记录见 [CHANGELOG.md](CHANGELOG.md)。

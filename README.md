# Infra Sentinel

Infra Sentinel 是一个本地优先的个人 AI Infra 资源归因面板。当前正式启用的资源模块包括只读网络监控，以及在检测到 OpenCode 或 Codex 后自动启用的本地 AI 用量统计。网络模块服务于一条明确的代理技术栈：

- 本地使用 Mihomo / Clash Meta 兼容内核；
- 可选通过 SSH 校验一台或多台独立 Linux VPS 网卡流量；
- 可选读取 Xray StatsService 的用户逻辑流量。

它按域名和实际代理链归因本机流量，并把 Xray 逻辑流量与 VPS 账单流量放到同一个统计周期中比较。

它不会抓包、读取请求内容或提示词、记录 URL 路径、终止进程、删除业务文件或断网。

![Infra Sentinel network dashboard](assets/dashboard-en.png)

> 图中的设备标签来自使用者自己的 Xray 配置，不是程序内置名称。

API 账户额度、磁盘和本地计算尚未接入，不会被伪装成可用功能。架构迁移计划见 [ROADMAP.md](ROADMAP.md)。

## 支持范围

当前正式支持：

- macOS；
- 当前用户拥有的 Mihomo Unix Socket；
- Clash Verge 服务模式在受控 `/tmp/verge` 目录下创建的 root-owned Socket；
- 兼容的只读 `/connections` API；
- 通过 `~/.ssh/config` 主机别名访问的 Linux VPS；
- Linux `/sys/class/net` 网卡累计计数；
- 仅监听远端 `127.0.0.1:10085` 的 Xray StatsService。
- OpenCode Desktop 的本地会话库，或其自动发现的 CLI。
- Codex 的本地状态库（自动发现，仅聚合工作负载元数据）。

桌面端使用 Tauri，并把 Python Agent 按目标平台打包为本地 sidecar。桌面端只
能读取版本化 Projection 和提交受限命令，无法读取任意文件或运行任意命令；
Agent 仍是采样、存储、策略和通知的唯一所有者。首次启动会创建默认配置，设置
保存后会由桌面端自动重启 Agent。

当前不支持：

- 任意 TCP Controller；
- sing-box、Surge 或其他代理核心；
- 非 Linux 远端网卡统计；
- 非 Xray 服务端用户统计；
- 其他 Agent、ChatGPT/Codex 订阅额度或任意 API 账户额度；
- 自定义 Xray API 地址、二进制路径或 VPS 网卡选择。

接口偶然兼容不等于正式支持。项目只为上面的路径维护实现和测试。

## 统计口径

App 自动发现本地 Mihomo Socket，并读取 `/connections`：

- `Mihomo 本机总量`：`uploadTotal + downloadTotal` 的相邻增量；
- `域名流量归因`：持续跟踪活跃连接 ID，并按站点主域聚合；
- `代理路径`：根据连接 `chains` 区分代理、`DIRECT`、阻断和未知路径；
- `未归因`：总增量减去已观察连接增量，通常来自两个轮询点之间结束的短连接。

始终满足：

```text
所有域名分类 + 未归因 = Mihomo 精确总增量
代理路径 + DIRECT + 阻断 + 未知路径 + 未归因 = Mihomo 精确总增量
```

每个 5 秒展示周期内，本地 Socket 默认每 250ms 读取一次，以提高多 Agent 短连接的归因覆盖。这些读取只发生在本机，不产生外网流量。单次响应上限为 64 MiB。

## 本地数据存储

从 `20260808.2` 起，App 在状态目录创建 `infra.sqlite3`，使用 SQLite WAL 保存标准化的网络与 AI 用量区间指标。写入通过稳定来源、时间、指标和维度生成去重键，因此重启或回填不会重复计数。

首次运行会从既有网络 JSONL 一次性回填；原始 JSONL 目前仍保留给会话恢复、证据快照和旧报告，下一次存储迁移会在查询路径完全切换后再停止该依赖。数据库保存累计字节、方向、代理路径、来源身份、必要的 Xray 客户端标签，以及稳定的服务标签（如 `ChatGPT` 或归并后的站点标签）；不保存完整主机名、URL、连接 ID、请求内容或提示词。

进入存储前，事实会经过独立 Collector 注册表：本机 Mihomo、每台 VPS 网卡、每台 VPS 的 Xray 统计、OpenCode 与 Codex 分别输出标准指标。某一个指标适配器出错只会标记对应数据源异常，其余来源仍会继续写入；Collector 不保存或展示请求内容。

## OpenCode AI 用量

检测到 OpenCode Desktop 后，Agent 每 60 秒以 SQLite 只读模式聚合当天
assistant 消息的 provider、model、时间、token 与 cost 元数据；不会选择消息
正文、提示词、响应、项目路径、账户行或认证凭据。Desktop 库不存在时，才回退执行：

```text
opencode stats --days 0 --models
```

Desktop 路径提供当天逐 assistant 消息的精确输入、输出、推理和缓存 Token；它不会把不同模型混在一个 session 总数里。CLI 后备只解析会话聚合 token、模型、缓存、消息数和 OpenCode 报告的费用；当前 CLI 的模型明细会把 reasoning 合入输出，因此界面会明确标为“输出 + 推理”。

OpenCode 未安装时，此资源模块不会出现在界面中，也不会造成数据源异常。若其 CLI 输出格式发生不兼容变化，模块会标记为数据源异常而不会写入猜测值。

## Codex 本地工作负载

检测到 `~/.codex/state_5.sqlite` 后，Agent 每 20 秒以 SQLite 只读模式聚合 Codex 的线程
`tokens_used`、模型、更新时间与 SQL 派生的子 Agent 拓扑。界面会展示本地累计 Token、模型分布、任务/子 Agent 总数、最近 10 分钟活跃子 Agent 与最大派生层级。它会在每天首次采样时保存本地基线，以后显示当天的 Token 增量；若当天中途首次启用，则该日从启用时刻开始计。

这不是 API 账单，也没有输入、输出、缓存或推理 Token 分项；`tokens_used` 是 Codex 的本地线程累计计数。AI 模块另提供“粗略本地汇总”：将 OpenCode 的今日精确会话 Token 与 Codex 自当日基线起的增量相加，并将可得来源的历史记录相加。它用于趋势判断，绝不等同于供应商账单；来源明细仍会分开显示。

## 分析视图

网络与 AI 用量详情均提供三个时间尺度：

- **今日**：当前本地日期内的累计分布；
- **记录累计**：Sentinel 可读取和保留的本地历史，而不是设备或供应商生命周期；
- **按日**：近 30 天内，每天一根柱的已记录增量。

网络分布只使用本机 Mihomo 已归因服务流量；VPS 账单量属于另一物理计费层，继续独立展示，绝不并入域名柱状图。AI 模型以标识符为主键；当前仅会把 OpenCode 的 `openai/<model>` 与 Codex 的同名 OpenAI 模型合并，其他带提供商前缀的名称保持独立，不做猜测性映射。模型总量视图至多展示前五个模型，并附带该集合内的占比环图。Token 速率图使用每个五分钟桶内真实写入的 `ai.tokens.total` 增量换算为每分钟 Token，不使用生命周期累计值。

查询绝不选择或保存任务标题、提示词、响应、预览、项目路径、Git 元数据、Agent 名称或 Agent 路径。若后续 Codex 版本改变或移除此状态库，来源会自动隐藏或标记异常，不影响网络与其他 AI 来源。

## 本地 Agent 协议

`infra_agent.py` 是采样、存储、Projection 与本地命令的唯一运行时。桌面 UI 只负责启动 Agent、读取状态和提交命令；它不参与流量计算。

当前协议版本为 `20260809.2`，以本地原子文件作为第一种传输方式：

- `state/projection.json`：完整只读 Projection，供任意 UI 读取；
- `state/health.json`：采样失败时的独立运行时健康状态；
- `state/commands/<uuid>.request.json`：UI 提交的命令；
- `state/commands/<uuid>.result.json`：Agent 执行结果。

当前正式命令为 `session.reset`、只读的 `metrics.query`，以及
`configuration.get` / `configuration.update`。后者仍由 Python 配置所有者
校验和原子写入；成功后 Agent 会请求受监督的重启，使新配置成为新的运行时。
查询只能按时间、资源、来源与指标读取 counter，并限定为分钟/小时级最多 90 天、按日最多两年，以及最多 10,000 个结果点；UI 无法执行 SQL 或写入指标。命令与 Projection 均不包含密钥、提示词、请求正文、URL 路径或网络载荷。未来若替换为仅回环的 IPC/HTTP，保留相同 JSON 合同即可，Collector、存储和 UI 业务语义无需改动。

内置的宽泛服务标签包括：

- OpenAI / ChatGPT 相关主域 → `ChatGPT`
- Google 相关主域 → `Google`
- GitHub 相关主域 → `GitHub`
- 其他域名按站点主域动态显示

Google 流量不会被推断为某个具体客户端。未知域名也会直接显示，不需要维护规则。

## 设置

从菜单栏或仪表板点击“设置”即可编辑全部用户配置。保存后，监控子进程会自动按新配置重新启动。

设置项只有：

- 警告窗口与单方向流量阈值；
- 严重窗口与上下行合计阈值；
- 每台远端 VPS 的启用状态、显示名称、`~/.ssh/config` 主机别名；
- 每台 VPS 是否读取 Xray 用户逻辑流量；
- 每台 VPS 的计费周期开始日，以及收发均计费（2.0×）或仅出站计费（1.0×）。
- 每台 VPS 独立的账单周期预算：警告与严重阈值均以 GiB 输入，按该 VPS 的计费方向计算。

以下行为固定，不进入配置：

- 本地展示周期 5 秒；
- 本地连接读取间隔 250ms；
- VPS 与 Xray 远端读取间隔 5 分钟；
- VPS 网卡自动发现；
- Xray StatsService 地址 `127.0.0.1:10085`；
- Xray 二进制路径 `/usr/local/bin/xray`；
- 日志单文件 10 MiB、保留 5 份归档。

配置文件由设置界面管理：

```text
~/Library/Application Support/Infra Sentinel/config.toml
```

当前配置契约为 `20260808.4`。版本采用 `YYYYMMDD.修订号`：同一天内迭代修订号，跨日发布使用新的日期。

```toml
schema_version = "20260808.4"

[app]
menu_bar_mode = "health"

[[policies]]
id = "network-traffic-alerts"
kind = "traffic.threshold"
resource_id = "network"
warning_window_minutes = 5
warning_mib = 250
critical_window_minutes = 10
critical_mib = 1024

[[sources]]
id = "local-mihomo"
kind = "network.mihomo"
enabled = true

[[sources]]
id = "primary"
kind = "network.linux-xray"
label = "Primary VPS"
enabled = true
ssh_host = "my-vps"
xray_stats_enabled = true
billing_mode = "both"

[[policies]]
id = "primary-daily-usage"
kind = "network.daily.usage"
source_id = "primary"
warning_gib = 600
critical_gib = 800
```

`local-mihomo` 是固定启用的本机数据源；远端 `network.linux-xray` 数据源可以有零个或多个。每个远端 `id` 对应一套独立的 VPS 网卡计数、Xray 计数和本地基线。仪表板顶部显示所有启用 VPS 的合计，远端明细区按名称分别列出各路账单量。

首次读取旧版 `[monitor]` / `[remote]` 或上一日期配置时，App 会在同目录写入日期化备份，然后原子改写为当前结构；迁移完成后不维护旧格式分支。

## VPS 与 Xray 计量校验

先在 `~/.ssh/config` 定义每台主机，再把 `Host` 后的别名填入对应的设置行；默认不预设任何服务器：

```sshconfig
Host my-vps
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519

Host my-vps-2
  HostName vps2.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

App 只保存这些别名和显示名称，不保存密钥、密码或主机地址。每台服务器通过短时、只读 SSH 采样访问，不维持长期 SSH 链接。

VPS 读取：

```text
/sys/class/net/<interface>/statistics/rx_bytes
/sys/class/net/<interface>/statistics/tx_bytes
/sys/class/net/<interface>/statistics/rx_packets
/sys/class/net/<interface>/statistics/tx_packets
```

Xray 分端统计只需：

1. 给每个 `clients[]` 项设置唯一的 `email` 标签和 `level: 0`；
2. 启用用户上下行统计及只监听回环地址的 StatsService；
3. 重启 Xray，并在设置页勾选 Xray 统计。

```json
{
  "inbounds": [{
    "settings": {
      "clients": [
        {"id": "<uuid-1>", "email": "mac", "level": 0},
        {"id": "<uuid-2>", "email": "phone", "level": 0}
      ]
    }
  }],
  "stats": {},
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    }
  },
  "api": {
    "tag": "api",
    "listen": "127.0.0.1:10085",
    "services": ["StatsService"]
  }
}
```

收发均计费时：

```text
VPS 账单量     = RX + TX
理想账单量     = Xray 用户逻辑流量 × 2
单机观测倍率   = VPS 账单量 ÷ Xray 用户逻辑流量
账单附加量     = max(0, VPS 账单量 − 理想账单量)
```

仅出站计费时，VPS 账单量使用 `TX`，理想倍率为 `1×`。

倍率只在同一台主机内解释；多台 VPS 的总账单可以相加，但不同路由、网卡覆盖范围和 Xray 用户范围下的倍率不能合并。双向计费的单机观测倍率低于 `2×` 时，说明所选网卡没有覆盖完整链路，或 Xray 用户统计范围不同；此时工具会标记“链路覆盖不完整”，不把差额解释为协议开销。其余账单差额可能包含 IP/TCP 头与 ACK、连接建立、REALITY/Vision 填充、重传和少量 VPS 背景流量。包数拆分是不抓包条件下的近似解释，不是精确丢包率。

## 菜单栏、仪表板与告警

菜单栏是健康状态入口，而不是持续滚动的指标面板：

```text
⌁  正常
⚠︎  需要关注、采样异常或数据源异常
⛔  严重告警
```

- 点击菜单栏即可打开完整仪表板；完整数值、网络归因和趋势只在仪表板中展示。
- 仪表板顶部先展示整体健康状态、正式资源模块和数据源数量；检测到 OpenCode 或 Codex 时会附加 AI 用量模块。
- Network 详情继续显示 VPS 账单、本机 Mihomo 总量、代理路径、域名归因、Xray 用户统计和最近 1 小时的 `MiB/min` 趋势。

仪表板显示 VPS 账单、本机 Mihomo 总量、代理路径、域名归因、Xray 用户统计和最近 1 小时的 `MiB/min` 趋势。界面支持中文和 English 即时切换。

默认告警：

- 5 分钟单方向超过 250 MiB：警告；
- 10 分钟上下行合计超过 1 GiB：严重。

启用某台 VPS 的“预算”后，它不会与本机 5/10 分钟流量告警混算：当前计费周期的 VPS 可计费字节达到该行的警告或严重 GiB 阈值时，单独产生带 VPS 名称的通知。收发均计费按 `RX + TX`，仅出站计费按 `TX`。

App 或采样器中断后的累计差额仍进入本周期总量，但标记为补记，不进入实时告警和速率趋势。点击通知会打开仪表板。

## 隐私

证据快照和状态文件只保存累计字节、聚合域名、代理路径、覆盖率和远端计数。不会保存：

- URL 路径或查询参数；
- 请求头、请求正文或响应正文；
- 提示词、命令或文件内容；
- 抓包数据。

## 构建与开发

项目不要求 Apple Developer 证书。

预编译 App 要求：

- Release 文件名所标注的 CPU 架构；首个版本提供 Apple Silicon (`arm64`)；
- macOS 13 或更高版本；
- Python 3.11 或更高版本，推荐通过 Homebrew 安装。

预编译 App 使用 ad-hoc 签名、未经 Apple 公证。macOS 如阻止首次启动，可右键选择“打开”，或在“系统设置 → 隐私与安全性”中确认。

从源码构建还需要 Xcode Command Line Tools：

```sh
git clone git@gitlab.com:glenzli/net-traffic-sential.git
cd net-traffic-sential
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

构建脚本会安装锁定的前端依赖，按当前 Mac 架构打包 Agent sidecar，并生成 ad-hoc 签名的 App。它不需要 Apple Developer 账号。

维护者生成 Release 附件：

```sh
./bin/package-release.sh
```

产物写入忽略版本控制的 `dist/`，文件名包含版本、最低 macOS 和 CPU 架构，并同时生成 SHA-256 校验文件。

运行测试：

```sh
python3 -m unittest discover -s tests -v
cd ui/src-tauri && cargo test --offline
cd ../.. && cd ui && npm run build
```

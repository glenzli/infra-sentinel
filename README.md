# Infra Sentinel

Infra Sentinel 是一个本地优先的个人 AI Infra 资源归因面板。当前正式启用的第一个资源模块是只读网络监控，服务于一条明确的代理技术栈：

- 本地使用 Mihomo / Clash Meta 兼容内核；
- 可选通过 SSH 对账一台或多台独立 Linux VPS 网卡流量；
- 可选读取 Xray StatsService 的用户逻辑流量。

它按域名和实际代理链归因本机流量，并把 Xray 逻辑流量与 VPS 账单流量放到同一个统计周期中比较。

它不会抓包、读取请求内容或提示词、记录 URL 路径、终止进程、删除业务文件或断网。

![Infra Sentinel network dashboard](assets/dashboard-en.png)

> 图中的设备标签来自使用者自己的 Xray 配置，不是程序内置名称。

网络以外的能力尚未接入；API 额度、磁盘和本地计算不会被伪装成可用功能。架构迁移计划见 [ROADMAP.md](ROADMAP.md)。

## 支持范围

当前正式支持：

- macOS；
- 当前用户拥有的 Mihomo Unix Socket；
- Clash Verge 服务模式在受控 `/tmp/verge` 目录下创建的 root-owned Socket；
- 兼容的只读 `/connections` API；
- 通过 `~/.ssh/config` 主机别名访问的 Linux VPS；
- Linux `/sys/class/net` 网卡累计计数；
- 仅监听远端 `127.0.0.1:10085` 的 Xray StatsService。

当前不支持：

- 任意 TCP Controller；
- sing-box、Surge 或其他代理核心；
- 非 Linux 远端网卡统计；
- 非 Xray 服务端用户统计；
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

从 `20260808.2` 起，App 在状态目录创建 `infra.sqlite3`，使用 SQLite WAL 保存标准化的网络区间指标。写入通过稳定来源、时间、指标和维度生成去重键，因此重启或回填不会重复计数。

首次运行会从既有网络 JSONL 一次性回填；原始 JSONL 目前仍保留给会话恢复、证据快照和旧报告，下一次存储迁移会在查询路径完全切换后再停止该依赖。数据库只保存累计字节、方向、代理路径、来源身份和必要的 Xray 客户端标签，不保存域名原文、URL、请求内容或提示词。

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

当前配置契约为 `20260808.3`。版本采用 `YYYYMMDD.修订号`：同一天内迭代修订号，跨日发布使用新的日期。

```toml
schema_version = "20260808.3"

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
billing_cycle_start_day = 1
billing_mode = "both"

[[policies]]
id = "primary-billing-budget"
kind = "network.billing.budget"
source_id = "primary"
warning_gib = 600
critical_gib = 800
```

`local-mihomo` 是固定启用的本机数据源；远端 `network.linux-xray` 数据源可以有零个或多个。每个远端 `id` 对应一套独立的 VPS 网卡计数、Xray 计数和本地基线。仪表板顶部显示所有启用 VPS 的合计，远端明细区按名称分别列出各路账单量。

首次读取旧版 `[monitor]` / `[remote]` 或上一日期配置时，App 会在同目录写入 `config.pre-20260808.3.toml` 备份，然后原子改写为当前结构；迁移完成后不维护旧格式分支。

## VPS 与 Xray 对账

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
实测账单倍率   = VPS 账单量 ÷ Xray 用户逻辑流量
账单附加量     = max(0, VPS 账单量 − 理想账单量)
```

仅出站计费时，VPS 账单量使用 `TX`，理想倍率为 `1×`。

账单附加量可能包含两条链路的 IP/TCP 头与 ACK、连接建立、REALITY/Vision 填充、重传和少量 VPS 背景流量。包数拆分是不抓包条件下的近似解释，不是精确丢包率。

## 菜单栏、仪表板与告警

菜单栏是健康状态入口，而不是持续滚动的指标面板：

```text
⌁  正常
⚠︎  需要关注、采样异常或数据源异常
⛔  严重告警
```

- 点击菜单栏即可打开完整仪表板；完整数值、网络归因和趋势只在仪表板中展示。
- 仪表板顶部先展示整体健康状态、正式资源模块和数据源数量；当前唯一正式资源模块为 Network。
- Network 详情继续显示 VPS 账单、本机 Mihomo 总量、代理路径、域名归因、Xray 用户统计和最近 15 分钟的 `MiB/min` 趋势。

仪表板显示 VPS 账单、本机 Mihomo 总量、代理路径、域名归因、Xray 用户统计和最近 15 分钟的 `MiB/min` 趋势。界面支持中文和 English 即时切换。

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
./bin/build-menubar-app.sh
open "Infra Sentinel.app"
```

构建脚本会生成适配当前 Mac 架构的 `Infra Sentinel.app`，复制 Python 模块并执行本机 ad-hoc 签名。

维护者生成 Release 附件：

```sh
./bin/package-release.sh
```

产物写入忽略版本控制的 `dist/`，文件名包含版本、最低 macOS 和 CPU 架构，并同时生成 SHA-256 校验文件。

运行测试：

```sh
python3 -m unittest discover -s tests -v
./bin/build-menubar-app.sh
```

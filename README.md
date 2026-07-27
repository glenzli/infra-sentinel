# Net Traffic Sentinal

一个只读的 macOS Mihomo / Clash Verge 流量分析工具。它直接读取代理内核的累计计数，按域名和实际代理链归因，并可选地与 Xray 用户逻辑流量、VPS 双向账单流量对账。

它不会抓包、读取请求内容或提示词、记录 URL 路径、终止进程、删除业务文件或断网。

## 核心口径

本机不再使用 `nettop`，也不依赖 Codex、Antigravity 或其他应用进程名。App 会自动发现 Clash Verge 启动的本机 Unix Socket，并读取 Mihomo `/connections`：

- `Mihomo 本机总量`：`uploadTotal + downloadTotal` 的相邻增量，是代理内核处理的精确累计；
- `域名流量归因`：持续跟踪活跃连接 ID，将连接字节按域名聚合；
- `代理路径`：根据连接 `chains` 区分真正经过代理的流量、`DIRECT`、阻断和未知路径；
- `未归因`：精确总增量减去已跟踪连接增量，主要来自在两次本地轮询之间结束的短连接。

始终满足：

```text
所有域名分类 + 未归因 = Mihomo 精确总增量
代理路径 + DIRECT + 阻断 + 未知路径 + 未归因 = Mihomo 精确总增量
```

域名归因不会把缺失字节按比例硬塞给某个服务。已识别代理路径因此是一个可靠下限；未归因越小，分类越接近精确值。App 在每个 5 秒展示周期内以 250ms 间隔读取本机 Socket，改善多 Agent 短连接的覆盖；这些读取只发生在本机，不产生外网流量。

内置的宽泛服务标签包括：

- `chatgpt.com`、`openai.com`、`oaistatic.com`、`oaiusercontent.com` → `ChatGPT`
- Google 相关主域 → `Google`
- GitHub 相关主域 → `GitHub`
- 其他域名按站点主域动态显示

Google 流量不会被声称为 Antigravity，因为单凭域名无法证明具体客户端。未知域名仍会按域名显示，不需要用户维护规则。

## 构建与运行

仓库只包含源码，不提交预编译 App。

要求：

- macOS；
- Xcode Command Line Tools；
- Python 3.11 或更高版本。

```sh
git clone git@gitlab.com:glenzli/net-traffic-sential.git
cd net-traffic-sential
./bin/build-menubar-app.sh
open "Traffic Sentinel.app"
```

构建脚本会编译原生 Cocoa 菜单栏程序、复制运行所需的 Python 模块、执行本机 ad-hoc 签名，并在仓库根目录生成 `Traffic Sentinel.app`。App 自行管理唯一的采样进程，不需要手动启动脚本。

## 菜单栏与仪表板

菜单栏格式类似：

```text
⌁ T2.9 GiB · ChatGPT 8.4 MiB/s
```

- 启用 VPS 时，`T` 是当前统计周期的 VPS 入站 + 出站账单量；
- 未启用 VPS 时，`T` 是当前统计周期的 Mihomo 本机精确总量；
- 后半部分是当前区间最大的域名服务及速率；
- 点击“重置统计”并在确认对话框中再次确认后，Mihomo、Xray 和 VPS 从同一个新基线开始。

仪表板显示：

1. VPS 当前账单量；
2. Mihomo 本机精确总量；
3. 已识别代理路径下限；
4. Top 3 域名服务和其他域名；
5. 域名归因覆盖率、未归因量和 `DIRECT` 量；
6. Xray 用户逻辑流量；
7. 最近 15 分钟的 `MiB/min` 趋势。

界面支持中文和 English 即时切换。

## VPS 与 Xray 对账

本机 Mihomo 无需配置。只有可选的远端观察需要编辑：

```text
~/Library/Application Support/Codex Traffic Sentinel/config.toml
```

这是为了兼容旧版安装而保留的支持目录名称。

```toml
[monitor]
sample_seconds = 5
warning_window_seconds = 300
warning_bytes = 268435456
critical_window_seconds = 600
critical_bytes = 1073741824

[vps]
enabled = true
ssh_host = "my-vps"
interface = "auto"
poll_seconds = 300
billing_cycle_start_day = 1

[xray_stats]
enabled = true
ssh_host = "" # 留空时复用 [vps].ssh_host
api_server = "127.0.0.1:10085"
binary_path = "/usr/local/bin/xray"
poll_seconds = 300
users = ["mac", "android", "pc", "legacy-unknown"]
flagged_users = ["legacy-unknown"]

[estimation]
vps_billing_legs = 2.0

[state]
max_log_bytes = 10485760
backups = 5
```

`ssh_host` 只引用 `~/.ssh/config` 中已有的别名。工具不保存密钥、密码或主机地址；每次到期时建立一条短暂、非交互、无 Agent 转发、无连接复用的只读 SSH。

VPS 读取：

```text
/sys/class/net/<interface>/statistics/rx_bytes
/sys/class/net/<interface>/statistics/tx_bytes
/sys/class/net/<interface>/statistics/rx_packets
/sys/class/net/<interface>/statistics/tx_packets
```

Xray StatsService 必须只监听回环地址。每个 VLESS 用户可以通过 `email` 字段设置 `mac`、`android`、`pc` 等统计标签：

```json
{
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

实测账单关系：

```text
实测账单倍率 = VPS (RX + TX) ÷ Xray 用户 (uplink + downlink)
双边理想账单 = Xray 用户逻辑流量 × vps_billing_legs
账单附加量   = max(0, VPS 账单量 − 双边理想账单)
```

账单附加量包含两条链路的 IP/TCP 头与 ACK、连接建立、REALITY/Vision 填充、重传和少量 VPS 背景流量。包数拆分只是无需抓包的近似解释，不会被描述成精确丢包率。

## 告警、隐私与迁移

- 默认 5 分钟单方向超过 250 MiB 时警告；
- 默认 10 分钟上下行合计超过 1 GiB 时严重告警；
- 点击通知会打开仪表板；
- 证据快照只保存累计字节、聚合域名、代理路径和覆盖率；
- 不保存 URL 路径、查询参数、请求头、正文、提示词或文件内容。

旧版的进程组、`nettop` 代理拆分和 Codex Hook 配置会在启动时移除。配置迁移前的副本保存为 `config.toml.pre-mihomo`。如果曾安装本项目的 Codex Hook，迁移器只删除包含 `--traffic-sentinel-capture` 标记的处理器，保留所有其他 Hooks，并留下 `hooks.json.pre-domain-attribution` 备份。

主要状态文件：

- `samples.jsonl`：Mihomo 精确增量、域名和路径归因；
- `mihomo-baseline.json`：Mihomo 与连接累计基线；
- `vps_samples.jsonl`：VPS 网卡低频增量；
- `xray_user_samples.jsonl`：Xray 用户低频增量；
- `session.json`：当前可重置统计周期；
- `events.jsonl`：告警状态变化。

## 开发

```sh
python3 -m unittest discover -s tests -v
./bin/build-menubar-app.sh
```

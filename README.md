# Codex Traffic Sentinel

一个只读的 macOS 菜单栏 App，用来观察本机 AI 项目、代理出口和 VPS 双向账单流量。它不会抓包、读取提示词、上传文件、终止 Agent、删除文件或断网。

## 日常使用

直接打开项目根目录的 `Codex Traffic Sentinel.app`。App 会自行启动唯一的内置采样器，退出 App 时采样器也会退出，不需要手动运行脚本。

菜单栏格式类似：

```text
⌁ T2.9 GiB · Codex 8.4 MiB/s
```

- `T` 是**当前统计周期**内 VPS 入站 + 出站的实际新增量。
- 后半部分是当前采样中流量最大的项目及其实时速率。
- VPS 关闭时 `T` 显示 `—`。
- 点击“重置统计 / Reset totals”后，菜单栏 `T`、仪表板三张卡、项目统计、模型统计和趋势统一从新周期开始；VPS 会立即建立一个新的只读网卡基线。
- 月度 VPS 历史和 JSONL 日志不会因重置而删除，只是不再混入菜单栏当前值。

菜单中的 `Language / 语言` 可以即时切换中文或 English，适合分别截取中英文界面。

## 仪表板口径

仪表板首先展示三个同周期数据：

1. `VPS 当前账单量`：VPS 网卡入站 + 出站；
2. `本机代理外网`：配置代理进程的非回环 socket；
3. `本机 AI 流量`：所有 `role = "attribution"` 项目的进程流量合计。

项目不超过 3 个时全部列出；超过 3 个时显示 Top 3 和“其他项目”。`本机其他流量`按下面的差值估算：

```text
本机其他 ≈ max(0, 本机代理外网 − 已配置项目合计)
```

VPS 与本机代理不再直接相除，因为 VPS 还可能承载手机、其他电脑和服务流量。工具只使用一个可配置的经验上限：

```text
账单估算上限 = vps_billing_legs × (1 + link_overhead_ratio)
默认值          = 2 × (1 + 20%) = 2.4×
```

这里的 `20%` 是相对理想双边账单 `2×` 的链路余量；换算成一份本地逻辑流量，是额外 `0.4×`，不能称为“40% 丢包”。它涵盖协议封装、连接建立、重试、重传及实际链路差异，但工具不会进一步猜测具体原因。

其他设备使用保守差额：

```text
其他设备账单量 ≈ max(0, VPS T − 本机代理外网 × 2.4)
其他设备逻辑量 ≈ 其他设备账单量 ÷ 2.4
```

如果实际链路倍率低于 2.4×，这个算法会少估其他设备，不会把正常链路开销误报成其他设备。VPS 尚未产生重置后的完整区间时，界面会显示“等待 VPS 基线”，不会显示伪造的 0。

趋势图按不均匀采样时长归一为一分钟速率，纵轴明确使用 `MiB/min`；显示流量最大的 3 个项目和代理外网。

## Codex 模型、子 Agent 与工具活动

`Codex 活动与模型详情`使用 Codex 生命周期 Hooks，显示：

- 当前、累计和峰值子 Agent 数；
- 工具调用、读取类工具和相同读取输入的重复候选次数；
- 工具输入与返回值的序列化体积；
- Sol、Terra 等模型的估算流量和活动计数。

首次使用时点击 `安装 / 审核 Codex Hook`。安装器会保留已有 `~/.codex/hooks.json` 内容；如果仍待信任审核，会打开 ChatGPT 自带 Codex CLI 的官方审核界面。选择 `Trust all and continue` 后重启 ChatGPT，旧任务和新任务都可以记录之后发生的事件。

Codex 进程总流量来自 `nettop`。同一采样区间只有一个模型活跃时，模型行显示“高可信独占”；多个模型或任务重叠时，依据活跃执行者和工具事件大小估算分摊。加密连接可能由多个任务共享，因此并发时无法精确把每个网络字节分到单一模型。

Hook helper 只写入时间、模型、事件类型、匿名 ID、次数、字节数和读取输入的 SHA-256 指纹，不保存提示词、命令、路径、工具参数、工具正文或最后一条助手消息。

## 配置

App 首次启动会创建：

```text
~/Library/Application Support/Codex Traffic Sentinel/config.toml
```

修改后在菜单中点“重新启动监控”生效。旧版 `[vps.diagnostics]` 和 `[reconciliation]` 会在启动时迁移为 `[estimation]`；原配置保存在同目录的 `config.toml.pre-estimation`。

```toml
[monitor]
sample_seconds = 5
warning_window_seconds = 300
warning_bytes = 268435456
critical_window_seconds = 600
critical_bytes = 1073741824
alert_group = "codex"

[codex_activity]
enabled = true
process_group = "codex"
warning_active_subagents = 4
warning_total_subagents = 10

[[process_groups]]
id = "codex"
label = "Codex"
role = "attribution"
patterns = ["Codex (Service)", "codex", "codex-code-mode-host"]

[[process_groups]]
id = "antigravity"
label = "Antigravity"
role = "attribution"
patterns = ["Antigravity"]

[[process_groups]]
id = "proxy"
label = "本地代理"
role = "observer"
patterns = ["verge-mihomo"]

[vps]
enabled = true
ssh_host = "my-vps"
interface = "auto"
poll_seconds = 300
billing_cycle_start_day = 1

[estimation]
proxy_group = "proxy"
vps_billing_legs = 2.0
link_overhead_ratio = 0.20
```

配置说明：

- 规则按顺序排他匹配，同一个 PID 只进入第一个进程组，避免重复计数。
- `role = "attribution"` 表示一个要展示的项目；`alert_group` 必须指向其中之一。
- `role = "observer"` 用于代理等独立观察进程，不会与项目或 VPS `T` 相加。
- `estimation.proxy_group` 必须指向一个 observer；只使用它的非回环外网流量。
- `codex_activity.process_group` 选择与 Codex Hooks 对齐的项目。
- `ssh_host` 只引用 `~/.ssh/config` 里的主机别名；本工具不保存密钥、密码或主机地址。
- VPS 使用非交互、严格主机密钥检查、无 Agent 转发、无连接复用的短 SSH，只读取 `/sys/class/net/.../statistics/{rx,tx}_bytes`。默认每 5 分钟一次，可按需要改成 600 或 900 秒。
- 不再执行远端 TCP 重传、活跃 IP 或端口检查。

## 计数、告警与隐私

- 本机每 5 秒用 `nettop` 读取一次累计计数，再按 `PID + 进程名` 计算相邻差值。首次看到、PID 复用和计数器回退均记为 0，避免重放旧流量。
- 代理会额外按 `external`、`loopback`、`other` 拆分；估算只使用 `external`。
- VPS 同样只累计相邻网卡读数之间的新增量；入 + 出已经是双向计费总量，不再额外乘 2。
- 默认告警只针对 Codex：5 分钟单方向超过 250 MiB 警告，10 分钟双向合计超过 1 GiB 严重。
- 点击 App 发出的 macOS 通知会打开仪表板。
- 告警快照只包含相关 PID、字节增量和连接摘要，不读取工作区或文件内容。

状态保存在 `~/Library/Application Support/Codex Traffic Sentinel/state/`。主要文件：

- `samples.jsonl`：本机项目与 observer 进程增量；
- `proxy_segments.jsonl`：代理接口拆分；
- `vps_samples.jsonl`：低频 VPS 网卡增量；
- `session.json`：当前可重置统计周期；
- `codex_activity.json`：经过隐私缩减的模型与 Agent 计数。

升级前留下的 `vps_diagnostics*.jsonl` 不再读取或更新，也不会被自动删除。

## 开发

```sh
bin/build-menubar-app.sh
python3 -m unittest discover -s tests -v
```

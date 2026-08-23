# Infra Sentinel

**中文** · [English](#english)

Infra Sentinel 是一个本地优先的个人 AI 基础设施观测工具，用于集中查看网络流量、AI 用量、本机运行状态、上游服务状态和已接入设施。不同数据域保留各自的来源、单位和统计口径。

![Infra Sentinel 中文概览](assets/overview-zh.png)

> 截图使用匿名演示数据，不含真实主机名、IP、账户、路径或项目内容。

## 观测范围

### 本机客户端与资源

- **网络流量**：Mihomo / Clash Meta 兼容内核的连接、域名和代理路径归因。
- **AI 用量**：Codex 桌面端与 CLI、OpenCode Desktop、Antigravity、Antigravity IDE / CLI，以及已接入的 Infer Runtime。
- **本机运行**：CPU、内存压力、Swap、磁盘吞吐、IOPS、容量、温度压力，以及尽力而为的 App 磁盘 I/O 归因。

### 远端与云端

- **VPS**：通过本机 `~/.ssh/config` Host 别名读取 Linux 网卡统计；可选接入 Xray 用户逻辑流量。
- **上游状态**：OpenAI、Claude、DeepSeek、Kimi / Moonshot 与 Cursor 的公开官方状态页。
- **本地设施**：通过 [Infra Protocol](https://github.com/glenzli/infra-protocol) 自动发现并显示 PCP、Infer Runtime、Dev Mesh Observer 等兼容服务。详细诊断仍在各自 Console 中完成。

采集仅处理可核对的本地聚合数据与公开状态信息，且始终为只读操作：不会抓包，不会读取提示词、响应、项目文件或凭据，也不会修改代理、服务器或本机系统状态。Codex 用量以本机 rollout JSONL 的累计计数差分为唯一事实源，捕获后保存不含任务内容的聚合账本；不再使用 `threads.tokens_used` 推算用量。主图显示原始 Token，标准 API 参考值按模型分别代入输入、缓存输入、缓存写入和输出价格；详情中的 30% 缓存折算量仅用于兼容对照，是本机数据反推而非 OpenAI 公布的额度公式。对于短连接、来源离线或统计窗口不一致等无法可靠归因的情况，界面将明确标记为未知、未归因或等待采样；本地统计不作为供应商账单使用。

## 使用方式

启动 App 后，应用负责管理本地采样进程；关闭主窗口后继续驻留菜单栏。首次启动会创建：

```text
~/Library/Application Support/Infra Sentinel/config.toml
```

Mihomo 默认自动发现。远端 VPS 只需要在设置中填写已经存在于 `~/.ssh/config` 的 Host 别名；App 不保存私钥、密码或真实地址。

```sshconfig
Host edge-a
  HostName vps.example.com
  User root
  IdentityFile ~/.ssh/id_ed25519
```

Xray 用户统计是可选项：每个客户端需要独立 `email` 标签，StatsService 应只监听远端回环地址。

首次运行新版 Agent 时，会先重建仍可见的 Codex 历史，备份旧 SQLite 与旧 Codex
检查点，然后只移除 SQLite 中旧的 Codex 指标投影。此后 JSONL 聚合账本按新增记录
递增，已捕获历史不受后来删除任务影响；其他机器和重建前已经删除的任务仍然缺失。

如需单独核对某台机器仍可见的 Codex 本地 JSONL 用量，可从源码运行只读审计。它按
`total_token_usage` 累计差分去重，并处理计数器重置；结果同样不是账户账单：

```sh
PYTHONPATH=src python3 -m infra_sentinel.cli.codex_usage_audit \
  --from 2026-08-22 --to 2026-08-23 --timezone Asia/Shanghai
```

## 安装与构建

当前桌面包支持 macOS 13+。预编译 App 使用 ad-hoc 签名、未经过 Apple 公证；首次启动如被 macOS 拦截，可右键选择“打开”，或在“系统设置 → 隐私与安全性”中确认。

从源码构建需要 Xcode Command Line Tools、Rust、Node.js LTS、Python 3.11+ 与 PyInstaller：

```sh
git clone git@github.com:glenzli/infra-sentinel.git
cd infra-sentinel
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

Windows 与 Linux 的系统采集接口已预留，但当前只有 macOS 提供正式桌面包。

## 数据与隐私

本地状态保存在：

```text
~/Library/Application Support/Infra Sentinel/state/
```

状态数据可能包含聚合域名、模型名、Xray 客户端标签和用户设置的显示名。默认情况下，采集、存储和分析均在本机完成；上游状态检查仅访问供应商公开状态页。实现与架构说明见 [docs](docs/)、[ROADMAP.md](ROADMAP.md) 与 [设施发现说明](docs/facility-discovery.md)。

## 开发验证

```sh
python3 -m unittest discover -s tests -v
cd ui && npm run build
cd src-tauri && cargo test --offline
```

---

## English

[中文](#infra-sentinel)

Infra Sentinel is a local-first desktop dashboard for personal AI infrastructure. It keeps network traffic, AI usage, host pressure, public provider status, and compatible local facilities as separate, traceable measurements rather than reducing them to one opaque score.

![Infra Sentinel overview](assets/overview-en.png)

### Coverage

- **Local**: Mihomo / Clash Meta traffic attribution; Codex desktop and CLI, OpenCode Desktop, Antigravity, Antigravity IDE / CLI, and Infer Runtime usage; macOS CPU, memory, disk, thermal, and best-effort per-app disk I/O.
- **Remote**: Linux VPS interface counters through an existing SSH Host alias, optional Xray user counters, and public status feeds for OpenAI, Claude, DeepSeek, Kimi / Moonshot, and Cursor.
- **Facilities**: compatible local services discovered through [Infra Protocol](https://github.com/glenzli/infra-protocol), including PCP, Infer Runtime, and Dev Mesh Observer.

The dashboard brings together measurements that can be checked locally while keeping their sources visible. Collection stays read-only: it does not capture packets, inspect prompts or responses, touch project files or credentials, or alter proxy/server configuration. Codex usage has one fact source: cumulative-counter deltas from local rollout JSONL, retained after capture in an aggregate-only ledger; `threads.tokens_used` is not an accounting input. Charts show raw Tokens, while the standard API reference prices input, cached input, cache writes, and output separately by model. The 30%-cached compatibility indicator in details is inferred from local data, not a published OpenAI allowance formula. When a measurement cannot be explained reliably, it stays unknown or unattributed rather than becoming a made-up invoice number.

The packaged desktop app currently targets macOS 13+. Build from source with:

```sh
python3 -m pip install pyinstaller
./bin/build-desktop-app.sh
open "ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
```

On first launch, the app creates `~/Library/Application Support/Infra Sentinel/config.toml`. Mihomo is discovered automatically; remote hosts are referenced only by an existing `~/.ssh/config` Host alias.

On the first upgraded Agent run, Infra Sentinel rebuilds still-visible Codex
history, backs up the prior SQLite store and legacy Codex checkpoints, then
removes only the old Codex metric projection. Later JSONL events extend the
aggregate ledger, so deleting a rollout after capture does not erase history.
Other machines and rollouts deleted before the rebuild remain absent.

For a separate read-only reconstruction of this machine's still-visible Codex
JSONL usage, run:

```sh
PYTHONPATH=src python3 -m infra_sentinel.cli.codex_usage_audit \
  --from 2026-08-22 --to 2026-08-23 --timezone Asia/Shanghai
```

The audit deduplicates cumulative snapshots and handles counter resets. It is
not account billing.

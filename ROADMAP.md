# Infra Sentinel Roadmap

Infra Sentinel 已从单一流量工具迁移为个人 AI Infra 可观测面板。当前正式资源是 **Network** 与 **AI usage**；下一阶段优先提高数据可信度和分析价值，不为“综合”而添加弱指标。

## 产品原则

- 本地优先、只读优先、隐私最小化；
- 每项资源保留自己的单位，不制造无意义的综合分数；
- 精确值、Provider 报告值、本机近似值和未知值必须可区分；
- 新数据源通过 Collector 和统一指标合同接入，不修改既有模块语义；
- 监控与解释是默认边界，不自动杀进程、删文件、改路由或调整 Infra；
- 不采集提示词、响应正文、URL 路径、文件内容或抓包数据。

## 当前状态

| 能力 | 状态 | 当前结果 |
| --- | --- | --- |
| 多 VPS Network 计量 | 已完成 | Mihomo、Linux 网卡与 Xray 独立计量；多主机不串账 |
| Infra Core | 已完成 | MetricPoint、Collector、Source、Resource、Policy 与 Projection 合同 |
| 本地时序存储 | 已完成最小闭环 | SQLite WAL、幂等写入、旧网络回填、受限聚合查询 |
| Tauri 桌面壳 | 已完成 | macOS App、菜单栏健康状态、设置、通知与受限 Rust bridge |
| 跨平台基础 | 进行中 | 平台无关 Host 合同、macOS/Linux/Windows backend、跨平台单实例锁与可覆盖本地集成路径；正式 Release 仍仅 macOS |
| AI 用量 | 已完成首批来源 | OpenCode 与 Codex 今日/历史/模型/活动观测 |
| 本地设施观测 | 已完成 | Infra Discovery 自动发现、PCP / Infer Runtime 独立协议 adapter 与 Console 深链 |
| 跨资源洞察 | 尚未开始 | 等待足够稳定、可比较的长期样本 |
| 本地计算与存储 | 候选 | 只有出现明确需求和可靠归因路径后才进入主线 |

## 已形成的架构

```text
Collectors
    ↓
Canonical metric model
    ↓
Local SQLite time-series store
    ↓
Policies / attribution
    ↓
Versioned UI projections ── Notifications
```

语义所有者：

- **Runtime**：进程生命周期、采样调度、健康状态和 Collector 注册；
- **Collectors**：协议访问、来源 checkpoint、增量守恒和来源错误；
- **Metrics / Store**：单位、counter/gauge 语义、幂等写入、查询和迁移；
- **Policies**：网络突增、VPS 每日用量和事件状态机；
- **Projections**：将事实转换为菜单栏、概览、资源详情和通知；
- **Native App**：窗口、导航、本地化和交互，不承担业务计量。
- **Platform adapters**：系统资源、进程锁、通知、URL 打开、本地应用发现与打包；不把平台分支带入指标、策略或 UI 语义。
- **Infra Discovery**：只负责验证注册、租约以及具体协议版本与 binding 的精确匹配；
- **Facility adapters**：分别拥有 PCP 与 Infer Runtime 应用 wire 的读取和私有 Projection 归一化；专属诊断留在设施 Console。

## 下一阶段：稳定与信任

暂不增加新的资源类型，先完成以下收尾：

1. 长时间运行验证：重启、休眠、Mihomo 重载、SSH 超时和状态库变化不造成重复计数；
2. 存储生命周期：明确 SQLite 保留、降采样、导出和旧 JSONL 退出条件；
3. 观测可信度：每个总量都能追溯到来源、窗口、方法和覆盖范围；
4. 分析一致性：Network 与 AI 详情共享时间范围、总量、组成和速率的交互结构；
5. 发布质量：匿名截图、准确 README、构建验证、最低系统版本和未公证说明同步更新。

## 跨平台迁移顺序

1. **已完成基础边界**：HostReading + capability 合同；macOS、Linux、Windows 系统 backend；Agent 单实例锁；SSH/OpenCode/Codex 路径覆盖；UI 按能力展示。
2. **Linux 桌面闭环**：Mihomo TCP/Unix Controller 的本机安全合同、桌面通知、Infra Protocol Unix Socket 实机互通、AppImage/deb 打包与休眠验证。
3. **Windows 桌面闭环**：回环 Mihomo Controller 与 Secret、Infra Protocol Named Pipe adapter、原生通知、OpenSSH/便携数据库路径验证、MSI/NSIS 打包。
4. **发布矩阵**：三平台独立 CI 构建和测试；每个平台只声明经过实机验证的 capability，不追求虚假的功能对称。

退出标准：

- Collector 失败不阻断其他资源；
- App 重启不重放已经写入的区间；
- 总量与维度分解守恒，残差明确展示；
- 查询不阻塞网络采样，UI 不直接访问存储；
- 文档描述与当前 App 行为一致。

## 新指标的进入门槛

未来任何新资源必须同时满足：

1. **可观测**：存在稳定、只读且可验证的数据来源；
2. **可归因**：至少能归到来源、模型、主机或工作负载之一；
3. **可行动**：数据能支持预算、异常发现或 Infra 调优；
4. **低侵入**：采样成本和隐私风险与收益相称；
5. **不伪装**：缺失字段保持未知，不用推断值冒充账单。

因此，屏幕时间目前不进入路线图。它难以区分“人在操作”“AI 在后台运行”和“为任务阅读资料”，归因弱且隐私成本高。

## 候选方向

这些方向没有承诺顺序，也不会提前创建空模块。

### Provider/API 成本

- 使用 Provider 明确返回的费用、额度或限流事件；
- 在价格表足够稳定时提供带版本和时间戳的估算费用；
- 真实费用与费率估算始终分开。

### 可靠性与浪费

- 失败、重试、超时、限流及其对应的 Token/流量增量；
- 区分真实额外工作、上下文重放与无法解释的残差；
- 只在数据能支持时给出结论，不把相关性写成因果性。

### 本地计算

- 本地模型实际启用后再接入 CPU/GPU、显存或统一内存与活跃时间；
- 无权限、无 GPU 或无本地推理时隐藏模块；
- gauge 与累计运行时间不能相加。

### 存储

- 仅扫描用户明确授权的模型、缓存、构建和日志目录；
- 展示当前体积、增长量和增长最快实体；
- 不扫描整个磁盘，不读取文件内容。

### 跨资源洞察

- 每个 Agent、模型或工作负载的 Token、网络、费用和可靠性对比；
- 基线、突变、周期异常和可解释残差；
- 基于证据的模型、并发、缓存、路由和保留策略建议。

## 长期不做

- 云端集中遥测或默认同步个人使用数据；
- 提示词、响应内容和文件内容分析；
- 将字节、Token、GPU 与磁盘强行合成单一分数；
- 自动修改用户的代理、VPS、模型或 Agent 配置；
- 为尚不存在的 Provider API 长期维护猜测性兼容代码。

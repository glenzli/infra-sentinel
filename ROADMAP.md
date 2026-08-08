# Infra Sentinel Roadmap

本路线图描述 Infra Sentinel 从单一网络流量工具迁移为个人 AI Infra 资源归因平台的过程。

路线图按依赖关系组织，不承诺具体日期。每个阶段必须达到退出标准后，后续阶段才进入主线。现有网络统计是第一个正式资源模块，不推倒重写，也不允许新能力继续堆入单个采样器或界面控制器。

## 产品方向

Infra Sentinel 面向个人拥有或控制的本地设备、代理链路、VPS 与 API 账户，提供：

- 资源使用采集：网络、API、存储、本地计算等；
- 多维归因：项目、工作负载、服务商、模型、设备、主机和账户；
- 配额与预算：周期额度、真实费用和基于费率的估算；
- 异常检测：突增、超预算、采集失联和无法解释的残差；
- 长期反馈：用证据支持模型、并发、缓存、路由和存储策略调优。

项目继续坚持本地优先、只读优先和隐私最小化。默认不采集提示词、请求正文、响应正文、URL 路径、文件内容或抓包数据。

## 阶段速览

版本号是建议落点，阶段退出标准优先于版本日期。

| 阶段 | 建议版本 | 结果 | 状态 |
| --- | --- | --- | --- |
| 0 | 1.1 | 稳定多 VPS 与现有网络统计 | 进行中 |
| 1 | 1.2 | 建立统一指标和 Collector 合同 | 进行中 |
| 2 | 1.3 | SQLite 时序存储与一次性迁移 | 进行中 |
| 3 | 2.0 | Infra Sentinel 外壳与健康状态菜单栏 | 进行中 |
| 4 | 2.1 | 首个 API 使用与配额模块 | 候选 |
| 5 | 2.2 | 本地存储与计算模块 | 候选 |
| 6 | 3.x | 跨资源分析与调优建议 | 远期 |

## 不做什么

- 不把字节、Token、磁盘容量、GPU 利用率强行合成一个没有意义的“总资源量”；
- 不把估算值伪装成精确账单；
- 不为了支持未来功能提前实现大量空采集器；
- 不长期维护两套配置、存储或计算管线；
- 不在第一阶段引入云端账户、遥测上传或中心服务器；
- 不自动杀进程、删文件、调整路由或修改 Infra 配置。

## 目标架构

```text
Collectors
    ↓
Canonical metric model
    ↓
Local time-series store
    ↓
Attribution engine
    ↓
Budget / anomaly policies
    ↓
UI projections ── Notifications ── Insights
```

### 语义所有者

目标代码边界按生命周期和失败策略划分：

- **Runtime**：进程生命周期、采样调度、健康状态和模块注册；
- **Collectors**：协议访问、原始计数器 checkpoint 和来源错误；
- **Metrics**：指标命名、单位、counter/gauge/quota/event 语义和校验；
- **Store**：原子写入、查询、保留策略、降采样和迁移；
- **Attribution**：实体映射、残差、方法和可信度；
- **Policies**：预算、阈值、异常和事件状态机；
- **Projections**：为菜单栏、仪表板、报告和通知生成只读视图；
- **Native App**：窗口、导航、交互和本地化，不承担业务计算。

现有 Mihomo、VPS 和 Xray 逻辑迁入网络 Collector。`sentinel.py` 最终只负责组合与生命周期，Dashboard Controller 最终只负责界面路由。

## 统一指标模型

所有资源模块输出同一种逻辑记录：

```text
MetricPoint
  observed_at
  interval_start / interval_end
  metric                 # network.bytes, api.tokens.input, disk.used_bytes
  instrument             # counter, gauge, quota, event
  value
  unit                   # bytes, count, seconds, ratio, usd ...
  source_id
  resource_id
  dimensions             # provider, model, project, host, device ...
  attribution_method     # exact, mapped, inferred, residual
  confidence
  estimated
```

要求：

- 指标名与单位稳定，展示文案由 UI 本地化；
- counter 保存区间增量，gauge 保存时点值；
- 真实费用与估算费用使用不同标记；
- 高基数字段、密钥、路径和请求内容不得进入 dimensions；
- 未归因数据必须保留为 residual，不能静默丢失。

## Source / Policy 配置预留结构

配置围绕数据源和策略组织，版本标识采用 `YYYYMMDD.修订号`。以下是目标轮廓，不代表当前版本已经支持所有类型：

```toml
schema_version = "20260808.3"

[app]
menu_bar_mode = "health"
retention_days = 90

[[sources]]
id = "local-mihomo"
kind = "network.mihomo"
enabled = true

[[sources]]
id = "vps-primary"
kind = "network.linux-xray"
label = "Primary VPS"
enabled = true

[sources.connection]
ssh_host = "my-vps"

[sources.billing]
mode = "both"
cycle_start_day = 1

[[sources]]
id = "api-primary"
kind = "api.provider"
label = "Primary API Account"
enabled = false

[sources.credentials]
keychain_account = "infra-sentinel-api-primary"

[[policies]]
id = "vps-primary-billing-alert"
kind = "network.billing.threshold"
source_id = "vps-primary"
metric = "network.billable_bytes"
period = "month"
warning_bytes = 1099511627776
critical_bytes = 1374389534720
```

密钥只进入 macOS Keychain，配置文件只保存引用。SSH 继续只保存 `~/.ssh/config` 中的 Host 别名。

## 迁移映射

| 当前职责 | 目标职责 | 迁移方式 |
| --- | --- | --- |
| Mihomo Socket 与连接归因 | `network.mihomo` Collector | 保留算法和精确总量不变量，改变输出接口 |
| 多 VPS 网卡采样 | `network.linux` Collector | 每个 source 独立 checkpoint 和健康状态 |
| Xray 用户统计 | `network.xray` Collector / attribution input | 保留用户维度，输出标准指标 |
| `SessionMeter` | 查询范围与会话投影 | 会话不再拥有一套独立账本 |
| `events.jsonl` 与 AlertEngine | Policies 与 Incidents | 保留告警状态机语义 |
| `menubar.json` | Overview projection | 作为派生视图，不作为事实来源 |
| JSONL 主存储 | SQLite 主存储 | 单次导入后停止双写；JSONL 仅用于导出和诊断 |
| `[monitor]` / `[remote]` | `[[sources]]` / `[[policies]]` | 已实施一次性迁移并备份旧配置 |

## 阶段路线

### 0. 稳定 Network 模块 1.x

状态：进行中。

范围：

- 完成多 VPS 独立采样、配置和仪表盘显示；
- 为每台 VPS 保留独立的角色、计费口径和账单告警策略；全局本机告警与任一 VPS 账单告警不得混算；
- 修复设置页和远端明细的布局、迁移和保存问题；
- 为现有流量统计建立 golden fixtures；
- 明确当前隐私边界和统计不变量；
- 对菜单栏进程进行至少一次长时间稳定性观察。

退出标准：

- 本机总量、域名归因与未归因始终闭合；
- 多 VPS 基线不串账，重启后不重放旧区间；
- 汇总倍率仅使用同一 VPS 上同时具备网卡账单与 Xray 逻辑流量的配对数据；
- 设置、重置、通知跳转和 App 构建均有回归验证；
- 当前功能可以作为迁移前对照实现。

### 1. 建立 Infra Core

状态：进行中。

范围：

- 定义 MetricPoint、Source、Resource、Entity、Attribution 与 Policy 合同；
- 建立 Collector 注册表和能力声明；
- 将运行时健康状态与业务指标分开；
- 建立内存实现与合同测试，暂不更换现有存储；
- 网络模块通过适配器产出标准指标与资源投影；原网络详情作为正式 Network 模块保留。

退出标准：

- 新增一个虚拟 Collector 不需要修改 Runtime、存储和 UI 核心；
- 单位、counter/gauge 语义和来源身份拥有固定测试；
- 网络标准指标与当前流量账本在 fixtures 上完全对得上；
- 没有把新模型塞回 `sentinel.py` 或 Native Controller。

### 2. SQLite 时序存储与一次性迁移

状态：进行中。

已完成：`MetricStore` 使用 SQLite WAL、原子事务和稳定去重键，首次运行会一次性回填既有网络 JSONL。当前网络原始 JSONL 仍是会话恢复、证据和旧报告的输入；在查询、报表与保留策略全部切换前，它不会被删除或伪装成已退役。

范围：

- 建立 metrics、entities、attributions、policies、incidents 和 collector checkpoints；
- 使用 WAL 与原子事务；
- 支持按时间、来源、资源和维度查询；
- 建立分钟、小时和天级降采样；
- 导入现有网络 JSONL 和当前会话；
- 迁移前备份旧配置和状态，成功后停止双写。

退出标准：

- 崩溃不会产生重复计数或半写入区间；
- 迁移工具可重复检查，但只提交一次迁移事务；
- 新旧网络统计在同一时间范围内误差为零；
- 可明确回滚到迁移前备份，不维护永久兼容分支。

### 3. Infra Sentinel App 外壳

状态：计划。

范围：

- 品牌从 Traffic Sentinel 迁移为 Infra Sentinel；
- 菜单栏默认只显示正常、警告、严重或采集异常状态；
- 点击菜单栏进入完整仪表盘；
- 建立概览、资源使用、归因、预算与告警、数据源、分析建议和设置导航；第一轮先交付概览、Network 资源卡和数据源健康摘要；
- 卡片由 Projection 提供，未启用的资源模块不显示；
- 网络模块成为第一张正式资源卡片。

退出标准：

- 菜单栏不依赖某个特定指标或单位；
- 网络模块达到 1.x 功能对等；
- Native App 不直接查询 Collector 或计算账单；
- 中文和 English 覆盖全部新导航及状态。

### 4. API 使用与配额模块

状态：候选。

范围：

- 先支持一个明确、可验证的 Provider；
- 采集请求数、Token、真实费用、配额快照和限流事件中实际可获得的部分；
- 账户凭证存入 Keychain；
- 支持 provider、account、model、project 等低敏维度；
- 无法从 Provider 获得的数据保持未知，不用推断值补齐。

退出标准：

- 不保存提示词、请求正文、响应正文和 API Key；
- Provider 返回值与账单/额度页面可对账；
- 真实费用和费率估算在 UI 中有明确区别；
- Provider 失败不会影响网络模块采样。

### 5. 本地存储与计算模块

状态：候选。

范围：

- 受控目录的当前体积、增长量和增长最快实体；
- 本地 GPU 可用性、利用率、显存/统一内存和活跃时间中系统允许只读获取的部分；
- 能力探测和权限说明；
- 默认不扫描整个磁盘，不记录文件内容；
- 将本地计算归因到明确的工作负载或保留为 residual。

退出标准：

- 采样开销可量化且默认低影响；
- 无权限或无 GPU 时优雅隐藏模块；
- 目录范围由用户明确授权；
- gauge 与累计使用时间不会被错误相加。

### 6. 跨资源洞察

状态：远期。

范围：

- 基线、趋势、突变和周期异常；
- 每项目、每工作负载、每模型的资源与费用视图；
- 网络、API、本地计算和存储之间的相关性分析；
- 基于证据的并发、缓存、模型、路由和保留策略建议；
- 明确区分观察、相关、推断和建议。

退出标准：

- 每条建议能追溯到指标、时间范围和归因方法；
- 不把相关性表述成因果性；
- 用户可以关闭洞察但继续使用监控与预算功能；
- 建议系统不自动修改 Infra。

## UI 演进

### 菜单栏

- 正常：安静的应用图标；
- 警告：预算接近、异常增长或单个 Collector 退化；
- 严重：预算超限、持续异常或关键来源失联；
- 点击：打开仪表盘；
- 默认不常驻展示某一种资源数字。

### 概览页

- 时间范围与总体健康状态；
- 当前预算风险和活跃异常；
- 网络、API、本地计算、存储等资源卡片；
- 每张卡只显示自身单位、趋势和最大来源；
- 没有数据的模块隐藏，而不是展示大面积零值。

### 资源详情

- 使用量与趋势；
- 真实费用、估算费用和额度余量；
- Top sources 与 residual；
- 数据来源、最近采样和可信度；
- 与其他资源的相关事件。

## 数据与隐私约束

- 本地 SQLite 是事实来源，导出文件不是；
- API 密钥使用 Keychain，SSH 使用已有 Host alias；
- 不存储提示词、消息、响应、URL 路径、请求头和文件内容；
- 项目名、模型名和账户标签允许用户重命名或隐藏；
- 每个 Collector 声明采集字段、权限、频率和保留策略；
- 删除数据是显式操作，并说明影响范围；
- 任何云端同步都必须作为未来独立功能重新设计和授权。

## 工程门槛

每个阶段都必须满足：

- Collector 失败隔离，不阻塞其他资源；
- 写入幂等，累计计数不因重启或补采重复；
- 配置和存储迁移有备份、验证与明确终点；
- 新指标拥有单位和聚合语义测试；
- UI 只消费 Projection，不复制业务计算；
- 构建包显式包含所有新增模块；
- README、配置示例、隐私说明和测试同时更新；
- 不引入永久兼容层或无主的 `helpers/common` 模块。

## 当前建议的下一步

在当前多 VPS 修改稳定并提交后，启动阶段 1，但只完成以下最小闭环：

1. 定义标准指标与 Source 合同；
2. 建立 Collector 注册与隔离生命周期；
3. 用适配器把 Mihomo、VPS、Xray 输出为标准网络指标；
4. 用 fixtures 验证新旧流量结果完全一致；
5. 暂不改菜单栏、不迁 SQLite、不增加 API/GPU/磁盘采集。

这个闭环完成后，阶段 2 和阶段 3 才有稳定基础，也能避免边做新 UI 边反复修改底层数据合同。

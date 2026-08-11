# Infra Sentinel 架构

[English](architecture.md)

仓库将可移植的资源语义、操作系统集成与可执行打包明确分开：

```text
bin/                         仅可执行 wrapper 与发布脚本
src/infra_sentinel/
  app/                       Agent 生命周期、配置、sidecar 协议
  core/                      Collector、指标、注册表、Projection 合同
  metrics/                   SQLite 持久化与受限查询
  platform/                  窄进程/操作系统集成合同
  resources/
    ai/                      本地 AI 用量 Provider adapter
    facilities/              Infra Protocol discovery 与 Provider adapter
    network/                 Mihomo、VPS、Xray、计量与策略
    system/                  capability 驱动的主机 Collector
      backends/              每个操作系统独立验证的 adapter
    upstream/                官方 Provider 状态观测
ui/                          平台无关 WebView 与原生 Tauri 壳
tests/                       合同与行为测试
```

`bin/infra_agent.py`、`bin/configuration.py`、`bin/report.py` 与
`bin/snapshot.py` 不拥有产品逻辑，只负责加载源码包并调用类型化入口。
PyInstaller 打包的也是测试直接导入的同一个 `infra_sentinel` 包。

## 平台边界

- 可移植 Collector 只消费协议和已声明 capability，不按操作系统分支；
- 平台选择只在 adapter 边界发生，未选中的主机 backend 不会被导入；
- 缺失能力保持缺失，不伪装成 0、“正常”或推断等价物；
- backend 可以先进入共享代码树，但只有在目标平台完成行为、生命周期、打包与休眠/恢复验证后，才进入正式支持范围；
- 用户自选的程序或数据库位置通过受验证配置字段进入，资源 Collector 不任意扫描磁盘。

原生壳遵循相同原则：Rust 负责通知、URL 打开、sidecar 生命周期和目标平台打包；WebView 只读取版本化 Projection 并提交白名单命令。

## 增加资源 Provider

1. 在对应资源族下实现一个 adapter，由它拥有协议、checkpoint 与错误语义；
2. 通过 Core Collector 合同返回规范指标点与隐私受限的当前快照；
3. 在 Agent composition root 注册 Collector；
4. Projection/UI 只为稳定资源语义扩展，不暴露 Provider transport 细节；
5. 增加 adapter 聚焦测试和一项 Projection 集成测试。

这样，后续 Windows 与 Linux 工作只需在对应平台填充并验证现有 adapter 合同，不需要把平台判断扩散到计量、策略、存储或 UI。

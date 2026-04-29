# DBK 当前完成度与后续计划

更新时间：2026-04-29（Asia/Shanghai）

## 1. 当前完成度总结

### 1.1 总体完成度（按详细设计文档目标估算）

* Phase 1（CLI + provider + basic loop）：`85%`
* Phase 2（workflow/tools/build-test）：`75%`
* Phase 3（observability/runtime analysis）：`78%`
* Phase 4（AIOps/knowledge/automation）：`30%`
* 全局综合完成度：`67%`

说明：当前仓库核心聚焦在 runtime/observability 方向，provider 多模型与完整工作流编排仍需继续补齐。

### 1.2 已完成能力

* `dbk` CLI 基础可用，支持 `collect / metrics / trace / diagnose / runtime`
* runtime metrics 采集（`mock` + `pgstat`）与 sqlite 落库
* PostgreSQL 能力探测与兼容降级（`pg_stat_statements` / `pg_stat_io` 缺失时降级）
* 故障诊断（latency incident）与 evidence bundle 产出
* trace profile 管理（模拟执行 + execute 守卫）
* 持续采集 daemon（多实例，按实例状态文件管理）
* 调度策略：priority、max-running、preempt、max-collections-per-minute
* 采集筛选：tag/source/instance-pattern/min-priority 组合过滤
* runtime retention 清理（dry-run/apply/vacuum）
* cleanup daemon 与 cleanup history 报表（支持 `window-hours`）
* 自动化测试体系：`28 passed, 1 skipped`

## 2. 待完成事项（重点）

### 2.1 功能缺口

* 真正的 Workflow Orchestrator（requirements→research→design→implement→test→runtime→doc→ops）尚未实现
* Provider 抽象与多模型适配未实现（目前无实际 LLM provider 流程）
* Sub-agent 调度框架未实现（仅 runtime/daemon 模块）
* AIOps pipeline 仍是初版规则逻辑，缺少 detect/analyze/recommend/validate 完整闭环
* eBPF execute 仍受运行环境权限影响，缺统一提权代理/审计链路

### 2.2 工程化缺口

* 缺 packaging 与发布流程（版本、变更日志、发布脚本）
* 缺统一配置体系（全局配置文件 + 环境覆盖 + 配置校验）
* 缺 CI 流程与质量门禁（lint/type/test/report）
* 缺 API/SDK 形式的复用层（目前以 CLI 为主）

### 2.3 风险与注意点

* 在当前沙箱环境下，daemon stop 可能出现 `permission_denied_on_sigterm`
* cleanup-daemon 在低阈值配置下可能快速清理大量历史数据，需生产保护阈值
* 历史报表目前为本地 JSONL 聚合，未接入集中式存储

## 3. 后续实现计划

### 3.1 迭代 A（1 周）：稳定性与可运维性

目标：让 runtime 子系统可长期稳定运行。

任务：

* 为 daemon/cleanup-daemon 增加统一 process wrapper（优雅停止、权限策略、健康探针）
* 增加 retention 安全阈值（最小保留小时数、最大单次删除量、二次确认开关）
* 为 `cleanup-report` 增加按实例维度统计（top instances by deleted metrics）
* 补齐异常链路测试（权限受限、损坏状态文件、历史文件损坏）

验收标准：

* 长跑 24h 无崩溃
* stop 行为在受限/非受限环境均返回可解释状态
* cleanup 安全阈值误删防护可测试

### 3.2 迭代 B（1-2 周）：真实数据库能力增强

目标：提升 pgstat 采集的真实性和诊断精度。

任务：

* 增加 PG 版本兼容映射（14/15/16）细粒度特性开关
* 接入慢 SQL 指纹采样与 explain 关联（诊断报告可引用）
* 增加复制/锁冲突专题诊断器（bottleneck template）
* 扩展 Docker 集成测试矩阵（多版本并行）

验收标准：

* pg 14/15/16 集成测试稳定通过
* 诊断报告包含 SQL 指纹与可执行验证命令

### 3.3 迭代 C（2 周）：工作流与 Agent 主干

目标：把 DBK 从 runtime 工具扩展为工程 Agent。

任务：

* 实现 workflow 状态机与阶段切换（最小 requirements→design→implement→test→runtime）
* provider 抽象层接入至少 1 个可运行 provider
* session runtime 持久化（任务/阶段/artifacts/history）
* 统一命令入口：`dbk run "<intent>"` 自动映射 workflow stage

验收标准：

* 端到端任务可从自然语言意图自动进入对应阶段
* 每阶段产物可追溯并写入 session store

## 4. 立即执行顺序（下一步）

1. 完成 daemon process wrapper（优先解决权限与停止行为一致性）
2. 增加 cleanup 安全阈值与限流策略
3. 推进 PG 版本矩阵集成测试自动化
4. 开始 workflow 状态机最小实现

## 5. 非目标与忽略项

* 当前阶段继续忽略工作区中与主线无关内容：`.DS_Store`、`jianli/`


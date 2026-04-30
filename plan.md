# DBK 当前完成度与后续计划

更新时间：2026-04-29（Asia/Shanghai）

## 1. 当前完成度总结

### 1.1 总体完成度（按详细设计文档目标估算）

* Phase 1（CLI + provider + basic loop）：`85%`
* Phase 2（workflow/tools/build-test）：`75%`
* Phase 3（observability/runtime analysis）：`88%` （+10%，新增时序分析/SQL诊断命令/trend检测/置信度）
* Phase 4（AIOps/knowledge/automation）：`35%` （+5%，改进 RCA 证据链/可执行 runbook）
* 全局综合完成度：`70%`（+3%）

### 1.2 已完成能力

* `dbk` CLI 基础可用，支持 `init / validate / collect / metrics / trace / diagnose / runtime`
* runtime metrics 采集（`mock` + `pgstat`）与 sqlite 落库，**现已支持 10 类指标**（原 6 类）：
  - 原有：query.p95_latency / wait.lock_ratio / io.read_latency / lock.blocked_sessions / replication.lag / buffer.hit_ratio
  - 新增：connection.active_count / connection.total_count / transaction.rollback_ratio_pct / checkpoint.write_latency_ms
* PostgreSQL 14/15/16/17 **版本特性映射表**，能力探测增强（含 pg_stat_bgwriter）
* **时序分析**：最近 20 个数据点的 avg/max/min/trend（stable/increasing/decreasing）
* **方向感知阈值判断**：query/lock/IO 等高值异常，buffer hit ratio 等低值异常
* **置信度评级**：基于并发异常数量的 high/medium/low
* **可执行 SQL runbook**：6 条诊断 SQL（active_wait / blocked_locks / long_running / cache_hit / replication_lag / top_slow_queries）
* runtime metrics **范围查询**（--from / --to / --aggregate）和聚合（avg/max/min）
* cleanup **安全阈值**：`--safety-floor-hours`（默认 24h）+ `--max-delete-per-run`（默认 10 万行）+ 截断标志
* cleanup **按实例 top 统计**：`top_instances` 报告
* daemon **两阶段优雅停止**（SIGTERM grace 3s + SIGKILL）+ ProcessLookupError 防护
* cleanup daemon 安全参数持久化（state.json）
* 配置系统：**环境变量覆盖**（DBK_ROOT / DBK_RUNTIME_DB_PATH / DBK_ARTIFACTS_ROOT / DBK_PG_DSN 等）
* 配置**校验命令**：`dbk validate`（目录写权限 / env var 类型检查）
* **持续采集 daemon**（多实例，优先级抢占，max_collections_per_minute 节流）
* 故障诊断（latency incident）与 evidence bundle 产出（evidence.json + runbook.md）
* trace profile 管理（模拟执行 + execute 守卫）
* cleanup daemon 与 cleanup history 报表（支持 window-hours + top instances）
* 自动化测试：**28 passed, 1 skipped**（含 Docker PG 集成测试）

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

* ~~在沙箱环境下，daemon stop 出现 `permission_denied_on_sigterm`~~ **已修复：两阶段停止 + SIGKILL fallback**
* ~~cleanup-daemon 在低阈值配置下可能快速清理大量历史数据~~ **已修复：safety_floor_hours + max_delete_per_run**
* 历史报表目前为本地 JSONL 聚合，未接入集中式存储（仍有效）

### 2.4 本次优化（2026-04-29）已解决

迭代 A 核心任务全部完成：
- daemon 优雅停止（两阶段 SIGTERM/SIGKILL，graceful_timeout 3s，ProcessLookupError 处理）
- cleanup 安全阈值（safety_floor_hours 默认 24h，max_delete_per_run 默认 10 万行，截断标志）
- cleanup report 按实例 top 统计
- 异常链路测试已补齐（28 passed）

迭代 B 部分提前完成：
- PG 14/15/16/17 版本特性映射（_PG_VERSION_FEATURES 字典 + _pg_features_for_version 查找）
- 新增 pg_stat_bgwriter 探测和 checkpoint.write_latency_ms 指标
- 新增 connection.* / transaction.rollback_ratio 指标（10 类指标体系）

迭代 C 前置能力补充：
- storage 范围查询（query_metric_range）+ 聚合（aggregate_rows）
- diagnose 增强：时序分析（avg/max/min/trend）+ 可执行 SQL runbook（6 条）+ 置信度评级
- 配置系统：环境变量覆盖 + dbk validate 命令

## 3. 后续实现计划

### 3.1 ~~迭代 A~~ 稳定性与可运维性（已完成）

~~目标：让 runtime 子系统可长期稳定运行。~~

~~任务：~~

- ~~daemon 优雅停止~~ ✅ 两阶段 SIGTERM/SIGKILL
- ~~cleanup 安全阈值~~ ✅ safety_floor + max_delete_per_run
- ~~cleanup report 按实例 top~~ ✅ top_instances 统计
- ~~补齐异常链路测试~~ ✅ 28 passed

验收标准：
- 长跑 24h 无崩溃 — 待验证
- stop 行为在受限/非受限环境均返回可解释状态 — ✅
- cleanup 安全阈值误删防护可测试 — ✅

### 3.2 迭代 B（进行中）：真实数据库能力增强

目标：提升 pgstat 采集的真实性和诊断精度。

任务：

- ~~PG 版本兼容映射（14/15/16/17）~~ ✅ _PG_VERSION_FEATURES + _pg_features_for_version
- 接入慢 SQL 指纹采样与 explain 关联（诊断报告可引用）
- 增加复制/锁冲突专题诊断器（bottleneck template）
- 扩展 Docker 集成测试矩阵（多版本并行）

验收标准：

- pg 14/15/16/17 集成测试稳定通过
- 诊断报告包含 SQL 指纹与可执行验证命令 — ~~部分~~ ✅ runbook 含 6 条 SQL

### 3.3 迭代 C（待启动）：工作流与 Agent 主干

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

## 5. CLI 重构策略（参考 pi-mono）

目标：吸收 `pi-mono` 在 agent core / runtime / tui 分层上的优点，同时避免立即跨语言重写风险。

策略：借鉴设计，不直接迁移。

### 5.1 短期（当前迭代）

* 保持 Python CLI 主体不变
* 继续按“core/runtime/daemon/report”边界整理模块
* 新功能优先以可测试的独立模块落地，避免 CLI 巨石化

### 5.2 中期（1-2 个迭代）

* 提炼 provider/runtime 抽象接口，对齐 `pi-agent-core` 风格职责
* 引入统一 command contract（输入/输出 schema、错误码）
* 为未来 TUI 或多前端接入预留 IPC/HTTP adapter

### 5.3 长期（按需求触发）

两条可选路线：

* 路线 A：继续 Python 技术栈，增强 TUI（低风险、快落地）
* 路线 B：新增 TS 前端层（参考 `pi-tui`），通过 IPC 调用现有 Python runtime（中风险、扩展性更强）

触发条件（满足任一）：

* 需要复杂交互式 TUI 且 Python UI 框架出现明显瓶颈
* 需要多终端共享同一 agent core 协议
* 现有 CLI 的演进速度明显受限于架构边界

## 6. 非目标与忽略项

* 当前阶段继续忽略工作区中与主线无关内容：`.DS_Store`、`jianli/`

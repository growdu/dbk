# 架构整改开发计划

## 1. 目标

本计划用于落实 [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) 中的整改建议，
目标是把当前分散的 agent 执行路径收敛为一条稳定、可测试、可扩展的主链路。

本轮整改不追求新增能力，优先解决以下问题：

- session 恢复不可靠
- workflow 只停留在提示词层
- plugin 扩展点与主执行链路脱节
- sub-agent 架构不是默认生产架构
- memory 不能稳定参与实时推理
- API 并发下 session 状态可能被覆盖

## 2. 总体原则

### 2.1 先收敛，再扩展

先统一主执行路径，再考虑增强 workflow、memory、sub-agent 和 plugin。

### 2.2 先修控制面，再修表现层

优先修复 `Agent` / `MainAgent` / `WorkflowOrchestrator` / session / plugin 的
协作关系，不先做 UI、TUI 或新工具扩展。

### 2.3 每阶段必须有验收标准

每个阶段都需要：

- 代码改动范围
- 必须新增的测试
- 可验证的完成条件

## 3. 里程碑

### Milestone 1：统一执行主链路

目标：

- 明确唯一生产 agent 类型
- 让 CLI / REPL / API / SDK 走同一条请求生命周期

建议结论：

- 以 `MainAgent` 作为默认生产 agent
- `Agent` 保留为内部基类，不再直接作为主要入口实例化

涉及模块：

- `dbk/agent/core.py`
- `dbk/agent/subagent.py`
- `dbk/api_server.py`
- `dbk/cli.py`
- `dbk/cli_agent.py`
- `dbk/cli_commands/run.py`
- `dbk/sdk.py`
- `dbk/agent/repl.py`
- `dbk/cli_tui.py`
- `dbk/tui.py`

任务拆分：

1. 盘点所有直接实例化 `Agent` 的入口。
2. 设计统一 agent factory，避免入口各自决定 agent 类型。
3. 将 API、REPL、CLI、SDK 切换为统一 factory。
4. 保留 direct 模式作为 `MainAgent` 的内部行为，而不是另一套公开架构。
5. 修正文档中对默认 agent 行为的描述。

验收标准：

- `rg "Agent\\(" dbk` 中仅保留基类/测试/子 agent 内部使用
- API、CLI、REPL、SDK 均可返回统一的 `info()` 结构
- sub-agent 开关行为在不同入口下保持一致

### Milestone 2：修复 session 生命周期

目标：

- 让 session 恢复成为主链路的一部分
- 消除“同一 session_id 实际新建会话”的隐患

涉及模块：

- `dbk/agent/core.py`
- `dbk/agent/session.py`
- `dbk/agent/session_store.py`
- `dbk/agent/subagent.py`
- `dbk/api_server.py`

任务拆分：

1. 在 agent 内增加统一 `resolve_session()` 内部方法。
2. 逻辑改为：先查内存，再查持久化 store，最后才新建。
3. store 命中后回填 `SessionManager`，避免后续重复 miss。
4. `process_message()` 与 `process_stream()` 共用同一套 session 解析逻辑。
5. `MainAgent.process_message()` 同样走统一 session 解析逻辑。
6. 清理入口层重复的 session 兜底逻辑。

必须新增测试：

- 跨进程恢复 session 后历史仍存在
- 恢复 session 后 workflow stage 保持不变
- `process_stream()` 与 `process_message()` 的恢复行为一致
- 已存在 session_id 不会被重新初始化

验收标准：

- 同一个 session 在重启进程后可继续对话
- `turn_count`、`workflow_stage`、`conversation_history` 不回退

### Milestone 3：把 plugin 接回主链路

目标：

- 让文档承诺的 plugin 生命周期真正生效

涉及模块：

- `dbk/plugins.py`
- `dbk/agent/core.py`
- `dbk/agent/subagent.py`

任务拆分：

1. 重构 prompt 构建流程，统一通过 plugin registry 的 `build_system_prompt()`。
2. 在消息处理结束后调用 `apply_post_message_hooks()`。
3. 明确失败场景也是否触发 post hook，并在文档中写清楚。
4. 检查 cleanup hook 的真实触发位置，补齐缺失接线。
5. 重新核对 `docs/PLUGIN_SYSTEM.md` 与实际行为。

必须新增测试：

- plugin system prompt 确实进入最终 prompt
- post-message hook 在 direct 和 delegated 场景均触发
- hook 抛错不会中断主流程

验收标准：

- plugin 的 system prompt、post-message、cleanup 行为可通过测试验证
- 文档中声明支持的 hook 均已接入主链路

### Milestone 4：把 workflow 从提示词升级为执行策略

目标：

- 让 workflow 成为真正的控制面

前置决策：

- 确定是否保留 `research` 阶段

涉及模块：

- `dbk/agent/state.py`
- `dbk/agent/workflow.py`
- `dbk/agent/core.py`
- `dbk/cli.py`
- `dbk/cli_commands/run.py`
- `docs/详细设计文档.md`
- `docs/SPEC.md`
- `docs/plan.md`

任务拆分：

1. 统一文档与代码中的 stage 枚举。
2. 让 workflow 层负责阶段解析、工具优先级和迁移约束。
3. 将 stage prompt、stage tool routing、transition validation 收口到统一策略对象。
4. 明确“阶段结果”写入 session 的格式。
5. 让 `run_stage()` 与 `run_full_workflow()` 依赖策略对象而不是拼接提示词。

必须新增测试：

- 非法阶段跳转被拦截
- 每阶段工具优先级/适配性可验证
- full workflow 执行后的阶段产物完整落库

验收标准：

- workflow 决定执行行为，不只是描述性提示
- 文档中的阶段模型与实现一致

### Milestone 5：让 memory 参与实时推理

目标：

- 把 memory 从旁路能力变成主链路能力

涉及模块：

- `dbk/agent/memory.py`
- `dbk/agent/core.py`
- `dbk/api_server.py`
- `dbk/agent/repl.py`

任务拆分：

1. 设计统一 prompt assembly pipeline。
2. 将 memory context 注入系统提示或用户前置上下文。
3. 统一 API/REPL 的 memory writeback 逻辑。
4. 明确短期 session state 与长期 memory 的边界。
5. 为 context 长度增加截断策略，避免 prompt 爆炸。

必须新增测试：

- memory context 能进入实际 prompt
- facts/episodes 对下一轮回答可见
- 截断策略不会破坏 prompt 结构

验收标准：

- memory 对 agent 决策链路可观察、可验证
- 不同入口的 memory 行为一致

### Milestone 6：并发与稳定性加固

目标：

- 让 API server 在共享 session 场景下具备基本一致性保障

涉及模块：

- `dbk/api_server.py`
- `dbk/agent/session.py`
- `dbk/agent/core.py`
- `dbk/agent/session_store.py`

任务拆分：

1. 为 session 级别更新引入锁或串行化机制。
2. 明确同 session 并发请求的行为约定。
3. 评估是否保留无锁内存 session map 作为主状态源。
4. 增加并发下的回归测试。

必须新增测试：

- 同一 session 并发写入不会丢 turn
- workflow transition 不被后写覆盖
- store 与内存态不会长期漂移

验收标准：

- 并发 session 修改具备最低一致性保证
- API server 的共享 agent 模式有清晰边界

## 4. 推荐执行顺序

建议顺序如下：

1. Milestone 1：统一执行主链路
2. Milestone 2：修复 session 生命周期
3. Milestone 3：plugin 接回主链路
4. Milestone 4：workflow 执行策略化
5. Milestone 5：memory 接入实时推理
6. Milestone 6：并发与稳定性加固

原因：

- 不先统一 agent 类型，后续所有整改都会重复做两遍
- 不先修 session，workflow 和 memory 的正确性都无法成立
- workflow、memory、plugin 都必须依附同一条主链路

## 5. 迭代建议

### Iteration A

范围：

- Milestone 1
- Milestone 2
- Milestone 3

目标：

- 收敛主链路
- 修掉最危险的状态一致性问题

交付结果：

- 统一 agent factory
- 可靠的 session 恢复
- plugin hooks 主链路接通

### Iteration B

范围：

- Milestone 4
- Milestone 5

目标：

- 让 workflow 与 memory 成为真正架构能力

交付结果：

- workflow policy 化
- prompt assembly pipeline
- memory 实时注入

### Iteration C

范围：

- Milestone 6
- 文档清理
- 回归测试补强

目标：

- 做稳定性和一致性收尾

交付结果：

- 并发模型明确
- 回归测试闭环
- 文档与实现重新对齐

## 6. 风险与依赖

### 风险

- `MainAgent` 替换默认入口后，部分测试可能假设 direct agent 行为
- prompt 组装路径调整后，快照类测试可能需要更新
- session 恢复修正后，现有“自动新建 session”的隐式行为会变化

### 依赖

- 需要先明确 `research` 是否保留
- 需要确认 SDK 是否也必须与 CLI/API 保持完全同构行为

## 7. Definition of Done

本轮架构整改完成的定义：

1. 所有用户入口都走统一 agent 主链路。
2. session 恢复、workflow stage、memory context 在跨入口下行为一致。
3. plugin 文档中的稳定 hook 都在生产主链路中真实触发。
4. workflow 不再只是提示词拼接，而是可测试的执行策略。
5. 同一 session 的并发写入具备明确的一致性保障。
6. `docs/详细设计文档.md`、`docs/SPEC.md`、`docs/PLUGIN_SYSTEM.md` 与代码现状一致。

## 8. 下一步

如果按这个计划执行，第一批实际代码改动建议从下面 5 件事开始：

1. 引入统一 agent factory，并替换 API / CLI / REPL / SDK 的实例化路径。
2. 实现 `resolve_session()`，接入 `process_message()` 和 `process_stream()`。
3. 接通 plugin 的 system prompt 和 post-message hooks。
4. 明确并修正文档中的 workflow stage 集合。
5. 补第一批 session 恢复和 hook 接线测试。

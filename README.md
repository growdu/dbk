# DBK MVP

Database Kernel Agent CLI 的最小可用实现，覆盖：

* runtime metrics 采集与存储（sqlite）
* eBPF profile 调度（支持模拟执行）
* 延迟故障诊断与 RCA 证据包输出

## 开发计划（当前已完成 MVP）

1. 初始化 CLI 与项目结构
2. 实现 runtime collector + sqlite 存储
3. 实现 trace profile 与 artifact pipeline
4. 实现 latency incident 诊断与 runbook
5. 完成基础单元测试与本地验证

## 快速开始

```bash
python3 -m dbk.cli init
python3 -m dbk.cli collect --instance pg-main-01
python3 -m dbk.cli metrics --metric query.p95_latency_ms --instance pg-main-01
python3 -m dbk.cli trace profiles
python3 -m dbk.cli trace run --profile cpu-hotpath --task-id demo-1 --duration 30
python3 -m dbk.cli diagnose latency --instance pg-main-01 --task-id incident-1
```

输出将写入：

* `.dbk/runtime.sqlite`
* `.dbk/artifacts/runtime/<task_id>/...`

## 测试

```bash
python3 -m pytest -q
```

## 当前限制

* collector 仅实现 `mock` 数据源（尚未接入真实 PostgreSQL 连接）
* trace 默认为模拟执行；`--execute` 仅在本机具备 `bpftrace` 时生效


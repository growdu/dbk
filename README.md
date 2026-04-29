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
python3 -m dbk.cli collect health --source mock
python3 -m dbk.cli collect health --source pgstat --dsn "postgresql://user:pass@127.0.0.1:5432/postgres"
python3 -m dbk.cli collect --source pgstat --instance pg-main-01 --dsn "postgresql://user:pass@127.0.0.1:5432/postgres"
python3 -m dbk.cli collect daemon start --source mock --instance pg-main-01 --interval-sec 15
python3 -m dbk.cli collect daemon start --source mock --instance pg-crit --interval-sec 5 --priority 90 --tags prod,critical --max-running 3 --preempt-lower-priority
python3 -m dbk.cli collect daemon status
python3 -m dbk.cli collect daemon list --tag prod --source mock --instance-pattern "pg-*" --min-priority 80
python3 -m dbk.cli collect daemon stop --instance pg-main-01
python3 -m dbk.cli collect daemon stop --all
python3 -m dbk.cli collect daemon stop
python3 -m dbk.cli metrics --metric query.p95_latency_ms --instance pg-main-01
python3 -m dbk.cli runtime cleanup --older-than-hours 168 --dry-run
python3 -m dbk.cli runtime cleanup --older-than-hours 168 --vacuum
python3 -m dbk.cli runtime cleanup-daemon start --interval-sec 3600 --older-than-hours 168
python3 -m dbk.cli runtime cleanup-daemon status
python3 -m dbk.cli runtime cleanup-report --limit 50 --window-hours 24
python3 -m dbk.cli runtime cleanup-daemon stop
python3 -m dbk.cli trace profiles
python3 -m dbk.cli trace run --profile cpu-hotpath --task-id demo-1 --duration 30
python3 -m dbk.cli trace run --profile io-latency --task-id demo-2 --duration 20 --execute --approve-privileged
python3 -m dbk.cli diagnose latency --instance pg-main-01 --task-id incident-1
python3 -m dbk.cli diagnose latency --instance pg-main-01 --task-id incident-2 --thresholds-file ./thresholds.example.json
```

输出将写入：

* `.dbk/runtime.sqlite`
* `.dbk/artifacts/runtime/<task_id>/...`

## 测试

```bash
python3 -m pytest -q
```

Docker 集成测试（默认跳过）：

```bash
DBK_RUN_DOCKER_TESTS=1 DBK_PG_DOCKER_VERSIONS=14,15,16 python3 -m pytest -q tests/test_pg_integration_docker.py
```

## PostgreSQL 兼容矩阵

| PG 版本 | `pg_stat_statements` | `pg_stat_io` | `query.p95_latency_ms` 来源 | `io.read_latency_ms` 来源 |
| --- | --- | --- | --- | --- |
| 14 | 可选扩展 | 不支持 | `pg_stat_statements` 或 `pg_stat_activity` 回退 | 不可用（置 0 + warning） |
| 15 | 可选扩展 | 不支持 | `pg_stat_statements` 或 `pg_stat_activity` 回退 | 不可用（置 0 + warning） |
| 16+ | 可选扩展 | 支持 | `pg_stat_statements` 或 `pg_stat_activity` 回退 | `pg_stat_io`（不可用时回退 0） |

## 当前限制

* PostgreSQL 采集依赖 `psycopg`；未安装时会提示并退出
* `pg_stat_statements` / `pg_stat_io` 等视图缺失时，会记录 warning 并以 0 填充该指标
* trace 默认为模拟执行；`--execute` 需要同时传 `--approve-privileged`
* `--execute` 模式的 trace 时长上限为 60 秒，且非 root 用户会自动降级为模拟
* 后台采集当前是“每实例一个守护进程”模型，支持标签、`--max-running` 与优先级抢占
* 自动清理守护进程当前为单实例（全局）任务模型

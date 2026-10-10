# 多活跃 Run 并发压力验收（P1 资源校准）

日期：2026-10-11（Asia/Shanghai）。对应 [#21](https://github.com/kksty/HuntWeave/issues/21)。分支 `codex/21-concurrency-acceptance`，自 `origin/main` `7c56643` 切出。本记录覆盖：多活跃 Run 之间的物理执行额度、同 IP 串行、背压与控制路径可用性、每 Run 事件游标独立性、重启接续与重复唤醒幂等，以及 P1 规格 §3.7 要的实际容量与延迟数字。

真实执行仍未开放（`real_execution_ready=false`，ADR-0010 四项门槛中 profile 复验与回退入口两项未满足），因此「真实执行下实测」的部分在本记录中**逐条标为受阻**，不标为通过。本轮验证的是**同一控制面机制**在假执行 profile 下的行为；真假执行端共用同一调用账本、同一租约与同一取消契约（验证记录 `0013`、`0018`）。

## 环境与入口

| 项 | 值 |
| --- | --- |
| 宿主 | Windows 11 x86_64，Docker Desktop / WSL2 |
| 解释器 | `D:\code\HuntWeave\backend\.venv\Scripts\python.exe`（Python 3.12.14，pytest 9.1.1，ruff、mypy 同环境） |
| 工作目录 | `D:\code\huntweave-wt\21-concurrency-acceptance`（worktree）；pytest 的 `pythonpath` 按 rootdir 解析，命令一律在 `backend/` 内执行 |
| 一次性数据库 | 独立 Compose 项目 `huntweave-i21-concurrency`，只起 `postgres`（`postgres:17-bookworm` 固定 digest），卷与网络按项目名前缀隔离 |
| 日常项目 | `huntweave`（`huntweave-app-1` / `huntweave-runner-1` / `huntweave-postgres-1`）本轮**未** up/down/stop/restart，未触碰其卷与网络 |
| 目标 | 全部为文档保留地址（`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`）与固定假执行；**未连接任何外部目标**，未启动任何工具容器 |

数据库与依赖的准备命令（`deploy/compose.isolated-db.yaml` 是本次新增的入口，只发布回环端口）：

```powershell
$env:HUNTWEAVE_ISOLATED_DB_PORT = "18400"   # 已用 Get-NetTCPConnection/Test-NetConnection 确认未被占用
docker compose --project-name huntweave-i21-concurrency `
  -f deploy/compose.yaml -f deploy/compose.isolated-db.yaml up -d --wait --wait-timeout 120 postgres

cd backend
$env:HUNTWEAVE_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:18400/huntweave"
$env:HUNTWEAVE_APP_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\app_db_password"
$env:HUNTWEAVE_CHECKPOINT_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\checkpoint_db_password"
$env:HUNTWEAVE_MIGRATOR_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\migrator_db_password"
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
$env:PYTHONPATH = "D:\code\huntweave-wt\21-concurrency-acceptance\backend\src"
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m huntweave.storage.migrate
# {"event":"migrations_complete"}   —— 迁移链 head 仍是 0008_retention_decisions，本票未新增迁移
```

`compose.isolated-db.yaml` 额外挂了一个非 internal 网络：基础文件里 `database` 是 `internal: true`，internal-only 网络上发布端口不会被接上（实测 `HostConfig.PortBindings` 已写入而 `NetworkSettings.Ports` 为空），补一个空网络才让回环绑定真实生效。

## 实施评审结论（对照实际代码）

| 验收项需要的实现 | 评审结论与代码证据 |
| --- | --- |
| 全局物理执行额度 | **原本缺失**。`contracts/runs.py:64` 的 `Budget.max_concurrency`（默认 4）**没有任何执行路径消费**：`grep -rn "max_concurrency" backend/src` 只有该定义处一处命中。`plan()` 只检查 `BudgetReservation` 计数、输出字节与墙钟预算，没有任何跨 Run 的物理占用记账 |
| 同 IP 主动执行串行 | **原本缺失**。`plan()` 在 `resolve_target()` 绑定目标后直接创建 `ToolCall`，全程不查询同一 `target_ip` 上是否已有在飞调用；`grep` 在 `backend/src` 中找不到任何按目标地址串行化的代码 |
| 「一个持有长调用的 Run 不饿死其他 Run」 | **原本不成立**。`claim()` 按 `Run.created_at` 升序取候选并返回**第一个**匹配者；而「持有在飞调用」并不使 Run 失去领取资格（旧代码只在 `run.status == "waiting" and not self._active(...)` 时 `continue`）。单进程单轮次因此每轮都被最老的那个 Run 领走，它自己的 `plan()` 又因有在飞调用返回 `None` —— 轮次花在一个动不了的 Run 上，后面的 Run 永远轮不到 |
| 事件游标按 Run 独立 | **原本已实现**。`runs/events.py` 取 `EventCursor` 行时 `with_for_update=True`，游标行主键是 `run_id`；`history()` 在 `after > cursor` 时返回 `event_cursor_ahead` 409 |
| 重复唤醒幂等 | **原本有缺口**。`source_event_id` 判重发生在取游标行锁**之前**（先 `SELECT` 判重、再 `session.get(..., with_for_update=True)`），两个并发到达的重复唤醒会双双读到「没有」，第二个撞 `(run_id, source_event_id)` 唯一约束报错 |
| 「可信停止才归还额度」 | **原本不可实现**。`runs/dispatch.py::_settle_undispatched` 为「账本确认从未接受」的调用写 observation，但**没写 `lease_active`**；按停止确认规则（`process_active/connection_open/lease_active` 三者都必须为 `False`）这条本身可信的「从未启动」事实会被读成「停止未确认」 |
| `execution/ledger.py` 全量序列化 | **确认存在，本票不处理**。`_persist()` 每次保存都 `json.dumps(self.data)` 全量写整份账本；这是 #32 的三个单进程热点之一，见「与 #32 的边界」 |

## 实现改动

| 改动点 | 行为 |
| --- | --- |
| `contracts/resources.py`（新增） | 版本化资源政策：`RESOURCE_POLICY_VERSION`、`ResourcePolicy`（控制派发 4 / 全局物理 4 / 同 IP 1，可由 `HUNTWEAVE_*_SLOTS` 覆盖，非法值拒绝而不是夹取）、`ExecutionQuota`、`SlotWait`、`slot_wait()`（先判全局、再按**规范排序**逐个检查全部实际目标）、`stop_confirmed()`/`holds_physical_slot()`（唯一一份停止规则）、以及 `LIVE_SLOT_SQL`（配额查询用的同一条规则的 SQL 拼写）。同一模块载有验收阈值 `ConcurrencyAcceptanceThresholds` 与 `ACCEPTANCE_THRESHOLDS_VERSION` |
| `contracts/runs.py` | 新增 `RESOURCE_POLICY_VERSION = 1`；`ScopeSnapshot` 增加 `resource_policy_version` 字段（默认取该常量），运行前随授权快照锁定，改数值须改版本 |
| `runs/orchestration.py` | `claim()` 的候选集合在 SQL 层排除「已有非终态调用」的 Run（`NOT EXISTS`），持有在飞调用的 Run 不再独占调度轮次；`plan()` 在创建 `ToolCall` 的同一短事务内、`BudgetReservation`/`Outbox` **之前**取 `pg_advisory_xact_lock(class, key)`（先全局键、再按排序取目标键）并统计 `tool_calls` 上的物理占用，超限则只写一条 `execution_backpressure` 事件后返回 `None`（背压，不挂起 Run、不领取任何东西）；同时把「决策已提交但调用未预留」变成**可续跑**：同一决策在后续轮次继续尝试预留，而不是被一条无法完成的决策卡死。`_stop_confirmed()` 改为委派 `contracts/resources.py`，停止规则只有一份 |
| `runs/dispatch.py` | `_settle_undispatched` 的 observation 补上 `lease_active=False`：账本明确「没有这个 call_id」即证明不可能有控制租约，这是 §7 允许归还额度的那一类预留 |
| `runs/events.py` | 先 `INSERT ... ON CONFLICT (run_id) DO NOTHING` 建行、再 `FOR UPDATE` 取锁（`populate_existing=True` 保证锁真的取到），**然后**才判 `source` 去重；并发重复唤醒退化为「第二条看到第一条已提交，什么都不做」 |
| `tests/conftest.py`（新增） | `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 时，每个检查前清空 Run 相关业务表（`access_key_state`、`web_sessions` 与未建模的 `runtime_processes` 除外，它们属于部署而不属于夹具）。理由见下节：物理额度现在是夹具状态 |
| `tests/test_orchestration_integration.py` | `settle()` 夹具补上真实账本会写的停止事实（`started=True`、进程/连接/租约为 `False`），否则它模拟的是一条「已结算但停止未确认」的调用，而不是一条正常结束的调用 |

**为什么需要 `conftest.py`。** 物理额度从调用本身推导：一个调用只有在执行端确认「它启动的东西都不在了」时才归还额度。于是一个只记录调用、从不交给执行端的检查会**真的占住一个额度**——这正是真实部署里的行为。旧套件是「只追加」的，没有任何清理，同一轮里累积到 4 条就把后续检查挡在背压后面（实测：旧夹具下 `test_a_multi_target_...` 直接失败）。可丢弃数据库是这一约束的正当出口，README 也早已声明「集成检查会清空其测试数据库中的演示表」。

## 实际命令与完整输出

### 三项本地检查（在 `backend/` 内）

```
& <venv>\python.exe -m pytest -m "not integration" -q
277 passed, 8 skipped, 53 deselected in 31.09s

& <venv>\python.exe -m ruff check src tests
All checks passed!

& <venv>\python.exe -m mypy --config-file pyproject.toml src
Success: no issues found in 51 source files
```

基线对照（同一 HEAD `7c56643` 的 `git archive` 副本，排除本票改动）：`253 passed, 8 skipped, 41 deselected`。本票净增 **24 项非集成检查**（`tests/test_execution_quota.py`）、**12 项集成检查**（`test_concurrency_integration.py` 11 + `test_concurrency_stress.py` 1），源文件 50 → 51。

其中 8 项跳过含 7 项「需要可丢弃数据库」的既有编排集成检查与 1 项「本次工作树没有前端产物」的控制台 bundle 检查。

### 完整后端套件（一次性数据库）

```
& <venv>\python.exe -m pytest tests --ignore=tests/test_startup_integration.py -q
332 passed, 1 skipped in 105.24s (0:01:45)
```

`test_startup_integration.py`（6 项，需要活动 app/Runner 容器）本轮**未**运行，见「未达成与限制」。

### 压力夹具（可重复运行）

```powershell
cd backend
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
& <venv>\python.exe -m pytest tests/test_concurrency_stress.py -q -s
# 1 passed in 29.81s   —— 报告为 JSON，打印在 stdout
```

夹具同时放大两个维度：**活跃调用数**（1/2/4/5 个 Run 各持一条在飞调用；5 是默认额度下第一个无法全部放行的数量）与**历史账本量**（0 / 2 000 / 20 000 条已结算调用，均由夹具直接写入，且都带可信停止事实因而不占额度）。每个组合测 15 次取中位数与 p95。可用 `HUNTWEAVE_STRESS_LEDGER_SIZES` / `HUNTWEAVE_STRESS_ACTIVE_COUNTS` / `HUNTWEAVE_STRESS_ITERATIONS` 缩放。

## 实测容量与延迟（P1 规格 §3.7 的校准结果）

策略版本 `policy_version=1`，阈值版本 `thresholds_version=1`，全局物理额度 4、同 IP 1、控制派发 4。单位毫秒。

| 操作 | 历史账本 | 活跃调用 | 中位数 | p95 | 最大 |
| --- | --- | --- | --- | --- | --- |
| 调度轮次 `claim()` | 0 | 1 / 2 / 4 | 2.99 / 2.98 / 7.44 | 3.97 / 3.31 / 8.76 | 3.97 / 3.31 / 8.76 |
| 调度轮次 `claim()` | 2 000 | 1 / 2 / 4 | 7.45 / 8.29 / 6.55 | 9.06 / 9.86 / 7.23 | 9.06 / 9.86 / 7.23 |
| 调度轮次 `claim()` | 20 000 | 1 / 2 / 4 | 7.94 / 7.79 / 6.70 | 9.47 / 10.29 / 8.07 | 9.47 / 10.29 / 8.07 |
| 额度查询（读 `tool_calls`） | 0 | 4 | 2.03 | 2.63 | 2.63 |
| 额度查询 | 2 000 | 4 | 2.33 | 3.48 | 3.48 |
| 额度查询 | 20 000 | 4 | 4.99 | 8.42 | 8.42 |
| 满额度下的计划尝试（含 claim+快照） | 0 / 2 000 / 20 000 | 4 | 39.87 / 35.31 / 42.54 | 52.74 / 57.37 / 60.79 | 52.74 / 57.37 / 60.79 |
| 取消 `control(cancel)` | 0 / 2 000 / 20 000 | 4 | 18.39 / 20.26 / 21.34 | 20.39 / 23.56 / 22.50 | 20.39 / 23.56 / 22.50 |
| 续租 `dispatcher.reconcile` | 0 / 2 000 / 20 000 | 4 | 18.26 / 22.83 / 20.68 | 19.92 / 26.95 / 23.24 | 19.92 / 26.95 / 23.24 |
| 对账扫描 `dispatcher.sweep(20)` | 0 / 2 000 / 20 000 | 4 | 79.45 / 85.95 / 96.77 | 82.76 / 103.62 / 160.42 | 82.76 / 103.62 / 160.42 |
| 结果结算（含 2 条证据） | 0 / 2 000 / 20 000 | 2 | 29.71 / 26.96 / 28.26 | 37.01 / 36.65 / 33.07 | 37.01 / 36.65 / 33.07 |
| 证据归档写入 64 KiB | 0 / 2 000 / 20 000 | 2 | 8.29 / 9.15 / 8.98 | 9.19 / 9.99 / 11.13 | 9.19 / 9.99 / 11.13 |
| 额度预留等待 advisory 锁 | 0 / 2 000 / 20 000 | 4 | 187.4 / 190.1 / 186.5 | — | — |

最后一行是设计出来的对照：持有方拿住全局额度键 200 ms 后释放，测得值已扣掉同库无争用时的预留耗时，所以它读到的就是锁等待本身（200 ms 持有 → 186–190 ms 等待，即等待几乎等于持有时间，没有额外串行放大）。

**容量结论（控制面）**：默认政策下，4 个 Run 各持一条在飞调用时全部放行；第 5 个 Run 即使目标地址完全不同也被拒绝（全局额度），且拒绝不领取任何东西（无 `ToolCall`、无 `BudgetReservation`、无 outbox 行）。同一地址上第二个 Run 的调用被拒绝，直到第一个调用的停止被确认。

**增长**：额度查询的中位耗时从 2.0 ms（账本为空）到 4.99 ms（20 000 条），即约 **0.15 ms / 1000 条历史调用**（阈值 2.0 ms / 1000 条）；夹具只对这一项做增长断言。调度轮次从 3.0 ms（账本为空）升到约 7–8 ms（2 000 条及以上）后在 2 000 与 20 000 条之间基本平坦，与账本量不成线性——它更像是一次计划选择的变化（候选 Run 数、活跃调用数都会影响它），而不是逐行扫描；真正随账本线性增长的是额度查询那一项。这些是本机单点观测，不构成吞吐承诺。

**两项硬零**：`duplicate_actions = 0`（在飞调用期间没有任何 Run 产生第二条调用）、`wrong_releases = 0`（没有任何额度在缺少可信停止事实时被归还）。控制路径最坏值 160.4 ms（对账扫描），远低于 `control_path_seconds = 5.0`。

## 逐条验收判定

### 第一组

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 每轮一个领取 + 轮转对账窗口；一个卡住或未核对的 Run 不饿死其他 Run | **通过（控制面）** | `claim()` 每轮只返回一个 Run（既有）；新增 `tests/test_concurrency_integration.py::test_a_run_holding_a_call_yields_the_turn_to_the_other_runs`（三个 Run 依次各被领取一次）、`::test_a_run_that_cannot_be_reconciled_does_not_starve_the_others`；既有 `test_orchestration_integration.py::test_run_awaiting_reconciliation_never_starves_the_single_scheduler` 仍通过。轮转对账窗口沿用 `agentd` 既有的 `SWEEP_LIMIT=20` + `sweep_offset` |
| 2 | 同 IP 主动执行串行；全局工具执行并发与预算不超限；并发完成判定不会刚宣布完成又发起动作 | **通过（控制面）** | `::test_one_address_runs_one_active_action_across_runs`（跨 Run 同地址串行，且不挂起第二个 Run）、`::test_a_full_global_pool_backpressures_new_target_execution`（不超全局额度）、`::test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does`、压力夹具 `duplicate_actions=0`。真实执行下的时序未实测（见 #10） |
| 3 | 每个 Run 的事件游标相对独立，不越过未发布事件；重启后按持久状态接续并幂等处理重复唤醒 | **通过** | `::test_each_run_keeps_its_own_event_cursor_and_a_reader_cannot_pass_it`（两 Run 交替追加，游标各为 1..n 稠密、互不串流；`after > cursor` 给 `event_cursor_ahead` 409）、`::test_a_duplicate_wakeup_lands_once_even_when_both_arrive_at_the_same_time`（8 线程并发同源唤醒只落一条事件）、`::test_a_restart_resumes_from_the_durable_records_and_reserves_no_second_call`（新 scheduler 重新取得租约代次，重放同一轮次不产生第二条调用） |
| 4 | 记录实测容量与延迟数据，作为 P1 规格 §3.7 校准的实际结果 | **部分通过** | 控制面容量与延迟已实测并记于本记录（上一节的方法与数字）。**真实执行下的容量与延迟受阻**：`real_execution_ready=false`（ADR-0010 四项门槛中 `profile_unvalidated` 与 `revert_path_missing` 未满足），产品拒绝创建真实 Run，因此无法在真实执行下取数 |
| 5 | 基础多 Run 公平与取消检查随 #17/#18 提前验收，最终的容量与延迟校准留在本条 | 基础公平与取消：**通过**；最终容量与延迟校准：**部分通过**（同 #4） | 公平：见 #1；取消：`::test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held` 与压力夹具的 `cancel` 一列 |

### 第二组（2026-10-10 同步 0.9.5 设计）

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 6 | 全局物理额度（默认 4）与同 IP 上限（默认 1，跨 Run、含受控复现）同时生效；并发记账不按主锚点只锁一个 IP，多目标调用检查全部实际目标 | **通过（控制面）／一项受限** | 全局与同 IP 同时生效：`::test_one_address_runs_one_active_action_across_runs`、`::test_a_full_global_pool_backpressures_new_target_execution`；受控复现（重派）走同一处门控：`plan()` 在 `replacing` 路径之后、创建 `ToolCall` 之前检查，`slot_wait()` 遍历 `_actual_targets()`。**受限**：当前票据只绑定一个 `target_ip`，`_actual_targets()` 目前只返回该地址；「检查全部实际目标」是为未来多目标票据预留的接缝，本票没有可表达的多目标调用可验。真实执行下的实测受阻（同 #10） |
| 7 | 停止未确认继续占用全局与同 IP 额度；结果结算、超时或租约到期均不释放物理额度，只有可信停止事实才归还；结果 `unknown` 但停止已确认可释放额度，核对结果另行保留 | **通过（控制面）** | `::test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does`（结算后仍占 1，补上可信停止事实后才归零，且状态仍是 `succeeded`）、`::test_an_unknown_outcome_with_a_confirmed_stop_returns_capacity_and_keeps_reconciling`（额度归零、调用仍 `unknown`、Run 仍 `execution_unknown`、`preview` 仍报 `execution_reconciliation_required`、仍在 `reconcilable_runs()` 中）、`::test_a_refused_intent_releases_the_slot_it_reserved`（账本确认从未接受的预留归还额度）、`tests/test_execution_quota.py` 的 24 项纯检查逐格覆盖停止规则 |
| 8 | 物理额度耗尽时新目标执行背压，但取消、核对与回收控制路径仍须可用；不得通过超额启动其他 IP 消除等待，也不承诺每 Run 保底一槽 | **通过（控制面）** | 背压：`::test_a_full_global_pool_backpressures_new_target_execution`（Run 仍 `running`、`reason_code` 为空、无任何领取）；控制路径可用：`::test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held`（4 个额度全被占时取消被接受、结算入库、待对账列表仍可用）与压力夹具在满额度下测得的 `cancel`/`renew`/`sweep`/`settle_with_evidence` 全部为毫秒级。回收路径（`runs/retention.py`）本轮未改，也没有任何「按 Run 保底一槽」的逻辑。**控制派发槽的现状要说清**：`ResourcePolicy.control_dispatch_slots = 4` 记录了 ADR-0016 的设计值，但本代码库里取消/续租/对账/结算都是直接调用，不经过任何派发队列，因此没有可强制的派发槽；「控制路径不被物理额度挡住」这条性质由上面的实测与检查直接证明，而不是由该字段强制 |
| 9 | 阈值（响应、等待、增长）在运行前锁定并版本化，不接受事后移动标准；`unknown` 误重试必须为 0，且无错误资源释放 | **通过** | 阈值在 `backend/src/huntweave/contracts/resources.py::ConcurrencyAcceptanceThresholds`，字段 `thresholds_version`（`ACCEPTANCE_THRESHOLDS_VERSION = 1`）；压力夹具把该版本与 `policy_version` 一并打印在报告里，本记录引用同一版本。实测 `duplicate_actions = 0`、`wrong_releases = 0`，与 `unknown_retries_max = 0`、`wrong_releases_max = 0` 一致 |
| 10 | 本条只验真实执行下的额度、背压与控制可用；完整负载矩阵与 24 小时墙钟两格归 P2-E | **受阻** | **阻塞于 ADR-0010 就绪门槛**：`real_execution_ready=false`，其中 `profile_revalidation`（`profile_unvalidated`）与 `deployment_revert`（`revert_path_missing`）未满足，`RunService.create_run` 对非假执行 profile 直接抛 `real_execution_not_ready`。因此「真实执行下的额度、背压与控制可用」无法实测。**复验条件**：四项门槛全部满足、`real_execution_ready=true` 后，用同一压力夹具把 Run 的执行 profile 换成 `real-lab-v1`（夹具的 `OrchestrationService(policy=...)` 与 `Runner` 桩改成真实 Runner 客户端）复跑，并把数字补进本记录的新增小节。完整负载矩阵（每 Run 1/10/50/100 IP × 1/2/4/5 活跃 Run、模型等待注入、准备快/慢/失败、目标快/慢/超时）与 24 小时墙钟两格**不在本条**，归 P2-E（`docs/specs/0003-agent-research.md` §9.4） |

## 受阻项与复验条件汇总

| 受阻项 | 阻塞于 | 复验条件 |
| --- | --- | --- |
| 真实执行下的容量与延迟取数（#4、#5） | `real_execution_ready=false`：ADR-0010 门槛 1（profile 复验，`profile_unvalidated`）与门槛 4（回退入口，`revert_path_missing`） | 四项门槛全部满足后，用本记录给出的同一夹具命令在真实 profile 下复跑，数字补入本记录新增小节 |
| 真实执行下的额度、背压与控制可用（#6、#7、#8、#10） | 同上 | 同上；届时还需补上「同 IP 串行在真实目标上确实没有并发连接」的观察，本轮的证据只是控制面不发出第二条调用 |
| 多目标调用检查全部实际目标（#6） | 票据契约：`ExecutionRequest` 只带一个 `target_ip`/`target_port` | 出现多目标票据后，`_actual_targets()` 返回全部地址，`slot_wait()` 与锁顺序无需改动即按该列表逐个检查；届时补一条覆盖多目标拒绝的检查 |
| 完整负载矩阵与 24 小时墙钟（#10） | 不在本条范围 | 归 P2-E（`0003` §9.4） |

## 已知限制

- **真实执行未开放**：本记录中所有数量与延迟都在假执行 profile 下取得。假执行端与真实执行端共用同一调用账本、同一控制租约与同一取消/续跑路径（`execution/ledger.py`、验证记录 `0013`/`0018`），但真实目标连接、真实进程与真实证据归档的时序**未**被测。
- **未确认停止确实会长期占用额度**。一个本地状态为已派发、而账本查不到该 `call_id` 的调用会走 `unknown()`，其 observation 保持 `None`；`reconcile()` 的 `not_executed` 裁定又要求账本给出可证事实。因此这条路径上「额度不归还」是规格要求（§7「停止未确认继续占用」），但没有操作员可用的绕过手段：恢复条件是执行端账本重新给出答案。这是设计要的背压，也是该设计最需要被监视的一处容量损失。验证记录 `0014` 已登记控制台尚未消费该区分，队列面板归 P2-B（#57）。
- **背压只按决策记一次**。`execution_backpressure` 事件用 `decision_id + ":backpressure"` 作为 `source_event_id` 去重，所以同一决策的等待只留一条事件，等待**时长**不落库。等待类别、时长与占用者的实时展示属于队列面板（ADR-0016「队列可解释性」），归 P2-B（#57）。
- **额度查询是全表扫描，本票没有加索引**。`_execution_quota()` 按 `LIVE_SLOT_SQL` 统计 `tool_calls`。本票另做了一次一次性探针（不属于夹具）：在临时表上造 1 000 000 行、其中恰好 4 行是持有者，并建一条以同一谓词为条件的部分索引——

  ```sql
  CREATE INDEX ix_probe_live ON probe.t (((ticket->>'target_ip')))
    WHERE (observation IS NULL
       OR observation->>'process_active' IS DISTINCT FROM 'false'
       OR observation->>'connection_open' IS DISTINCT FROM 'false'
       OR observation->>'lease_active' IS DISTINCT FROM 'false');
  ANALYZE probe.t;
  EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM probe.t WHERE <同一谓词>;
  ```

  结果是 `Parallel Seq Scan`（约 55 ms），索引未被选中：裸 `count(*)` 上的 index-only scan 无法并行，而规划器对谓词的选择率估计远高于实际，于是并行顺序扫描仍然更便宜。所以一个「只为这次统计」的部分索引不会被用上，本票因此**没有**新增索引或迁移，只把增长曲线测出来（0.15 ms / 1000 行 @ ≤20 000 行）。是否需要为此改结构是 #32 的题目。
- **压力夹具用直写 SQL 造历史账本**，只覆盖会被调度读到的表（`runs`/`research_tasks`/`agent_sessions`/`decisions`/`tool_calls`/`budget_reservations`/`audit_events`/`event_cursors`），不代表真实证据归档的体积；证据写入用真实的 `EvidenceArchive` 单独测量。
- **未运行项**：`tests/test_startup_integration.py`（6 项，需要活动的 app/Runner 容器）与 `lab/` 四份探针（动作 18、生命周期 29、出口 30、保留 10）本轮未复跑——本票的接缝在控制面事务与调度选择，不涉及容器生命周期与出口规则。
- **未做前端与浏览器验收**：按 ADR-0018，本轮是后端切片；控制台没有新增页面或字段，队列等待类别的前端呈现归 P2-B。
- **单点观测**：全部数字来自一台 Windows 11 + Docker Desktop/WSL2 宿主与一个 `postgres:17-bookworm` 容器；没有原生 Linux 宿主对照，也没有重复多轮取分布（压力夹具的 15 次迭代是同一进程内的重复）。

## 与 #32 的边界

- #32 测的是**单进程内部的三个热点**：`api/agentd.py` 的顺序推进、`execution/ledger.py::_persist()` 的全量序列化、`runs/events.py` 的游标行锁，口径是**成本/吞吐曲线**，结论形式是「调参数还是改结构」。
- 本票测的是**多活跃 Run 之间**的额度、串行、公平、背压与控制路径可用性，口径是**并发正确性与校准值**。
- 本票**复用**了 #32 需要的压力夹具与账本规模维度（`tests/test_concurrency_stress.py` 的「历史账本量」一维），但**不替 #32 下「是否需要结构改动」的结论**：本记录只给出实测增长（额度查询 0.15 ms / 1000 行、无索引可用），并把「要不要为此改结构」明确留给 #32。本票也**没有**声称完成 #32，没有优化 `_persist()` 或 `agentd` 的推进方式。

## 迁移与共享面影响

- **未新增、未修改任何 Alembic 迁移**。迁移链 head 仍是 `0008_retention_decisions`（`down_revision = "0007_drop_login_throttle"`），`storage/database.py::BUSINESS_REVISIONS` 未改，合入时**无需**迁移编号顺序核对。新增的 `ON CONFLICT (run_id) DO NOTHING` 依赖 `event_cursors` 既有主键，新增的额度统计只读 `tool_calls` 既有列（`status`、`ticket`、`observation`）。
- **`contracts/`（共享面）**：新增 `contracts/resources.py`（新文件，只被 `runs/` 与检查消费）；修改 `contracts/runs.py`（新增 `RESOURCE_POLICY_VERSION` 常量与 `ScopeSnapshot.resource_policy_version` 字段，二者都带默认值，旧快照仍可校验）。这是本票唯一动到共享契约的地方，其他并行分支若同时改 `ScopeSnapshot` 或政策版本常量会冲突，需按 `docs/agents/p2-execution-batches.md` 第 4 节串行合入。
- **`api/` 装配**：**未改**。`OrchestrationService.__init__` 新增的 `policy` 是带默认值的关键字参数，`api/agentd.py` 与 `api/app.py` 的既有装配无需改动。未新增路由，未新增原因码（因此 `tests/test_reason_codes.py` 不需要改，等待类别走 `waiting_category` 字段而不是 `reason_code`）。
- **`runs/`**：`orchestration.py`（`claim()` 候选过滤、`plan()` 额度门控与可续跑决策、新增 `_execution_quota`/`execution_quota`/`_lock_execution_slots`/`_actual_targets`、`_stop_confirmed` 改为委派）、`events.py`（取锁与判重的顺序）、`dispatch.py`（一处 observation 补字段）。`orchestration.py` 是最容易与其他并行分支冲突的文件。
- **`deploy/`**：新增 `compose.isolated-db.yaml`（可选覆盖文件，只在显式独立项目名 + 显式 `-f` 时生效，不影响日常项目）。
- **`backend/tests/conftest.py`（新文件，影响面最大）**：它给**每一个** `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 下的检查加了「先清空 Run 相关表」的 autouse 夹具。其他并行分支若新增依赖跨检查保留业务数据的集成检查，会与本夹具冲突，需要改用各自的显式夹具。

## 待人工确认

- 本票**未** push、**未**关闭 Issue、**未**合并到 `main`；分支 `codex/21-concurrency-acceptance` 上的提交由总协调人决定合入顺序。
- 上面「受阻项」的四条中，前两条取决于 ADR-0010 的就绪门槛何时满足；第三条取决于票据契约是否会出现多目标调用。请确认这三条是否按本记录的口径留在 #21（而不是另开票）。

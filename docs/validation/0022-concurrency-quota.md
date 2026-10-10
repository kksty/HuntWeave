# 多活跃 Run 并发压力验收（P1 资源校准）

日期：2026-10-11（Asia/Shanghai）。对应 [#21](https://github.com/kksty/HuntWeave/issues/21)。分支 `codex/21-concurrency-acceptance`，自 `7c56643` 切出，经协调人 rebase 到 `3ca3684`（含 #37/#33）后本票提交落在其上。本记录覆盖：多活跃 Run 之间的物理执行额度、同 IP 串行、背压与控制路径可用性、每 Run 事件游标独立性、重启接续与重复唤醒幂等，以及 P1 规格 §3.7 要的实际容量与延迟数字。

真实执行仍未开放（`real_execution_ready=false`，ADR-0010 四项门槛中 profile 复验与回退入口两项未满足），因此「真实执行下实测」的部分在本记录中**逐条标为受阻**，不标为通过。本轮验证的是**同一控制面机制**在假执行 profile 下的行为；真假执行端共用同一调用账本、同一租约与同一取消契约（验证记录 `0013`、`0018`）。

本记录经**独立两轴评审**（Standards / Spec）后修订。首稿的结论**没有被静默改写**：判定与数字的改动、以及评审指出的每一处缺陷，都记在文末的[「本记录的更正」](#本记录的更正)。

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
# 缺这个变量时 Compose 直接失败，不会把日常项目的库发布到宿主：
#   error while interpolating services.postgres.ports.[]: required variable
#   HUNTWEAVE_ISOLATED_DB_PORT is missing a value
$env:HUNTWEAVE_ISOLATED_DB_PORT = "18400"   # 已用 Get-NetTCPConnection/Test-NetConnection 确认未被占用
docker compose --project-name huntweave-i21-concurrency `
  -f deploy/compose.yaml -f deploy/compose.isolated-db.yaml up -d --wait --wait-timeout 120 postgres

cd backend
# 秘密文件是每个部署自己的（`/runtime/` 被忽略，独立 worktree 需要自己那一份）：
$env:HUNTWEAVE_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:18400/huntweave"
$env:HUNTWEAVE_APP_DB_PASSWORD_FILE = "<checkout>\runtime\secrets\app_db_password"
$env:HUNTWEAVE_CHECKPOINT_DB_PASSWORD_FILE = "<checkout>\runtime\secrets\checkpoint_db_password"
$env:HUNTWEAVE_MIGRATOR_DB_PASSWORD_FILE = "<checkout>\runtime\secrets\migrator_db_password"
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
$env:PYTHONPATH = "<checkout>\backend\src"
& <venv>\python.exe -m huntweave.storage.migrate
# {"event":"migrations_complete"}   —— 迁移链 head 仍是 0008_retention_decisions，本票未新增迁移
```

`compose.isolated-db.yaml` 额外挂了一个非 internal 网络：基础文件里 `database` 是 `internal: true`，internal-only 网络上发布端口不会被接上（实测 `HostConfig.PortBindings` 已写入而 `NetworkSettings.Ports` 为空），补一个空网络才让回环绑定真实生效。

## 实施评审结论（对照实际代码）

| 验收项需要的实现 | 评审结论与代码证据 |
| --- | --- |
| 全局物理执行额度 | **原本缺失**。`contracts/runs.py` 的 `Budget.max_concurrency`（默认 4）**没有任何执行路径消费**：`grep -rn "max_concurrency" backend/src` 只有定义处一处命中。`plan()` 只检查 `BudgetReservation` 计数、输出字节与墙钟预算，没有任何跨 Run 的物理占用记账 |
| 同 IP 主动执行串行 | **原本缺失**。`plan()` 在 `resolve_target()` 绑定目标后直接创建 `ToolCall`，全程不查询同一 `target_ip` 上是否已有在飞调用；`grep` 在 `backend/src` 中找不到任何按目标地址串行化的代码 |
| 「一个持有长调用的 Run 不饿死其他 Run」 | **原本不成立**。`claim()` 按 `Run.created_at` 升序取候选并返回**第一个**匹配者；而「持有在飞调用」并不使 Run 失去领取资格（旧代码只在 `run.status == "waiting" and not self._active(...)` 时 `continue`）。单进程单轮次因此每轮都被最老的那个 Run 领走，它自己的 `plan()` 又因有在飞调用返回 `None` —— 轮次花在一个动不了的 Run 上，后面的 Run 永远轮不到 |
| 事件游标按 Run 独立 | **原本已实现**。`runs/events.py` 取 `EventCursor` 行时 `with_for_update=True`，游标行主键是 `run_id`；`history()` 在 `after > cursor` 时返回 `event_cursor_ahead` 409 |
| 重复唤醒幂等 | **原本有缺口**。`source_event_id` 判重发生在取游标行锁**之前**，两个并发到达的重复唤醒会双双读到「没有」，第二个撞 `(run_id, source_event_id)` 唯一约束报错 |
| 「可信停止才归还额度」 | **原本不可实现**。`runs/dispatch.py::_settle_undispatched` 为「账本确认从未接受」的调用写 observation，但**没写 `lease_active`**；按停止确认规则（`process_active/connection_open/lease_active` 三者都必须为 `False`）这条本身可信的「从未启动」事实会被读成「停止未确认」 |
| 「unclear」/「在飞」的定义只有一份 | **原本是两份拼写**。`claim()` 的候选过滤内联 `~exists(~status.in_(TERMINAL_CALLS))`，`_active()` 另写一遍同样的条件；`_actual_targets()` 这种只有一个返回值的间接层则没有消费者。首稿沿用了前者、又新加了后者，评审指出后已收敛（见「本记录的更正」） |
| `execution/ledger.py` 全量序列化 | **确认存在，本票不处理**。`_persist()` 每次保存都 `json.dumps(self.data)` 全量写整份账本；这是 #32 的三个单进程热点之一，见「与 #32 的边界」 |

## 实现改动

| 改动点 | 行为 |
| --- | --- |
| `contracts/resources.py`（新增） | 版本化资源政策：`RESOURCE_POLICY_VERSION`、`ResourcePolicy`（控制派发 4 / 全局物理 4 / 同 IP 1，可由 `HUNTWEAVE_*_SLOTS` 覆盖，非法值拒绝而不是夹取）、`ExecutionQuota`、`SlotWait`、`slot_wait()`（先判全局、再按**规范排序**逐个检查全部实际目标）、`STOP_FIELDS`（停止规则三个字段的**唯一定义**，`stop_confirmed` 与 `LIVE_SLOT_SQL` 都由它构造）、`stop_confirmed()`/`holds_physical_slot()`、以及 `LIVE_SLOT_SQL`。同一模块载有验收阈值 `ConcurrencyAcceptanceThresholds` 与 `ACCEPTANCE_THRESHOLDS_VERSION` |
| `contracts/runs.py` | 新增 `RESOURCE_POLICY_VERSION = 1`；`ScopeSnapshot` 增加 `resource_policy_version` 字段，明确是**审计留痕**（这个 Run 在哪个政策版本下开的），**不是**按 Run 冻结的额度——见「已知限制」 |
| `runs/orchestration.py` | 新增模块级 `in_flight_call(owner)`：「这个 Run 有在飞调用」的**唯一**谓词，`claim()` 用它过滤候选、`_active()` 用它回答单个 Run；`plan()` 在创建 `ToolCall` 的同一短事务内、`BudgetReservation`/`Outbox` **之前**取 `pg_advisory_xact_lock(class, key)`（先全局键、再按排序取目标键）并统计 `tool_calls` 上的物理占用，超限则只写一条 `execution_backpressure` 事件后返回 `None`（背压，不挂起 Run、不领取任何东西）；「决策已提交但调用未预留」变为**可续跑**。`_call_conditions` 改用 `holds_physical_slot()`，`_stop_confirmed()` 委派 `stop_confirmed()` |
| `runs/dispatch.py` | `_settle_undispatched` 的 observation 补上 `lease_active=False`：账本明确「没有这个 call_id」即证明不可能有控制租约，这是 §7 允许归还额度的那一类预留 |
| `runs/events.py` | 先 `INSERT ... ON CONFLICT (run_id) DO NOTHING` 建行、再 `FOR UPDATE` 取锁（`populate_existing=True` 保证锁真的取到），**然后**才判 `source` 去重；docstring 写明前置条件「调用者的事务必须已持有该 Run」（`event_cursors.run_id` 有指向 `runs.id` 的外键） |
| `tests/conftest.py`（新增） | `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 时，每个检查前清空 Run 相关业务表（`access_key_state`、`web_sessions` 与未建模的 `runtime_processes` 除外，它们属于部署而不属于夹具）。理由见下节：物理额度现在是夹具状态 |
| `tests/test_orchestration_integration.py` | `settle()` 夹具补上真实账本会写的停止事实（`started=True`、进程/连接/租约为 `False`），否则它模拟的是一条「已结算但停止未确认」的调用，而不是一条正常结束的调用 |

**为什么需要 `conftest.py`。** 物理额度从调用本身推导：一个调用只有在执行端确认「它启动的东西都不在了」时才归还额度。于是一个只记录调用、从不交给执行端的检查会**真的占住一个额度**——这正是真实部署里的行为。旧套件是「只追加」的，没有任何清理，同一轮里累积到 4 条就把后续检查挡在背压后面（实测：旧夹具下 `test_a_multi_target_...` 直接失败）。可丢弃数据库是这一约束的正当出口，README 也早已声明「集成检查会清空其测试数据库中的演示表」。

## 实际命令与完整输出

### 三项本地检查（在 `backend/` 内，最终提交后复跑）

```
& <venv>\python.exe -m pytest -m "not integration" -q
277 passed, 9 skipped, 54 deselected in 31.48s

& <venv>\python.exe -m ruff check src tests
All checks passed!

& <venv>\python.exe -m mypy --config-file pyproject.toml src
Success: no issues found in 51 source files
```

基线对照（`7c56643` 的 `git archive` 副本，排除本票改动）：`253 passed, 8 skipped, 41 deselected`。本票新增 **25 项** `tests/test_execution_quota.py` 检查（纯跑里 24 项通过、1 项因需要可丢弃数据库而跳过；连库跑是 `25 passed`）与 **13 项**集成检查（`test_concurrency_integration.py` 12 + `test_concurrency_stress.py` 1），即纯跑 `+24 passed / +1 skipped / +13 deselected`；源文件 50 → 51。8 → 9 项跳过里，7 项是「需要可丢弃数据库」的既有编排集成检查、1 项是本次新增的 SQL 谓词求值检查、1 项是「本次工作树没有前端产物」的控制台 bundle 检查。

### 完整后端套件（一次性数据库）

```
& <venv>\python.exe -m pytest tests --ignore=tests/test_startup_integration.py -q
334 passed, 1 skipped in 104.60s (0:01:44)
```

`test_startup_integration.py`（6 项，需要活动 app/Runner 容器）本轮**未**运行，见「未达成与限制」。

### 防漂移检查的变异验证（S2）

判据是「Python 规则与 SQL 谓词在**同一条记录上**给出同一个答案」。为证明这条检查抓得到反转，对两侧各做一次只改运算符的变异（保留全部字段名与字面量）：

```
# 基线
$ python -m pytest tests/test_execution_quota.py -q          → 25 passed in 1.08s

# 变异 A：Python 侧 is False → is not True
$ python -m pytest tests/test_execution_quota.py -q -rf      → 9 failed, 16 passed
FAILED tests/test_execution_quota.py::test_an_unstated_or_live_field_is_not_a_stop[process_active-None]
FAILED tests/test_execution_quota.py::test_an_unstated_or_live_field_is_not_a_stop[connection_open-None]
FAILED tests/test_execution_quota.py::test_an_unstated_or_live_field_is_not_a_stop[lease_active-None]
FAILED tests/test_execution_quota.py::test_a_record_with_no_observation_holds_its_slot
FAILED tests/test_execution_quota.py::test_settling_a_result_does_not_release_capacity[succeeded-True]
FAILED tests/test_execution_quota.py::test_settling_a_result_does_not_release_capacity[failed-True]
FAILED tests/test_execution_quota.py::test_settling_a_result_does_not_release_capacity[cancelled-True]
FAILED tests/test_execution_quota.py::test_settling_a_result_does_not_release_capacity[unknown-True]
FAILED tests/test_execution_quota.py::test_the_sql_predicate_and_the_python_rule_agree_case_by_case

# 变异 B：SQL 侧 IS DISTINCT FROM → IS NOT DISTINCT FROM
$ python -m pytest tests/test_execution_quota.py -q -rf      → 2 failed, 23 passed
FAILED tests/test_execution_quota.py::test_the_python_rule_reads_the_one_shared_field_list
FAILED tests/test_execution_quota.py::test_the_sql_predicate_and_the_python_rule_agree_case_by_case
# 取值级比对的差异项：{'confirmed stop': True} != {'confirmed stop': False}
#                      {'empty statement': False} != {'empty statement': True}
#                      {'every field live': False} != {'every field live': True}

# 还原
$ python -m pytest tests/test_execution_quota.py -q          → 25 passed in 1.04s
```

两处反转各自变红，且**取值级比对在两种变异下都独立变红**（不依赖结构断言）。变异只改运算符、随后按原始字节完整还原，工作树在还原后与变异前一致。

### 压力夹具（可重复运行）

```powershell
cd backend
$env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
& <venv>\python.exe -m pytest tests/test_concurrency_stress.py -q -s
# 1 passed in 36.43s   —— 报告为 JSON，打印在 stdout
```

夹具同时放大两个维度：**请求的活跃调用数**（1/2/4/5，5 是默认额度下第一个**请求量**超过额度的档位）与**历史账本量**（0 / 2 000 / 20 000 条已结算调用，直写 SQL 造，都带可信停止事实因而不占额度）。报告对每一行同时给出 `requested_calls` 与 `admitted_calls`，**实际放行数才是实测值**，请求数只是夹具设定。每个组合测 15 次取中位数与 p95。可用 `HUNTWEAVE_STRESS_LEDGER_SIZES` / `HUNTWEAVE_STRESS_ACTIVE_COUNTS` / `HUNTWEAVE_STRESS_ITERATIONS` 缩放。

## 实测容量与延迟（P1 规格 §3.7 的校准结果）

策略版本 `policy_version=1`、阈值版本 `thresholds_version=1`，全局物理额度 4、同 IP 1。报告里同时打印 `policy_source: ResourcePolicy.from_environment() of this process`，夹具开头断言 `POLICY == ResourcePolicy.from_environment()`，所以这些数字属于**本进程实际解析到的部署政策**，不是文件里的字面量。单位毫秒，除注明外为 15 次的中位数。

| 操作 | 档位（请求 → 实际放行） | 0 条 | 2 000 条 | 20 000 条 |
| --- | --- | --- | --- | --- |
| 调度轮次 `claim()` | 1→1 / 2→2 / 4→4 / 5→4 | 3.06–7.34 | 6.36–7.69 | 6.49–7.82 |
| 额度查询（读 `tool_calls`） | 同上 | 2.08–2.37 | 2.28–2.42 | 5.14–7.93 |
| 满额度下的计划尝试（含两次快照 + claim） | 4→4 / 5→4 | 40.2–41.7 | 39.6–42.4 | 47.7–50.5 |
| 就绪 Run 的调度等待 `scheduling_wait` | 1→1 / 2→2（各 1 次观测） | 84.3–91.3 | 78.0–79.3 | 86.3–87.1 |
| 取消 `control(cancel)` | 4→4 | 18.0 | 18.4 | 19.2 |
| 续租 `dispatcher.reconcile` | 4→4 | 19.7 | 19.8 | 20.6 |
| 对账扫描 `dispatcher.sweep(20)` | 4→4 | 84.5 | 80.0 | 76.5 |
| 结果结算（含 2 条证据） | 4→2 | 25.4 | 23.8 | 24.4 |
| 证据归档写入 64 KiB | 4→2 | 7.0 | 8.0 | 9.8 |
| 额度预留等待 advisory 锁 | 4→4（各 1 次观测） | 187.7 | 188.8 | 190.2 |

整轮的最大值（`worst_ms`）：`claim_turn` 9.4、`quota_count` 8.9、`plan_refused_pool_full` 75.2、`scheduling_wait` 91.3、`cancel` 20.9、`renew` 80.1、`sweep` 115.3、`settle_with_evidence` 31.6、`archive_write` 11.7、`quota_lock_wait` 189.8。`renew` 的 80.1 ms 是单次离群值（各档中位数 19.7–20.6 ms），不是稳定成本。

最后一行是设计出来的对照：持有方拿住全局额度键 200 ms 后释放，测得值已扣掉同库无争用时的预留耗时，所以它读到的就是锁等待本身（200 ms 持有 → 187.7–190.2 ms 等待，即等待几乎等于持有时间，没有额外串行放大）。

**容量结论（控制面）**：默认政策下，4 个 Run 各持一条在飞调用时全部放行；第 5 个 Run 即使目标地址完全不同也被拒绝（全局额度），且拒绝不领取任何东西（无 `ToolCall`、无 `BudgetReservation`、无 outbox 行）。同一地址上第二个 Run 的调用被拒绝，直到第一个调用的停止被确认。

**增长**：额度查询在 4 个档位上的中位数均值从 2.21 ms（账本为空）到 6.45 ms（20 000 条），即约 **0.21 ms / 1000 条历史调用**（阈值 2.0 ms / 1000 条）；夹具只对这一项做增长断言。调度轮次从 3.1–7.3 ms（账本为空）升到 6.4–7.8 ms（2 000 条及以上）后在 2 000 与 20 000 条之间基本平坦，与账本量不成线性——更像一次计划选择的变化，而不是逐行扫描；真正随账本线性增长的是额度查询那一项。这些是本机单点观测，不构成吞吐承诺。

**有界等待**：`scheduling_wait_seconds` 有断言，不是只写在契约里。夹具在「已放行 N 个持有者、再让一个就绪 Run 去领取并计划」时量这段墙钟时间，6 次观测为 78.0–91.3 ms，断言 `worst["scheduling_wait"] / 1000 <= 3.0` 通过。这个数字包含 Run/授权快照的创建，所以它是「就绪 Run 多久真的拿到轮次」的**上界**，而不是纯排队延迟。

**两个硬零的实际证明范围**（首稿把它们写成了「两项硬零」，范围说过头了，见「本记录的更正」）：

| 指标 | 实测 | 它真正证明的事 |
| --- | --- | --- |
| `second_calls_while_in_flight` | 0 | 在飞调用期间，没有任何 Run 产生第二条调用（每个持有者恰好 1 条）。这是「并发完成判定不会刚宣布完成又发起动作」的一面 |
| `test_an_unknown_outcome_is_never_acted_on_again` | 0 次重试 | **真的走 `unknown()`**：一个结果未知且无停止事实的调用，后续 claim + plan 一轮**什么都不产生**，调用数仍为 1，`preview.can_resume` 为 false，且额度仍被占用。这是「unknown 误重试必须为 0」的直接测量 |
| `released_without_stop` | 0（对照组 3 次 `held` + 93 次 `released_on_stop`） | 每一次额度归还都被夹具按判据核过：读归还前后占用与**留下的记录**，只有记录是可信停止时才计为合法归还。「结算但无停止事实」的对照组（3 次）必须回到 `held`，否则这条指标就是空的 |

`duplicate_actions` / `wrong_releases` 这两个名字在首稿里把上面两件事划了等号，现已拆开：前者是「在飞期间没有第二条调用」，后者由 `released_without_stop` + 正对照承担。

## 逐条验收判定

### 第一组

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 每轮一个领取 + 轮转对账窗口；一个卡住或未核对的 Run 不饿死其他 Run | **通过（控制面）** | `claim()` 每轮只返回一个 Run（既有）；新增 `tests/test_concurrency_integration.py::test_a_run_holding_a_call_yields_the_turn_to_the_other_runs`（三个 Run 依次各被领取一次）、`::test_a_run_that_cannot_be_reconciled_does_not_starve_the_others`；既有 `test_orchestration_integration.py::test_run_awaiting_reconciliation_never_starves_the_single_scheduler` 仍通过。轮转对账窗口沿用 `agentd` 既有的 `SWEEP_LIMIT=20` + `sweep_offset` |
| 2 | 同 IP 主动执行串行；全局工具执行并发与预算不超限；并发完成判定不会刚宣布完成又发起动作 | **通过（控制面）** | `::test_one_address_runs_one_active_action_across_runs`、`::test_a_full_global_pool_backpressures_new_target_execution`、`::test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does`、夹具 `second_calls_while_in_flight=0`。真实执行下的时序未实测（见 #10） |
| 3 | 每个 Run 的事件游标相对独立，不越过未发布事件；重启后按持久状态接续并幂等处理重复唤醒 | **通过** | `::test_each_run_keeps_its_own_event_cursor_and_a_reader_cannot_pass_it`（两 Run 交替追加，游标各为 1..n 稠密、互不串流；`after > cursor` 给 `event_cursor_ahead` 409）、`::test_a_duplicate_wakeup_lands_once_even_when_both_arrive_at_the_same_time`（8 线程并发同源唤醒只落一条事件）、`::test_a_restart_resumes_from_the_durable_records_and_reserves_no_second_call`（新 scheduler 重新取得租约代次，重放同一轮次不产生第二条调用） |
| 4 | 记录实测容量与延迟数据，作为 P1 规格 §3.7 校准的实际结果 | **部分通过** | 控制面容量与延迟已实测并记于本记录（上一节的方法与数字）。**真实执行下的容量与延迟受阻**：`real_execution_ready=false`（ADR-0010 四项门槛中 `profile_unvalidated` 与 `revert_path_missing` 未满足），产品拒绝创建真实 Run，因此无法在真实执行下取数 |
| 5 | 基础多 Run 公平与取消检查随 #17/#18 提前验收，最终的容量与延迟校准留在本条 | 基础公平与取消：**通过**；最终容量与延迟校准：**部分通过**（同 #4） | 公平：见 #1；取消：`::test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held` 与夹具的 `cancel` 一列 |

### 第二组（2026-10-10 同步 0.9.5 设计）

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 6 | 全局物理额度（默认 4）与同 IP 上限（默认 1，跨 Run、含受控复现）同时生效；并发记账不按主锚点只锁一个 IP，多目标调用检查全部实际目标 | **受限（部分）** | 前两句：**通过**——`::test_one_address_runs_one_active_action_across_runs`、`::test_a_full_global_pool_backpressures_new_target_execution`；受控复现（重派）走同一处门控（`plan()` 在 `replacing` 路径之后、创建 `ToolCall` 之前检查）。**第三句受限**：当前票据只绑定一个 `target_ip`，`plan()` 里是单元素元组 `targets = (bound_ip,)`，`slot_wait()` 遍历它；也就是说「检查全部实际目标」这条规则**没有可表达的多目标调用可验**，纯检查断言的形状是生产代码今天永远不会传进来的。判定因此保持**受限**，不写通过 |
| 7 | 停止未确认继续占用全局与同 IP 额度；结果结算、超时或租约到期均不释放物理额度，只有可信停止事实才归还；结果 `unknown` 但停止已确认可释放额度，核对结果另行保留 | **通过（控制面）** | `::test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does`（结算后仍占 1，补上可信停止事实后才归零，且状态仍是 `succeeded`）、`::test_an_unknown_outcome_with_a_confirmed_stop_returns_capacity_and_keeps_reconciling`、`::test_an_unknown_outcome_is_never_acted_on_again`、`::test_a_refused_intent_releases_the_slot_it_reserved`、`tests/test_execution_quota.py` 的停止规则逐格覆盖（含变异验证） |
| 8 | 物理额度耗尽时新目标执行背压，但取消、核对与回收控制路径仍须可用；不得通过超额启动其他 IP 消除等待，也不承诺每 Run 保底一槽 | **通过（控制面）** | 背压：`::test_a_full_global_pool_backpressures_new_target_execution`（Run 仍 `running`、`reason_code` 为空、无任何领取）；控制路径可用：`::test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held`（4 个额度全被占时取消被接受、结算入库、待对账列表仍可用）与夹具在满额度下测得的 `cancel`/`renew`/`sweep`/`settle_with_evidence`。回收路径（`runs/retention.py`）本轮未改，也没有任何「按 Run 保底一槽」的逻辑。**控制派发槽的现状要说清**：`ResourcePolicy.control_dispatch_slots = 4` 记录了 ADR-0016 的设计值，但本代码库里取消/续租/对账/结算是直接调用、不经过任何派发队列，因此没有可强制的派发槽；「控制路径不被物理额度挡住」这条性质由上面的实测与检查直接证明，而不是由该字段强制 |
| 9 | 阈值（响应、等待、增长）在运行前锁定并版本化，不接受事后移动标准；`unknown` 误重试必须为 0，且无错误资源释放 | **部分通过**（首稿写「通过」，已下调） | 阈值确实在运行前锁定并版本化：`contracts/resources.py::ConcurrencyAcceptanceThresholds`，`thresholds_version = ACCEPTANCE_THRESHOLDS_VERSION = 1`，夹具把该版本连同 `policy_version` 与 `policy_source` 一起打印，本记录引用同一版本；响应、等待、增长三项现在都有实测值与断言（控制路径最坏 115.3 ms ≤ 5 000 ms；调度等待最坏 91.3 ms ≤ 3 000 ms；额度查询增长 0.21 ≤ 2.0 ms/1000 条）。**`unknown` 误重试为 0** 由 `::test_an_unknown_outcome_is_never_acted_on_again` 直接测（不是由「在飞期间无第二条调用」代替）。**无错误资源释放**由夹具的 `released_without_stop = 0` 加上「结算但无停止事实」的 3 次正对照承担，**不是**由断言之后的一行差值承担。之所以仍判「部分」而不是「通过」：本条的「真实执行下」限定与 #10 同因受阻，而且「无错误资源释放」目前只在控制面可判——真实执行端是否会在别处归还额度，本票没有证据 |
| 10 | 本条只验真实执行下的额度、背压与控制可用；完整负载矩阵与 24 小时墙钟两格归 P2-E | **受阻** | **阻塞于 ADR-0010 就绪门槛**：`real_execution_ready=false`，其中 `profile_revalidation`（`profile_unvalidated`）与 `deployment_revert`（`revert_path_missing`）未满足，`RunService.create_run` 对非假执行 profile 直接抛 `real_execution_not_ready`。因此「真实执行下的额度、背压与控制可用」无法实测。**复验条件**：四项门槛全部满足、`real_execution_ready=true` 后，用同一夹具把 Run 的执行 profile 换成 `real-lab-v1`（`Runner` 桩改成真实 Runner 客户端）复跑，并把数字补进本记录的新增小节。完整负载矩阵（每 Run 1/10/50/100 IP × 1/2/4/5 活跃 Run、模型等待注入、准备快/慢/失败、目标快/慢/超时）与 24 小时墙钟两格**不在本条**，归 P2-E（`docs/specs/0003-agent-research.md` §9.4） |

### 与 `0006 §11` 成对验收三行的对照（本条的核心判据）

`docs/specs/0006-state-model-and-delivery.md` §11 的成对表里，与本条直接对应的三行逐条对照如下：

| 成对行 | 「必须通过」的一半 | 「必须阻止或保留未决」的一半 |
| --- | --- | --- |
| **资源** | `::test_an_unknown_outcome_with_a_confirmed_stop_returns_capacity_and_keeps_reconciling`：结果 `unknown` 但停止已确认 → 额度归还，核对继续（Run 仍 `execution_unknown`、仍在 `reconcilable_runs()`、`preview` 仍要求核对） | `::test_a_settled_result_does_not_return_capacity_but_a_confirmed_stop_does`：**结算成功本身不释放**——`completed` 记录先到、额度仍为 1，只有随后的可信停止事实才归零；`test_execution_quota.py` 的 `is False` 逐格矩阵把「控制槽释放/成功结算/租约过期被当作停止」三种误读都钉住 |
| **容量** | `::test_cancelling_reconciling_and_settling_stay_usable_with_every_slot_held`：4 个停止未确认把额度占满时，取消被接受、结算入库、待对账列表仍可用；夹具在满额度下测得这些路径都是毫秒级 | `::test_a_full_global_pool_backpressures_new_target_execution`：额度耗尽时新目标执行被**背压**（Run 仍 `running`、无 `reason_code`、无调用/预留/outbox），而不是被启动；夹具断言「请求 5 → 实际只有 4」并把 `requested_calls`/`admitted_calls` 分开打印，证明没有通过超额启动其他 IP 消除等待 |
| **IP** | `::test_one_address_runs_one_active_action_across_runs`：同一地址跨 Run 只跑一个主动动作，且第一个 Run 的调用被确认停止后同一地址立刻放行第二个 Run | `::test_a_full_global_pool_backpressures_new_target_execution` 的同一处门控也覆盖同 IP；`slot_wait()` 先判全局再逐个判地址，**不按主锚点只锁一个 IP**。注：`degraded` 下禁止发起只读请求这一句属执行端状态机，本轮未触及（§7 的 `quarantine`/`degraded` 效果等价但未显式建模，见「已知限制」） |

## 受阻项与复验条件汇总

| 受阻项 | 阻塞于 | 复验条件 |
| --- | --- | --- |
| 真实执行下的容量与延迟取数（#4、#5） | `real_execution_ready=false`：ADR-0010 门槛 1（profile 复验，`profile_unvalidated`）与门槛 4（回退入口，`revert_path_missing`） | 四项门槛全部满足后，用本记录给出的同一夹具命令在真实 profile 下复跑，数字补入本记录新增小节 |
| 真实执行下的额度、背压与控制可用（#6、#7、#8、#10） | 同上 | 同上；届时还需补上「同 IP 串行在真实目标上确实没有并发连接」的观察，本轮的证据只是控制面不发出第二条调用 |
| 多目标调用检查全部实际目标（#6） | 票据契约：`ExecutionRequest` 只带一个 `target_ip`/`target_port` | 出现多目标票据后，`plan()` 里那个单元素元组改为列出全部地址即可（`slot_wait()` 与 `_lock_execution_slots()` 已按列表工作），并补一条**集成**检查覆盖多目标拒绝——纯函数检查证明不了生产代码会传进多元素列表 |
| 「无错误资源释放」在真实执行端的一半（#9） | 真实执行未开放 | 与第一条同时复验：真实执行端归还额度的路径（`SandboxManager.stop_fact` → `_observation_after`）需要独立的检查 |
| 完整负载矩阵与 24 小时墙钟（#10） | 不在本条范围 | 归 P2-E（`0003` §9.4） |

## 已知限制

- **额度不按 Run 冻结：改部署环境变量会立刻影响活跃 Run**（S1 的处置）。`ScopeSnapshot.resource_policy_version` 是**审计留痕**，记录「这个 Run 在哪个资源政策版本下开的」；全仓**没有任何代码读回它**。实际限额来自 `ResourcePolicy.from_environment()`（`runs/orchestration.py` 在 `plan()` 里每次预留都读 `self.policy`），而 `OrchestrationService.__init__` 只在**进程启动时**解析一次环境。所以：改 `HUNTWEAVE_GLOBAL_EXECUTION_SLOTS` 并重启后，**已经 Running 的 Run 的额度立刻改变**。本票接受这一行为，理由与改用冻结的判据如下：
  - **接受的理由**：额度是**部署容量**而不是 Run 的属性——它约束的是这台机器同时能跑几个真实执行，与某个 Run 的授权、预算或时限无关；把容量按 Run 冻结会让同一台机器上的新旧 Run 各自按不同上限占用真实资源，容量反而不可控。`policy_version` 的版本记录义务（`PROJECT.md` §12）由「改默认值须改 `RESOURCE_POLICY_VERSION`」承担。
  - **何时必须改成按 Run 冻结（方案①：一张「版本 → 数值」表，`ScopeSnapshot` 记版本、调度器按该版本查表）**：出现下列任一情形时，审计留痕不再够用——①客户合同/审计要求「一个 Run 的运行条件在授权时固定，事后不可变」；②同一部署需要让不同授权来源的 Run 遵守不同的物理上限（例如外部托管的靶场要按 Run 限流）；③回退/前滚需要「旧 Run 继续按旧额度跑」以做对照测量。三者都不是今天的场景，所以本票选方案②（降级为审计留痕 + 如实写明），并把这个判据留在这里。
- **真实执行未开放**：本记录中所有数量与延迟都在假执行 profile 下取得。假执行端与真实执行端共用同一调用账本、同一控制租约与同一取消/续跑路径（`execution/ledger.py`、验证记录 `0013`/`0018`），但真实目标连接、真实进程与真实证据归档的时序**未**被测。
- **未确认停止确实会长期占用额度**。一个本地状态为已派发、而账本查不到该 `call_id` 的调用会走 `unknown()`，其 observation 保持 `None`；`reconcile()` 的 `not_executed` 裁定又要求账本给出可证事实。因此这条路径上「额度不归还」是规格要求（§7「停止未确认继续占用」），但没有操作员可用的绕过手段：恢复条件是执行端账本重新给出答案。这是设计要的背压，也是该设计最需要被监视的一处容量损失。验证记录 `0014` 已登记控制台尚未消费该区分，队列面板归 P2-B（#57）。
- **公平口径仍是 FCFS + `created_at`（P2 建议）**。本票只消除了「在飞调用白占一个调度轮次」这一种饿死；候选之间仍然是先到先服务。ADR-0016 要求的**分池与校准份额**（成本归一化、首触优先与 aging、复审保留 20%）**不在本条**，归 P2-E（#57）用本票与 #32 的数据去校准。
- **`agentd` 的轮转对账窗口有一处既有缺陷（P3 建议，本票未覆盖）**。`SWEEP_LIMIT = 20` + `sweep_offset` 的轮转在「可对账 Run 数恰为 20 的整数倍」时会周期性跳过同一段窗口（最后一页恰好取满 20 条时 `swept == SWEEP_LIMIT` 让 offset 归零，下一轮的窗口又从头开始）。这是**既有**行为，本票既未触发也未修复；归属：`agentd` 的扫描窗口策略，登记为独立缺陷（不属 #21 的验收面，#21 只要求「轮转窗口存在且一个卡住的 Run 不饿死其他 Run」，这一点已由 `reconcilable_runs(limit, offset)` + 检查覆盖）。
- **`quarantine` 状态未显式建模（P4 建议）**。`0006 §7` 写明「停止未确认时相关 IP 进入 `quarantine` 显式状态，仍占全局物理额度」。本票的效果等价（该地址的额度不归还、同地址新动作被拒），但**没有**一个可查询的 `quarantine` 标记，操作员在界面上看不到「这个 IP 被隔离」。归属：`quarantine`/`degraded` 的状态机与投影属 P2-B（#57）与 `0006` §7 的落地，#21 只要求额度不被错误归还。
- **背压只按决策记一次**。`execution_backpressure` 事件用 `decision_id + ":backpressure"` 作为 `source_event_id` 去重，所以同一决策的等待只留一条事件，等待**时长**不落库。等待类别、时长与占用者的实时展示属于队列面板（ADR-0016「队列可解释性」），归 P2-B（#57）。
- **额度查询是全表扫描，本票没有加索引**。`_execution_quota()` 按 `LIVE_SLOT_SQL` 统计 `tool_calls`。本票另做了一次**一次性探针**（P6：该探针的脚本**没有入库**，第三方无法照抄复跑，本记录把它标成「不是本票夹具」）：在临时表上造 1 000 000 行、其中恰好 4 行是持有者，并建一条以同一谓词为条件的部分索引——

  ```sql
  CREATE INDEX ix_probe_live ON probe.t (((ticket->>'target_ip')))
    WHERE (observation IS NULL
       OR observation->>'process_active' IS DISTINCT FROM 'false'
       OR observation->>'connection_open' IS DISTINCT FROM 'false'
       OR observation->>'lease_active' IS DISTINCT FROM 'false');
  ANALYZE probe.t;
  EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM probe.t WHERE <同一谓词>;
  ```

  结果是 `Parallel Seq Scan`（约 55 ms），索引未被选中：裸 `count(*)` 上的 index-only scan 无法并行，而规划器对谓词的选择率估计远高于实际，于是并行顺序扫描仍然更便宜。所以一个「只为这次统计」的部分索引不会被用上，本票因此**没有**新增索引或迁移，只把增长曲线测出来。**这条结论只供 #32 参考，不是本票的验收证据**；#32 若要复算，需要自己重建上述表与索引（本记录给出的是当时的 SQL，不是可执行脚本）。本环境**能**复跑的是夹具本身（32–36 秒，命令见上）；**不能**从仓库复跑的是这个 100 万行探针与其中的 55 ms 数字。
- **压力夹具用直写 SQL 造历史账本**，只覆盖会被调度读到的表（`runs`/`research_tasks`/`agent_sessions`/`decisions`/`tool_calls`/`budget_reservations`/`audit_events`/`event_cursors`），不代表真实证据归档的体积；证据写入用真实的 `EvidenceArchive` 单独测量。
- **未运行项**：`tests/test_startup_integration.py`（6 项，需要活动的 app/Runner 容器）与 `lab/` 四份探针（动作 18、生命周期 29、出口 30、保留 10）本轮未复跑——本票的接缝在控制面事务与调度选择，不涉及容器生命周期与出口规则。
- **未做前端与浏览器验收**：按 ADR-0018，本轮是后端切片；控制台没有新增页面或字段，队列等待类别的前端呈现归 P2-B。
- **单点观测**：全部数字来自一台 Windows 11 + Docker Desktop/WSL2 宿主与一个 `postgres:17-bookworm` 容器；没有原生 Linux 宿主对照，也没有重复多轮取分布（夹具的 15 次迭代是同一进程内的重复）。`scheduling_wait` 与 `quota_lock_wait` 各只有 1 次观测，是「上界式」的读数。

## 与 #32 的边界

- #32 测的是**单进程内部的三个热点**：`api/agentd.py` 的顺序推进、`execution/ledger.py::_persist()` 的全量序列化、`runs/events.py` 的游标行锁，口径是**成本/吞吐曲线**，结论形式是「调参数还是改结构」。
- 本票测的是**多活跃 Run 之间**的额度、串行、公平、背压与控制路径可用性，口径是**并发正确性与校准值**。
- 本票**复用**了 #32 需要的压力夹具与账本规模维度（`tests/test_concurrency_stress.py` 的「历史账本量」一维），但**不替 #32 下「是否需要结构改动」的结论**：本记录只给出实测增长（额度查询 0.21 ms / 1000 行、无索引可用），并把「要不要为此改结构」明确留给 #32。本票也**没有**声称完成 #32，没有优化 `_persist()` 或 `agentd` 的推进方式。

## 迁移与共享面影响

- **未新增、未修改任何 Alembic 迁移**。迁移链 head 仍是 `0008_retention_decisions`（`down_revision = "0007_drop_login_throttle"`），`storage/database.py::BUSINESS_REVISIONS` 未改，合入时**无需**迁移编号顺序核对。新增的 `ON CONFLICT (run_id) DO NOTHING` 依赖 `event_cursors` 既有主键，额度统计只读 `tool_calls` 既有列（`status`、`ticket`、`observation`）。
- **`contracts/`（共享面）**：新增 `contracts/resources.py`；修改 `contracts/runs.py`（新增 `RESOURCE_POLICY_VERSION` 常量与 `ScopeSnapshot.resource_policy_version` 字段，二者都带默认值，旧快照仍可校验）。其他并行分支若同时改 `ScopeSnapshot` 或政策版本常量会冲突，需按 `docs/agents/p2-execution-batches.md` 第 4 节串行合入。
- **`api/` 装配**：**未改**。`OrchestrationService.__init__` 新增的 `policy` 是带默认值的关键字参数，`api/agentd.py` 与 `api/app.py` 的既有装配无需改动。未新增路由，未新增原因码（因此 `tests/test_reason_codes.py` 不需要改，等待类别走 `waiting_category` 字段而不是 `reason_code`）。
- **`runs/`**：`orchestration.py`（新增模块级 `in_flight_call()`，`claim()` 与 `_active()` 共用它；`plan()` 额度门控与可续跑决策；新增 `_execution_quota`/`execution_quota`/`_lock_execution_slots`；`_call_conditions` 改用 `holds_physical_slot()`；`_stop_confirmed` 委派）、`events.py`（取锁与判重的顺序 + 前置条件文档）、`dispatch.py`（一处 observation 补字段）。`orchestration.py` 是最容易与其他并行分支冲突的文件。
- **`deploy/`**：新增 `compose.isolated-db.yaml`。它**不是**「少一个 -f 就安全」的便利文件：`HUNTWEAVE_ISOLATED_DB_PORT` 没有默认值，缺它时 Compose 直接报 `required variable ... is missing a value` 并拒绝启动，因此不会出现「多打一个 -f 就把日常项目的库发布到宿主」的情况。
- **`backend/tests/conftest.py`（新文件，影响面最大）**：它给**每一个** `HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 下的检查加了「先清空 Run 相关表」的 autouse 夹具。其他并行分支若新增依赖跨检查保留业务数据的集成检查，会与本夹具冲突，需要改用各自的显式夹具。
- **`docs/`**：只新增本记录与 `docs/validation/README.md` 的一行索引。`PROJECT.md`、`docs/STATUS.md`、`docs/specs/`、`docs/adr/` 本轮**未改**——见「待人工确认」。
- **合入时的顺序核对**：本分支首稿基于 `7c56643`，协调人随后把它 rebase 到 `3ca3684`（含 #37 的契约对齐与 #33 的视觉 tokens），本票的提交因此落在该历史之上；本记录的数字与判定都是相对 **`3ca3684`** 这一基线得出的（`git diff --stat 3ca3684..HEAD` = 13 files, +2482/−39）。写记录时 `origin/main` 又前进到 `c8033c8`（#38 的 Phase 0 来源盘点与契约目录），因此：
  - 对 `origin/main` 直接取 diff 会把 #38 的提交显示成「被本分支删除」（`contracts/phase0.py`、`tests/test_phase0_contracts.py`、`docs/validation/0023-*.md` 等），那不是本票的改动；本票没有触碰任何 Phase 0 文件。
  - `docs/validation/README.md` 是唯一可预期的文本冲突点：本票在表尾追加 0022 行、#38 追加 0023 行，两行都要保留（编号互不冲突：0020 = #37、0021 = #33、0022 = 本条、0023 = #38）。索引已核对为 0019 → 0020 → 0021 → 0022。
  - `docs/specs/0006-state-model-and-delivery.md` 在 #37/#38 中只增补了 §3 与 §10 的内容，**§7 与 §11 的标题与编号未变**，本记录与代码注释对它们的引用仍然有效；`PROJECT.md` §12 的数值表未变。迁移链仍是 `0008_retention_decisions`，无编号顺序问题。

## 本记录的更正

本节记录独立两轴评审（Standards / Spec）对首稿的发现与处置。**首稿的结论没有被静默改写**，下面每一条都是「原来怎么写 → 现在怎么改 → 证据」。

| # | 首稿的问题 | 处置 | 证据 / 位置 |
| --- | --- | --- | --- |
| S1 | `contracts/runs.py` 的字段注释断言「一个 Run 保留它被打开时的额度……改部署槽数是版本化行为而非静默重新定价」，与实现**不符**：限额实际来自当前进程的 `ResourcePolicy.from_environment()`，全仓无人读该字段，改环境变量重启后活跃 Run 的额度立刻改变 | 采纳评审倾向的方案②：注释改为**审计留痕**并明确写出「改环境变量会即时影响活跃 Run 的额度，这不是按 Run 冻结的值」；`ResourcePolicy` 的 docstring 写同一句话；本记录「已知限制」新增一节，点名这个事实并给出**何时必须改用方案①（版本→数值表）**的三条判据 | `backend/src/huntweave/contracts/runs.py` 的 `resource_policy_version` 注释；`contracts/resources.py::ResourcePolicy` docstring；本记录「已知限制」第一条 |
| S2 | 防漂移检查只比字段名集合、数 `"IS DISTINCT FROM 'false'"` 出现 3 次、禁 `<>`；评审推演出 `is False → is not True` 与 `IS DISTINCT FROM → IS NOT DISTINCT FROM` 两处反转**不变红** | 检查改为**取值级**：8 条构造观测（无记录、空记录、缺 `lease_active`、进程/连接无法确认、租约仍活、全部活、全部停止）同时喂给生产判据与 `SELECT … WHERE <LIVE_SLOT_SQL>`（对 `VALUES` 列表求值，不建表），断言逐例一致；另留一条不需要数据库的结构守卫（禁 `<>` 与 `IS NOT DISTINCT FROM`，并要求 `stop_confirmed` 真的读 `STOP_FIELDS`）。字段名不再比对，因为只有一份定义 | `tests/test_execution_quota.py::test_the_sql_predicate_and_the_python_rule_agree_case_by_case`、`::test_the_python_rule_reads_the_one_shared_field_list`；**变异验证**见上（A：9 failed；B：2 failed；还原后 25 passed） |
| S6 | `wrong_releases = max(0, admitted - quota.global_used)` 紧跟在 `assert quota.global_used == admitted` 之后，只能恒为 0 —— 它度量的是那句断言，不是释放规则 | 改为夹具自查：`settle_audited()` 读归还**前后**的占用与**留下的记录**，只有记录是可信停止时才计 `released_on_stop`，否则计 `released_without_stop`；并加**正对照** `settle_without_a_stop()`（结算但无停止事实）必须回到 `held`，否则指标是空的 | 夹具报告字段 `released_on_stop = 93`、`released_without_stop = 0`、`held_after_settlement_without_stop = 3`；断言 `released_without_stop <= wrong_releases_max` 且 `held_after_settlement_without_stop >= 1` |
| S7 | `duplicate_actions == THRESHOLDS.unknown_retries_max` 把两个不同的量划等号；夹具全程不产生 `unknown`，所以它对「unknown 误重试」没有证明力 | 拆成两条：①改名 `second_calls_while_in_flight`（在飞期间没有第二条调用），不再与 `unknown_retries_max` 比较；②新增**真的走 `unknown()`** 的检查，断言 claim + plan 一轮什么都不产生、调用数为 1、`can_resume` 为 false、额度仍被占用 | 夹具字段 `second_calls_while_in_flight = 0`；`tests/test_concurrency_integration.py::test_an_unknown_outcome_is_never_acted_on_again`（并用 `retries_of()` 与 `THRESHOLDS.unknown_retries_max` 比较） |
| S3 | `_active()` 表达的正是 `claim()` 里内联的 `~exists(~status.in_(TERMINAL_CALLS))`，同一个「在飞」条件两份拼写（评审说它已成死代码；实测它仍被 `control("resume")` 使用，所以真正的问题是**重复**而不是死代码） | 抽出模块级 `in_flight_call(owner)`，`claim()` 的候选过滤与 `_active()` 都改用它；`_active()` 的 docstring 注明它就是 `claim()` 选取的同一谓词 | `backend/src/huntweave/runs/orchestration.py` 的 `in_flight_call()`、`claim()`、`_active()` |
| S4 | `holds_physical_slot()` 零生产调用，真正生效的是 `_execution_quota()` 里内联的 SQL 文本，而三处 docstring 对「谁读哪一份」的说法互相矛盾（「控制台读它」是错的） | 让字段列表只有一份定义（`STOP_FIELDS`，两侧都由它构造），并给 `holds_physical_slot()` 一个真实生产消费者（`_call_conditions` 用它决定 `stop_unconfirmed`）；三处 docstring 分别写明 `LIVE_SLOT_SQL` 由 `_execution_quota()` 读、`stop_confirmed` 由 `_stop_confirmed` 读、`holds_physical_slot` 由 `_call_conditions` 读与检查读。「控制台读它」的说法已删除 | `contracts/resources.py` 的 `STOP_FIELDS`/`LIVE_SLOT_SQL`/`stop_confirmed`/`holds_physical_slot` docstring；`orchestration.py::_call_conditions` |
| S5 | `_actual_targets()` 的间接层没有消费者，`_execution_quota` 的 docstring 把「未来多目标形状」写成了现状 | 按评审的第二个选项删掉间接层，改为调用点内联 `targets: tuple[str, ...] = (bound_ip,)` 并注明「多目标票据在这里列出全部地址」；`_execution_quota` 的 docstring 改为陈述现状（只读 `LIVE_SLOT_SQL`，Python 拼写由检查比对） | `orchestration.py` 的调用点注释与 `_execution_quota` docstring |
| S8 | 夹具报告只打印请求的 `active` 值，不打印实际放行数；记录把「5 无法全部放行」写成实测峰值 | 报告每行同时给出 `requested_calls` 与 `admitted_calls`；本记录的容量结论改为「**请求 5 → 实际 4**」，并说明 5 是夹具设定的档位、实测值只有 4 档放行 | 报告字段 `requested_calls` / `admitted_calls`；本记录「实测容量与延迟」的档位列与容量结论 |
| S9 | 记录把 `policy_version`（契约常量）与 `thresholds_version` 并列成「运行前锁定」的证据，但 Run 的配额实际来自进程环境 | 两者分别标注来源；夹具开头断言 `POLICY == ResourcePolicy.from_environment()`，报告里新增 `policy_source` 字段说明数字属于本进程解析到的政策 | 夹具断言与报告字段 `policy_source`；「实测容量与延迟」开头一段 |
| S10 | `compose.isolated-db.yaml` 的注释称「deliberately opt-in」，但实际只要多一个 `-f` 就会把日常 `huntweave` 库发布到 `127.0.0.1:18400` | 端口改成必填占位符 `${HUNTWEAVE_ISOLATED_DB_PORT:?...}`，缺变量时 Compose 直接失败；注释改为如实警示（「这是刻意动作，不是便利文件」） | `deploy/compose.isolated-db.yaml`；实测缺变量时的报错见「环境与入口」 |
| S11 | 记录没写「本轮不改 `docs/STATUS.md`」 | 「待人工确认」补一行说明原因与归属 | 本记录「待人工确认」 |
| S12 | `append_event` 现在会先建游标行，`event_cursors.run_id` 有指向 `runs.id` 的外键，对不存在的 Run 调用会以 FK 违例失败 | docstring 写明前置条件「调用者的事务必须已持有该 Run」，并说明失败是刻意的 | `backend/src/huntweave/runs/events.py::append_event` |
| — | `scheduling_wait_seconds` 有 3.0 s 上界但**没有断言** | 新增 `scheduling_wait` 一行（就绪 Run 领取 + 计划的墙钟时间，1 次观测/档），并断言 `<= scheduling_wait_seconds`；同时说明它是上界（含 Run 创建） | 夹具 `record("scheduling_wait", ...)` 与末尾断言；「实测容量与延迟」的「有界等待」一段 |
| P5 | 记录没引 `0006 §11` 的成对验收三行 | 新增「与 `0006 §11` 成对验收三行的对照」小节，按「资源／容量／IP」逐条列通过面与阻止面 | 本记录该小节 |
| P6/P7 | 一次性 100 万行探针未入库，第三方无法复跑 | 「已知限制」明确「该探针不是本票夹具、结论仅供 #32 参考」，并如实区分哪些数字**能**在本环境复跑（夹具 32–36 秒）与哪些**不能**（100 万行探针的 55 ms） | 本记录「已知限制」的索引条目 |
| P2 | 公平口径的归属没写 | 「已知限制」写明：仍是 FCFS + `created_at`，ADR-0016 的分池与校准份额归 P2-E（#57），本票只消除了「在飞调用白占轮次」这一种饿死 | 本记录「已知限制」 |
| P3 | `agentd` 轮转窗口的既有缺陷没登记 | 「已知限制」登记：`SWEEP_LIMIT=20` 的轮转在「可对账 Run 数恰为 20 的整数倍」时会周期性跳过同一段；**既有**行为，本票未触发也未修复，归属为独立缺陷 | 本记录「已知限制」 |
| P4 | `quarantine` 状态没登记 | 「已知限制」登记：本票效果等价（占用不归还）但没有可查询的 `quarantine` 标记，操作员看不见；归属 P2-B（#57）与 `0006 §7` 的落地 | 本记录「已知限制」 |
| — | 第 9 条判定原为「通过」 | **下调为「部分通过」**，并写明两项零值断言的实际证明范围 | 判定表第 9 行 |
| — | 第 6 条判定原写「通过（控制面）／一项受限」 | 改为**「受限（部分）」**，正视「纯检查在证明一个生产代码今天永远不会传进来的形状」这一点 | 判定表第 6 行 |

### 合入 `#43` 后的位置更正（2026-10-11，协调人补记）

`#43` 删除了旧的整段 `plan()`，把一次计划拆成 `begin_planning → 事务外模型判断 → commit_planning`。本记录里「**`plan()`** 在同一短事务内预留物理额度」「`plan()` 在 `replacing` 路径之后、创建 `ToolCall` 之前检查」这类指向 `plan()` 的说法，因此**只描述 #21 交付时的位置**。语义未变，落点已变：

| 本记录的说法 | 合入 `#43` 后的实际位置 |
| --- | --- |
| `plan()` 在创建 `ToolCall` 的同一短事务内、`BudgetReservation`/`Outbox` 之前取锁并统计物理占用 | `runs/orchestration.py::_dispatch`：`_lock_execution_slots` → `_execution_quota` → `slot_wait` 的道次，仍在写 `ToolCall`/`BudgetReservation`/`Outbox` 的同一短事务内 |
| 先全局键、再按排序取目标键（锁先于计数） | 未变（`_lock_execution_slots` 仍在 `_execution_quota` 之前） |
| 超限只写一条 `execution_backpressure` 后返回 `None`，不领取任何东西 | 未变；去重键 `str(decision_id) + ":backpressure"` 随移植保留 |
| 受控复现（重派）走同一处门控 | 未变；额度门在 `replacing is None / Redispatch` 分支**之后**，两条路径共用 |
| 「决策已提交但调用未预留」可续跑 | 未变，但在 `#43` 的结构下位于 `begin_planning` 的已提交分支 |

`test_concurrency_integration.py` 与 `test_concurrency_stress.py` 的调用点也随 `#43` 改为经 `tests/_planning.py` 的 `plan_step` 走三段协议。**这两个文件在 `#43` 合入前一直在调用已删除的 `plan()`，而它们被 `-m "not integration"` 跳过，所以本记录第 6 条的额度门控当时其实没有任何检查在跑**（该缺陷、修复与变异验证见 [0024](./0024-model-outside-transactions.md) 的「合入基线、重放与 #21 的合成」）。本记录其余结论（容量、延迟、判定、限制）不受影响。

## 待人工确认

- 本票**未** push、**未**关闭 Issue、**未**合并到 `main`、**未** rebase（历史的 rebase 由协调人执行）；分支 `codex/21-concurrency-acceptance` 上的提交由总协调人决定合入顺序。
- **`docs/STATUS.md` 本轮未改**（S11）：该文件由协调人持有，且阶段/能力状态只在其中维护、本记录不复述状态；本票的合入状态与「#21 已验收/受阻项」的表述需要协调人在合入时统一回接，本记录只提供素材（判定表 + 受阻项表）。
- 「受阻项」五条中，前两条取决于 ADR-0010 的就绪门槛何时满足；第三条取决于票据契约是否会出现多目标调用；第四条取决于真实执行端；第五条不在本条范围。请确认这组口径是否按本记录留在 #21（而不是另开票）。
- 「已知限制」里的三处归属（公平口径归 P2-E/#57、`agentd` 轮转窗口缺陷另立、`quarantine` 标记归 P2-B/#57）请在 Issue 上确认登记位置。

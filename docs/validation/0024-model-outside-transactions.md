# P2-A1 事务外模型请求与独立任务、会话与决策身份（#43）实施评审与验证

状态：实现与检查已完成，**未合入**。日期：2026-10-11。工作树 `D:\code\huntweave-wt\43-p2-a1-model-outside-transactions`，分支 `codex/43-p2-a1-model-outside-transactions`，起点 `main` `c8033c8`（已含 #37 与 #38）。对应 [#43](https://github.com/kksty/HuntWeave/issues/43)，依据 [0003 §2.2/§5/§8.2](../specs/0003-agent-research.md)、[0006 §1/§2/§4/§11](../specs/0006-state-model-and-delivery.md)、[ADR-0014](../adr/0014-execution-lifecycle-and-environment-identity.md)、[0010 §9](../specs/0010-phase0-source-inventory.md)。

本记录只写**本切片实际做了什么、实际跑了什么**。它不改变阶段状态（`docs/STATUS.md` 不在本切片范围），不复述设计规格，也不把「已实现」写成「已交付主线」。

## 环境与入口

| 项 | 值 |
| --- | --- |
| Python | 复用主仓库虚拟环境 `D:\code\HuntWeave\backend\.venv\Scripts\python.exe`（pytest 9.1.1），未重建 |
| 工作目录 | worktree 的 `backend/`（`pytest` 按 rootdir 解析 `pythonpath=src`） |
| 纯检查 | `python -m pytest -m "not integration" -q` |
| 真实数据库检查 | 一次性独立容器 `hw-a1-model-postgres-1`，`127.0.0.1:18899`，镜像 digest `postgres@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826`，角色/模式按 `deploy/postgres/init.sh` 建立，用完即删 |
| 未触碰 | 常驻项目 `huntweave`（`huntweave-app-1`/`-runner-1`/`-postgres-1`）、#44 的 `huntweave-i44-facts-postgres-1`(#18777)、#45 的 `hw-e2-retention-postgres-1`(#18455) 均未启停、未清理 |
| 未接触 | 任何外部目标；全部检查只用 `192.0.2.0/24` 文档保留地址与固定假执行 |
| 数据库隔离方式 | 自有容器名 + 自有回环端口；`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` 只对本容器设置；`deploy/compose.isolated-db.yaml`（#21 引入，本 worktree 无此文件）在主线可用，本切片改用等价的 `docker run` 一次性容器，效果相同（独立库、独立端口、用完即删） |

## 实施评审（PROJECT §14）

### 现状是什么（基线 `c8033c8` 的真实代码）

1. **模型确实在持有 Run 行锁的事务内被调用。** `backend/src/huntweave/runs/orchestration.py:286` 的 `with Session(self.engine()) as session, session.begin():` 打开事务，紧接着 `:287` 的 `self._run(...)` 执行 `select(Run).where(...).with_for_update()`（Run 行锁），`:288` 又对 `ResearchTask` 加 `with_for_update()`；而 `:334` 的 `decision = self.model.decide(task.role, task.step, agent.context)` 仍在同一个事务里。慢 Adapter 会把 Run 行锁连同事务一直握到返回为止，而 `control()`（pause/cancel）、`accept()`（结果结算/对账）、`hold_for_readiness()` 全都要先取同一把 Run 行锁。
2. **模型没有 Interface。** `harness/model.py:15` 只有一个具体类 `DeterministicModel`，`decide(role, step, context)` 直接返回 dict；适配器由业务侧持有（`runs/orchestration.py:113` 的 `self.model = DeterministicModel()`），检查通过属性赋值替换（`tests/test_orchestration_integration.py:168`）。真实/确定性适配器没有共同可检查的契约。
3. **身份只由 `run_id + role(+step)` 决定，同角色两个 Worker 必然碰撞：** `runs/orchestration.py:158` `stable(run.id, "task:" + role)`、`:193` `stable(run.id, "session:" + role)`、`:300` `stable(run.id, f"decision:{task.role}:{task.step}")`、`:449` `stable(decision_id, "call")`。一个 Run 里同一角色只能有一行 task/session，第二次建任务会返回同一行，多轮补证也只会覆盖同一处身份。
4. **没有任何模型请求的持久记录。** `storage/models.py:102-108` 的 `Decision` 只有 `id/run_id/session_id/step/content`；没有输入快照、输入 hash、水位、版本、代次、provider/model、用量，也没有「请求已发出但结果未知」的表示。重放之所以不重复派发，靠的是 `Decision` 主键确定性，而不是「已提交结果被复用」的可核查记录。
5. **晚到建议完全没有路径。** 模型返回后直接进入派发，不再检查 Run 状态、租约代次或授权窗口；`plan()` 也没有「记录但不应用」的分支。

### 缺什么

- 一段**不持锁**的模型调用边界：准备（固定快照/水位/预算/版本）与提交（复核版本后落库）必须是两个短事务，模型调用在两者之间且不持任何锁。
- 一个**任务/会话/决策/调用都绑到独立任务与稳定尝试**的身份方案，让同角色两个 Worker、同一角色多轮补证可并存。
- 一张**模型请求记录**：请求意图、输入快照与 hash、水位、预算、版本、代次、结果、来源（provider/model/提示词版本）、用量（含「没有回执」）。
- 一条**晚到建议的处置路径**：留下来源与用量、拒绝应用、不改动已停止的工作。
- 一个**真实与确定性适配器共用的 Interface**，且框架/供应商类型不进入 `contracts/` 与 `runs/` 的公共类型。

### 本切片补什么，以及每一段的锁

三段协议落在 `runs`（准备/提交）与 `harness`（判断）之间，`harness/graph.py:39` 的 `ResearchHarness.plan_step` 是唯一的组合点：

| 段 | 位置 | 事务与锁 | 做什么 |
| --- | --- | --- | --- |
| 1 准备 | `runs/orchestration.py:434` `begin_planning` | 一个短事务：**Run 行锁 + Task 行锁**，只写到模型请求行为止 | 校验租约/代次/Run 状态；已提交的决策直接复用（或已授权的重派）；无在途调用且授权窗口有效后，写 `decisions` 行：`status=requested`、`input_snapshot`/`input_hash`、`input_watermark`、`budget_snapshot`、`run_version`/`task_version`/`scope_version`/`lease_generation`、`prompt_version`、`request_count=1`、`usage_state=unknown`；发 `model_requested` 事件 |
| 2 判断 | `harness/graph.py:39` `plan_step` → `harness/model.py:83` `ModelAdapter.decide` | **无事务、无任何行锁** | 用冻结输入构造 `ModelRequest`，调用适配器，得到 `ModelSuggestion`（内容 + 用量回执 + provider/model/提示词版本） |
| 3 提交 | `runs/orchestration.py:513` `commit_planning` | 一个短事务：**Run 行锁 + Task 行锁 + 该请求行锁** | 先落库答案与来源用量（`_record_answer`，`:755`），再用 `application_refusal`（`:144`）复核 Run 状态、授权窗口、租约代次、任务/Run 版本；拒绝则 `_refuse_attempt`（`:769`）只记录不应用；接受则 `_dispatch`（`:864`）原子提交决策 + 至多一个调用 + 预算预留 + outbox + 事件 |

`runs` 不再持有任何适配器（`orchestration.py` 里没有 `DeterministicModel`/`ModelAdapter`/`.decide(`，由检查固定），框架与供应商类型只出现在 `harness/model.py`。

身份方案（`runs/orchestration.py:103-123`）：

```
task_identity(run, role, ordinal)        # 独立任务：同角色第 N 个任务
session_identity(task)                  # 会话绑到任务，不再绑到角色
attempt_identity(task, step, ordinal)   # 稳定尝试：任务 + 步骤 + 尝试序号
call  = stable(decision_id, "call")     # 调用仍绑到自己的决策
```

`research_tasks.ordinal` + `UNIQUE (run_id, role, ordinal)` 让「同角色两个 Worker」在数据库层面也不可能合并成一行；`open_follow_up_task`（`:617`）是补证/第二个 Worker 的业务入口。

模型用量（`0003` §5 的「未知用量仍待核对」）表示为：`decisions.usage`（供应商回执原文，可为 NULL）与 `decisions.usage_state`（`known`/`unknown`）；`model_usage_summary`（`:127`）把未获回执的请求单独计入 `unknown` 并列入 `pending_attempt_ids`，**不折进已知总额、也不记 0**；`record_model_usage`（`:585`）是补记回执的唯一入口，改已记回执返回 `reconciliation_conflict`（复用既有原因码）。

## 迁移

新增 **`0009_planning_attempt_identity`**，`down_revision = "0008_retention_decisions"`（合入时由协调人核对顺序；协调人已确认 #43→#44→#45 的合并顺序，本切片不必改 `down_revision`）。`storage/database.py:15` 的 `BUSINESS_REVISIONS` 在同一提交内改为 `("0008_retention_decisions", "0009_planning_attempt_identity")`。

**未新建表**，只新增列/约束/索引（因此 `0010 §3.3` 的表清单守卫不受影响）：

| 表 | 新增 | 数量 |
| --- | --- | --- |
| `research_tasks` | 列 `ordinal`；唯一约束 `uq_research_tasks_run_role_ordinal (run_id, role, ordinal)` | 1 列 + 1 约束 |
| `decisions` | 列 `task_id`、`attempt_ordinal`、`status`、`input_snapshot`、`input_hash`、`input_watermark`、`budget_snapshot`、`run_version`、`task_version`、`scope_version`、`lease_generation`、`prompt_version`、`provider`、`model_name`、`request_count`、`usage`、`usage_state`、`reason_code`、`created_at`、`finished_at`、`supersedes_id` | 21 列 + 1 索引（`ix_huntweave_decisions_task_id`）+ 2 外键（`fk_decisions_task_id`、`fk_decisions_supersedes_id`） |

既有行的语义：`status` 回填默认 `applied`（旧路径只在模型答完并派发后才写这行），`task_id` 从 `agent_sessions.task_id` 回填后置为 `NOT NULL`，`usage_state` 默认 `unknown`（从未记录过任何用量回执，记 0 就是编造数字）。

实际应用与回读（一次性库，只读查询）：

```
alembic_version: 0009_planning_attempt_identity
research_tasks columns (10): ... ordinal integer nullable=NO
decisions columns (26): id, run_id, session_id, step, content, task_id, attempt_ordinal, status,
  input_snapshot, input_hash, input_watermark, budget_snapshot, run_version, task_version,
  scope_version, lease_generation, prompt_version, provider, model_name, request_count, usage,
  usage_state, reason_code, created_at, finished_at, supersedes_id
constraints on research_tasks: research_tasks_pkey | research_tasks_run_id_fkey |
  uq_research_tasks_run_role_ordinal UNIQUE (run_id, role, ordinal)
indexes on decisions: decisions_pkey | ix_huntweave_decisions_run_id | ix_huntweave_decisions_task_id
foreign keys on decisions: decisions_run_id_fkey | decisions_session_id_fkey |
  fk_decisions_supersedes_id | fk_decisions_task_id
```

**回退**：`downgrade()` 丢弃新增列。空库可完整往返（`decisions` 列数 26 → 5 → 26，`alembic_version` 0009 → 0008 → 0009）；但一旦库中真有「同一 Run 同角色多个任务」，丢 `ordinal` 就不可逆地把两行合成不可区分的两行，重新 upgrade 会因唯一约束失败。因此 `downgrade()` 显式拒绝这种情况（而不是静默毁掉区分）：

```
now the same downgrade on a database that used the new identity:
  downgrade refused: This database holds more than one task of the same role in a Run, so dropping
  research_tasks.ordinal would merge them irreversibly; the downgrade is refused.
  still at: 0009_planning_attempt_identity
```

## 检查清单与命令输出

### 新增检查

| 检查 | 位置 | 覆盖 |
| --- | --- | --- |
| 21 项纯检查 | `backend/tests/test_planning_identity.py` | 身份派生（同角色两个 Worker、同角色第二轮、同步骤新尝试、重放）、晚到建议的四种拒绝码与优先级、未知用量不折零、三段顺序「准备→模型→提交」、重放不唤醒适配器、一个 Interface 承载两类适配器、`contracts/` 无框架/供应商类型、`runs` 不持有适配器 |
| 7 项真实 PostgreSQL 检查 | `backend/tests/test_model_outside_transactions_integration.py` | 阻塞 Adapter 下锁探针/续租/对账/暂停仍推进（验收 1、4）、取消后的晚到建议（验收 4）、授权到期后的晚到建议、重放复用已提交答案且只派发一次（验收 3）、同角色两个 Worker 不碰撞（验收 2）、`RunSnapshot` 严格契约接受带尝试的快照 |

锁探针自带**负向对照**：故意持锁时 `SELECT ... FOR UPDATE NOWAIT` 必须报错（`test_the_lock_probe_reports_a_lock_that_is_really_held`），所以「没有锁」不是探针失灵。

### 命令与结果

```
$ cd <worktree>/backend
$ python -m pytest -m "not integration" -q
307 passed, 15 skipped, 41 deselected in 31.77s

$ python -m pytest -p no:cacheprovider -q tests/test_model_outside_transactions_integration.py \
    tests/test_orchestration_integration.py tests/test_reconciliation_integration.py
# 环境：一次性库 127.0.0.1:18899（HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1，app/checkpoint/migrator 口令文件指向临时目录）
27 passed in 9.47s

$ python -m ruff check src tests
All checks passed!

$ python -m mypy --config-file pyproject.toml src
Success: no issues found in 51 source files
```

纯检查基线为 285 passed / 8 skipped / 41 deselected，本切片新增 21 项纯检查与 1 项确定性适配器用量检查（`tests/test_real_plan_and_mode.py`），并新增 7 项数据库检查（纯检查运行中跳过）。

**未跑的三项**：容器内检查（`deploy/compose.verify.yaml` 的 checks 容器）、靶场探针（`deploy/verify_action.py` 等）、浏览器验收。理由与限制见下。

## 变异验证

「检查存在但守不住结论」在本仓库刚被记为缺陷（[0023](./0023-phase0-source-inventory.md) 的更正节）。因此把每个新结论**写错一次**，确认对应检查变红，再还原（脚本对每个变异 `try/finally` 还原原文）。11 个变异全部变红，没有一个是恒绿：

| 变异 | 把什么写错 | 目标检查 | 结果 |
| --- | --- | --- | --- |
| M1 | 在持 Run 行锁的事务里调用适配器（复现被移除的缺陷） | 阻塞 Adapter 数据库检查 | RED `1 failed in 0.94s` |
| M2 | `task_identity` 丢掉序号，退回「只有角色」 | 同角色两个 Worker 纯检查 | RED `1 failed in 0.60s` |
| M3 | 已停止的 Run 不再拒绝晚到建议 | 晚到建议拒绝码纯检查 | RED `7 failed in 0.59s` |
| M3b | 同上，在数据库级别 | 取消后晚到建议检查 | RED `1 failed in 0.78s` |
| M4 | 未知用量折进已知总额 | 用量不折零检查 | RED `1 failed in 0.58s` |
| M5 | 判断之后再跑一次「准备」 | 三段顺序检查 | RED `1 failed in 0.58s` |
| M6 | 复用分支仍然唤醒适配器 | 重放不唤醒适配器检查 | RED `1 failed in 0.61s` |
| M7a | `contracts/` 里 `import openai` | 契约层无框架/供应商类型检查 | RED `1 error in 0.06s`（收集期导入失败，仍是失败） |
| M7b | `runs` 里调用 `self.adapter.decide(...)` | `runs` 不持有适配器检查 | RED `1 failed in 0.59s` |
| M8 | 已提交答案不再复用（每步重新问模型） | 重放复用/单次派发检查 | RED `1 failed in 0.74s` |
| M9 | 尝试视图多带一个未声明字段 | `RunSnapshot` 严格契约检查 | RED `1 failed in 0.82s` |

## 逐条验收结论

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 移除持 Run 行锁期间调用模型的路径；慢 Adapter 阻塞时暂停、取消、续租和对账仍可执行 | **通过** | 模型调用在 `harness/graph.py:39` 的 `plan_step` 中、两次短事务之间，`runs` 不再持有适配器（检查 `test_the_runs_module_never_reaches_for_an_adapter`）。数据库检查在 Adapter 阻塞期间证明：`FOR UPDATE NOWAIT` 探针可取得 Run 行（负向对照证明探针有效）、同一任务续租成功、对同一 Run 的结算/对账成功提交、`control(pause)` 成功返回；M1 变异（把模型调用放回持锁事务）使该检查变红 |
| 2 | 任务/会话/decision/call 身份绑定独立任务、状态版本及稳定尝试；同角色两个 Worker 和多轮补证不碰撞 | **通过**（碰撞面）／**部分**（是否真调度出第二个 Worker 属 #49） | `task_identity`/`session_identity`/`attempt_identity`（`runs/orchestration.py:103-123`）+ `UNIQUE (run_id, role, ordinal)`；检查覆盖同角色两个 Worker 的任务/会话/尝试/决策/调用两两不同、同角色第二轮步骤 0 不碰撞、重放同序号幂等（纯检查 4 项 + 数据库 `open_follow_up_task` 与唯一约束拒绝重复行）。「一个 Run 实际并行跑两个同角色 Worker」的调度策略归 P2-B（#49），本切片只保证身份不碰撞 |
| 3 | 持久记录请求意图、输入版本与模型结果，重放复用已提交结果，不重复派发或结算；未知模型用量仍待核对 | **通过** | `decisions` 行在模型调用前写入（`status=requested` + 输入快照/hash/水位/预算/版本/代次），答案、来源与用量在提交段落库；重放走 `_applied_decision` 复用已提交答案，数据库检查断言适配器只被问 1 次、只有 1 个调用、1 条预留、1 条 `tool_planned`、1 条 `model_requested`；未知用量经 `usage_state`/`model_usage_summary.pending_attempt_ids` 单独列出，`record_model_usage` 才使其离开待核对列表，**从不记 0**（M4/M8 变异变红） |
| 4 | 取消/到期后的晚到建议留下来源和用量但拒绝应用，不恢复已停止工作 | **通过** | `application_refusal`（`:144`）+ `_refuse_attempt`（`:769`）：Run 已取消 → `invalid_run_state`，授权窗口关闭 → `authorization_expired`；两条数据库检查断言 `planning[-1].status == "refused"`、`reason_code` 正确、`suggestion`/`provider`/`model`/`usage` 已记录、`calls == []`、`decisions == []`、Run 仍是 `cancelled`/`waiting` 而未回到 `running`、任务仍 `cancelled`（M3/M3b 变异变红） |
| 5 | 真实/确定性 Adapter 走相同 Interface；框架及供应商类型留在 harness | **通过** | `harness/model.py:83` 的 `ModelAdapter` Protocol + `ModelRequest`/`ModelSuggestion`/`ModelUsage`；`DeterministicModel` 与检查里的两类适配器（阻塞无回执、计数有回执）都实现同一 `decide`；结构检查拒绝 `contracts/` 内的框架/供应商导入与 `runs/` 内的适配器引用（M7a/M7b 变异变红）。**本切片不含真实供应商适配器**（P2-C/#54）；Interface 与「无回执」语义是它落地的前置 |

## 共享面影响

- **改动的 `contracts/` 文件**：只有 `contracts/orchestration.py`（本切片独占）。新增 `ModelUsageView`、`PlanningAttemptView`、`ModelUsageSummary`；`TaskView` 增 `ordinal`；`DecisionView` 增 `task_id`；`RunSnapshot` 增 `planning`/`model_usage`（均为必填，`snapshot()` 总是提供）。
- **`contracts/errors.py` 的原因码：未新增任何原因码。** 晚到建议复用 `invalid_run_state`/`authorization_expired`/`lease_stale`/`version_conflict`，用量回执冲突复用 `reconciliation_conflict`，非法角色复用 `invalid_request`。因此 `tests/test_reason_codes.py` 与 `frontend/src/workspace.ts` **无需改动**（两者在本切片的纯检查中保持通过）。
- **`api/app.py`：未改，也不需要新入口。** 新增的 `planning`/`model_usage` 通过既有 `GET /api/v1/runs/{run_id}` 快照返回（`RunSnapshot` 严格契约已由检查覆盖）。补记模型用量目前只在业务 Interface 上（`record_model_usage`）；若协调人要暴露为 HTTP，需要新增一个导出 router 工厂，本切片**不**提供那一行，因为它不是任何一条验收标准的必要条件。
- **`docs/specs/0010-phase0-source-inventory.md`、`contracts/phase0.py`、`docs/STATUS.md`、`PROJECT.md`、`docs/adr/**`、`frontend/**`：均未改。**
- **表清单守卫**：本切片**未新建表**，`tests/test_phase0_contracts.py::test_the_inventory_document_lists_the_tables_the_code_really_has` 保持通过（不需要协调人补 `0010 §3.3`）。
- **迁移链**：新增 `0009_planning_attempt_identity`（`down_revision=0008_retention_decisions`），`BUSINESS_REVISIONS` 同提交同步；`down_revision` 未改，等协调人按 #43→#44→#45 顺序合入。

## 未达成与限制

1. **未跑容器内检查与靶场探针**：本切片没有新的执行端行为，`deploy/verify_*.py` 与 checks 容器未复跑；`lab/isolation/action.py` 只做了适配新 Interface 的机械改动（`self.plan(...)`），**未在靶场复验**。
2. **未做浏览器验收**：`RunSnapshot` 新增字段只经严格契约校验；前端类型与渲染未接入（归 P2-F/#65）。
3. **真实模型仍不存在**：Interface 已就位，但没有供应商适配器、没有 token 计费与模型并发槽；`budget_snapshot` 记的是工具调用额度画面，不是模型额度预留（`0003` §2.2 的「预留模型额度」在无计费来源时只能记「无回执」，未做假账）。
4. **模型调用失败没有持久化**：适配器抛异常时请求行停留在 `requested`、用量为 `unknown`（待核对），但本切片没有「模型不可用/限流/Schema 错误」的原因码与有界退避——那是 `0003` §5 的后续切片（P2-B/#49 与 P2-C/#54）。当前失败会让 agentd 的调度轮次中止（既有行为，未变）。
5. **崩溃窗口**：答案只在提交段落库。若进程在「模型已答、提交段未跑」之间死掉，该请求行停在 `requested`（用量待核对），恢复后按新尝试重新提问；`0003` §2.2 的「崩溃前已保存的有效模型结果优先复用」只在本切片覆盖「已提交结果」，未实现「已答未提交」的第三条写入。它不影响正确性（不重复派发、不重复结算），但会多花一次模型请求。
6. **第二个同角色 Worker 的调度**：`open_follow_up_task` 是业务入口，实际「何时开第二轮」的触发规则（Reviewer 补证建议 → 新任务）归 P2-B（#49/#50），本切片不做。
7. **`RunSnapshot` 不允许未声明字段**：真实 profile 的确定性决策会把 `target_ip`/`target_port` 写进 `decisions.content`，而 `DecisionView` 不声明这两个字段。这是**既有**的潜在问题（旧代码同样把 `content` 展开进快照），本切片未改；一旦真实 profile 的 Run 被控制台读取，需要另行处理（不在 #43 范围）。
8. **降级不完整**：见上「迁移」一节，有数据的库上 `downgrade()` 会显式拒绝。
9. 未更新 `docs/STATUS.md` 与 `docs/validation/README.md` 之外的索引（根 README 无逐条记录索引）；STATUS 的「下一实施项」由协调人在合入时更新。

## 待人工确认

1. **迁移顺序**：本切片已按协调人确认的 #43→#44→#45 顺序保留 `down_revision=0008_retention_decisions`；合入时仍需核对单一 head。
2. **`PlanningAttemptView` 的取名与暴露面**：尝试行复用 `decisions` 表（不新建表），字段名 `planning`/`model_usage` 是新增的读面；若 P2-D6（#70）的冻结 manifest 需要这些输入，应引用同一视图而不是另建一份。
3. **`record_model_usage` 是否需要 HTTP 入口**：当前只有业务 Interface 与检查使用它。若运营上需要人工补记供应商账单，需协调人加一行 `include_router`。
4. **`usage_state` 的既有行语义**：迁移把既有 `decisions` 行记为 `unknown`（从未记录过用量回执）。如果协调人认为历史 Run 不应进入「待核对模型用量」列表，需要另定规则（本切片不改）。

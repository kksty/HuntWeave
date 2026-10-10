# P2 Phase 0：来源盘点、六轴接口与冻结输入

状态：盘点与契约已固定，**未实施**。日期：2026-10-11。对应 [#38](https://github.com/kksty/HuntWeave/issues/38)，依据 [0006 §10](./0006-state-model-and-delivery.md#10-phase-0映射与兼容策略) 与 [0003 §8 实施切片与验收](./0003-agent-research.md#8-实施切片与验收)（其中 §8.1 是实施前 Phase 0）。本文件是 **Phase 0 的契约交接件**：它盘点真实代码与迁移历史，固定后续 P2 切片共享的字段、引用方向、冻结输入与写入方。它不重新定义 P0，不提前实现 P2，也不宣称任何能力已交付。

**本文件不复制 0006/0003 的设计文字**，只记录「真实代码里现在是什么」以及「后续切片在哪落脚」。取值与业务规则仍以 [0006](./0006-state-model-and-delivery.md)、[0004](./0004-finding-admission.md)、[0005](./0005-capability-claim-criteria.md) 为唯一来源；契约口径更正以 [ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md) 与[验证记录 0020](../validation/0020-contract-alignment.md) 为准。实际核对命令、输出与逐条验收判定见[验证记录 0023](../validation/0023-phase0-source-inventory.md)。

## 1. 本次实施评审结论（PROJECT §14）

对照真实代码、迁移链与现存数据逐项复核后，本切片的开工结论如下。**评审的是「0006 §10 的描述是否与代码一致」，不是「P2 是否可以开始写业务」**——后续切片各自仍须按 PROJECT §14 完成自己的实施评审。

| # | 复核项 | 结论 |
| --- | --- | --- |
| 1 | 0006 §10 首段的对象清单（Run/ResearchTask/AgentSession/Decision/ToolCall/ToolResult/Evidence/ReconciliationDecision） | **成立但不完整**：八个对象都存在，然而真实 schema 另有 14 个对象（见 §3.3），原作者未列 |
| 2 | 0006 §10「没有完整 Claim、Attempt、Finding、Review、准入或 frozen manifest 表」 | **成立**：`class Claim` / `Finding` / `Review` / `Attempt` / `FrozenManifest` 在 `backend/src` 中匹配数均为 **0**，`__tablename__` 声明数与迁移建表数均无对应项 |
| 3 | `Host` / `Service` / `WebEndpoint`（#44 需要） | **确实不存在**：`class Host` / `class WebEndpoint` 匹配数 **0**；`class Service` 唯一命中是 `contracts/errors.py:1` 的 `ServiceError`，不是领域对象 |
| 4 | 0006 §10「现存核对 `outcome=undetermined` 是『调用是否发生仍未决』」 | **成立，且当前库中没有这种记录**：`ReconciliationOutcome` 定义在 `contracts/orchestration.py:12`，但 `reconciliation_decisions` 表 **0 行**，`tool_calls.status` 只出现 `succeeded` / `cancelled` |
| 5 | 0006 §0.1 的旧 Finding 值（`suspected`/`supported`/`verified`/`inconclusive`/`refuted`/`admitted`/`stale`） | **表里是设计文档的值，不是库里的数据**：库里没有任何 Finding 对象或这些取值，因此**一条迁移事实都不产生**（§4.2） |
| 6 | `EXECUTION_POLICY_VERSION` 与「策略版本随授权快照固定」 | **成立**：`contracts/runs.py:23` 定义，`:106` 写入 `ScopeSnapshot.policy_version`，旧 Run 不被当前构建改写 |
| 7 | `BUSINESS_REVISIONS` ≡ 迁移 head | **成立**：`storage/database.py:15` 为 `("0008_retention_decisions",)`，与 §5 的链尾一致 |
| 8 | 六轴是否已有载体 | **A1 / A2 / A4 完全没有载体**；A3 只有调用的那一半；A5 / A6 有部分载体（§6） |
| 9 | 冻结输入是否可生产 | **不完整**：`MANIFEST_INPUTS` 共 49 个字段，其中 `available` 6、`partial` 13、`absent` 30（§7.3）；`manifest_gaps()` 另有 43 个缺口 |

**评审判定：可以开工，但后续切片不得假设本文件之外的旧数据存在。** 三处必须在实施中处理的缺口：A4→A1 的单向引用没有载体（§5.1）、30 个冻结输入字段没有生产者（§7.3，即 `manifest_gaps()` 的 43 个缺口覆盖部分）、`dual_read` 目前无事可做（§8）。

## 2. 盘点基线与范围

- 起点：`origin/main` `1e5b0eb`（已含 #37）。仓库 `kksty/HuntWeave`，Issue #38 正文声明的盘点基线是 2026-10-11 / `main` `4f0d063`；本 worktree 的起点比它新，已含 #37 与 #77 的后续提交。
- 盘点范围：`backend/src/huntweave/**`、`backend/migrations/**`、`backend/tests/**`。前端、`deploy/`、`lab/` 不在本切片范围（字段归 B/D、资源归 E、UI 归 F）。
- 核对方式：源码与迁移**静态读取**，加上对**本机常驻部署**的只读 `information_schema` 查询。**没有启动任何新栈、没有执行迁移、没有写任何一行业务数据。**
- 与 #37 的关系：本文件在 ADR-0026 固定的契约口径上开工，不与之矛盾。`engagement_mode` / `PolicyGate` / `StopPolicy` / `required_confirmation_level` 在代码中匹配数仍为 **0**；CIDR 边界仍**未决**，保留 IP-only 与 `TARGET_LIMIT = 100`。

## 3. 来源盘点（0.1）

### 3.1 ORM 类（`storage/models.py`，18 张有 ORM 类的表）

`Base.__table_args__` 固定 `schema="huntweave"`（`models.py:15`）。文件里共 19 个类，其中 18 个声明了 `__tablename__`；第 19 个是 `__abstract__ = True` 的 `Base` 自身，不建表（因此不在下表里）。18 个声明为：

| # | 行 | 类 | `__tablename__` |
| --- | --- | --- | --- || 1 | 18 | `AccessKeyState` | `access_key_state` |
| 2 | 25 | `WebSession` | `web_sessions` |
| 3 | 38 | `Project` | `projects` |
| 4 | 46 | `AuthorizationScope` | `authorization_scopes` |
| 5 | 55 | `Run` | `runs` |
| 6 | 77 | `ResearchTask` | `research_tasks` |
| 7 | 92 | `AgentSession` | `agent_sessions` |
| 8 | 102 | `Decision` | `decisions` |
| 9 | 111 | `ToolCall` | `tool_calls` |
| 10 | 138 | `ReconciliationDecision` | `reconciliation_decisions` |
| 11 | 163 | `RetentionDecisionRecord` | `retention_decisions` |
| 12 | 189 | `ToolResult` | `tool_results` |
| 13 | 198 | `BudgetReservation` | `budget_reservations` |
| 14 | 207 | `Outbox` | `execution_outbox` |
| 15 | 215 | `Evidence` | `evidence` |
| 16 | 223 | `EventCursor` | `event_cursors` |
| 17 | 229 | `AuditEvent` | `audit_events` |
| 18 | 240 | `InterruptionRecord` | `interruption_records` |

（此表 18 行，正好对应 18 个 `__tablename__` 声明。第 19 个类是 `Base` 自身，`__abstract__ = True`，不建表；19 张业务表里另 1 张 `runtime_processes` 以 raw SQL 建表、没有 ORM 类，见 §3.3。）

### 3.2 迁移链（`backend/migrations/versions/`，8 个 revision，单一 head）

| revision | down_revision | 文件 | 实际 DDL |
| --- | --- | --- | --- |
| `0001_runtime` | `None` | `0001_runtime.py` | 建 `runtime_processes`(`name` PK, `instance_id`, `heartbeat_at`) |
| `0002_identity_runs` | `0001_runtime` | `0002_identity_runs.py` | 建 `access_key_state`、`web_sessions`、`login_buckets`、`projects`、`authorization_scopes`(+`project_id` 索引)、`runs`(+`ck_runs_versions`、`ck_runs_status`、`project_id` 索引) |
| `0003_orchestration` | `0002_identity_runs` | `0003_orchestration.py` | `runs` 加 `demonstration_scenario`、`demonstration_duration_ms`、`started_at`、`reason_code`；**循环建 7 张表** `research_tasks`、`agent_sessions`、`decisions`、`tool_calls`、`budget_reservations`、`evidence`、`interruption_records`（各带 `run_id` 索引）；另建 `tool_results`、`execution_outbox`、`event_cursors`、`audit_events`(`(run_id, cursor)` PK + `(run_id, source_event_id)` 唯一) |
| `0004_reconciliation` | `0003_orchestration` | `0004_reconciliation.py` | `tool_calls` 加 `replaces_call_id`(+自引用外键 `fk_tool_calls_replaces_call_id`)、`observation`；建 `reconciliation_decisions`(+`run_id` 索引) |
| `0005_run_execution_profile` | `0004_reconciliation` | `0005_run_execution_profile.py` | `runs` 加 `execution_profile` |
| `0006_call_runtime` | `0005_run_execution_profile` | `0006_call_runtime.py` | `tool_calls` 加 `runtime`、`progress` |
| `0007_drop_login_throttle` | `0006_call_runtime` | `0007_drop_login_throttle.py` | **删** `login_buckets`（`downgrade` 可重建） |
| `0008_retention_decisions` | `0007_drop_login_throttle` | `0008_retention_decisions.py` | 建 `retention_decisions`(`decision_id` PK)，**无 `run_id`**、无索引 |

**当前单一 head = `0008_retention_decisions`**，与 `storage/database.py:15` 的 `BUSINESS_REVISIONS` 一致。链是线性的：8 个 revision、8 条 `down_revision` 边（含 1 个 `None` 根），无分支、无悬挂。

### 3.3 完整表清单（真实 schema）

真实 schema = 19 张业务表 + `alembic_version` + 已删除的 `login_buckets`（历史），另有独立 schema `huntweave_checkpoint` 的 4 张库表。**`0006 §10` 只列了其中 8 个对象**，遗漏 14 项（下表「0006 §10」列标 ✗）。

| 表 | ORM 类 | 迁移来源 | 写入方 | 0006 §10 |
| --- | --- | --- | --- | --- |
| `access_key_state` | `AccessKeyState` | 0002 | `access/service.py` | ✗ |
| `web_sessions` | `WebSession` | 0002 | `access/service.py` | ✗ |
| `projects` | `Project` | 0002 | `runs/service.py` | ✗ |
| `authorization_scopes` | `AuthorizationScope` | 0002 | `runs/service.py` | ✗ |
| `runs` | `Run` | 0002(+0003/0005 加列) | `runs/service.py`、`runs/orchestration.py`、`runs/suspension.py` | 列为 Run |
| `research_tasks` | `ResearchTask` | 0003 | `runs/orchestration.py` | 列为 ResearchTask |
| `agent_sessions` | `AgentSession` | 0003 | `runs/orchestration.py` | 列为 AgentSession |
| `decisions` | `Decision` | 0003 | `runs/orchestration.py` | 列为 Decision |
| `tool_calls` | `ToolCall` | 0003(+0004/0006 加列) | `runs/orchestration.py`、`runs/dispatch.py` | 列为 ToolCall |
| `tool_results` | `ToolResult` | 0003 | `runs/orchestration.py`、`runs/dispatch.py` | 列为 ToolResult |
| `evidence` | `Evidence` | 0003 | `runs/dispatch.py`（Runner 归档后落库） | 列为 Evidence |
| `reconciliation_decisions` | `ReconciliationDecision` | 0004 | `runs/orchestration.py`（操作员裁定） | 列为 ReconciliationDecision |
| `runtime_processes` | **无**（raw SQL） | 0001 | `api/agentd.py:63`（心跳 upsert） | ✗ |
| `budget_reservations` | `BudgetReservation` | 0003 | `runs/orchestration.py`、`runs/dispatch.py` | ✗ |
| `execution_outbox` | `Outbox` | 0003 | `runs/dispatch.py` | ✗ |
| `event_cursors` | `EventCursor` | 0003 | `runs/events.py:40`（唯一推进点） | ✗ |
| `audit_events` | `AuditEvent` | 0003 | `runs/events.py:45` | ✗ |
| `interruption_records` | `InterruptionRecord` | 0003 | `runs/suspension.py:52` | ✗ |
| `retention_decisions` | `RetentionDecisionRecord` | 0008 | `runs/retention.py` | ✗ |
| `alembic_version` | **无** | Alembic 自身 | `alembic upgrade` | ✗ |
| `login_buckets` | **无**（已删） | 0002 建、0007 删 | 已无写入方 | ✗ |
| `huntweave_checkpoint.checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` | 库表 | 不由 Alembic 建 | `harness/checkpoints.py:29` 的 `PostgresSaver(...).setup()` | ✗ |

**`runtime_processes` 是唯一「raw SQL 建、无 ORM 类」的表。** 通过 grep 全 `backend/src` 找 `INSERT INTO` / `CREATE TABLE` / schema 名，除它之外没有别的表以裸 SQL 访问：全部命中为 `agentd.py:63`（写心跳）、`orchestration.py:1408` 与 `database.py:45`（读心跳）。

### 3.4 现有契约模型与版本常量

| 对象 | 位置 | 说明 |
| --- | --- | --- |
| `EXECUTION_POLICY_VERSION = 1` | `contracts/runs.py:23` | 唯一「随授权快照固定」的策略版本；写入 `ScopeSnapshot.policy_version`(`:106`) |
| `BUSINESS_REVISIONS` | `storage/database.py:15` | 本构建可服务的业务 schema 版本白名单 |
| `SUPPORTED_PROFILE_VERSION = 1` | `execution/sandboxprofile.py:18` | 沙箱 profile 版本 |
| `TARGET_LIMIT = 100` | `runs/inputs.py:13` | 单 Run 去重后目标上限（CIDR 未决，见 ADR-0026 §3） |
| `TARGET_LIMIT` 相关行级拒绝 | `runs/inputs.py:51-54` | `target_limit_exceeded` 按行返回 |
| `DEFAULT_CANDIDATE_WINDOW_DAYS/RUNS`、`DEFAULT_CACHE_TTL_DAYS/CAPACITY_BYTES` | `contracts/retention.py:36-39` | 保留策略默认值（**部署配置，不属冻结输入**，见 §7.4） |
| `protocol_version = "1"` | `contracts/execution.py:196`、`contracts/capabilities.py:40` | 执行/能力协议版本 |
| `config_version = "p0-b-v1"` | `contracts/runs.py:89`、`:103` | 授权快照配置版本 |
| 4 个就绪门槛名 | `contracts/capabilities.py:14-19` | ADR-0010 的 `ReadinessGateName` |
| `contracts/` 现有模型 | `runs.py`/`execution.py`/`orchestration.py`/`retention.py`/`capabilities.py`/`errors.py` | 六个模块，共 6 个文件 |

**没有**版本常量：判据版本、要求集版本、准入政策版本、完成规则版本、主张/评估版本、图修订版本、投影 schema 版本、manifest schema 版本（最后一项本切片新增，见 §7.1）。

### 3.5 游标与 checkpoint 的 owner

| 事项 | 唯一写入方 | 说明 |
| --- | --- | --- |
| Run 事件游标 | `runs/events.py:40` | `EventCursor` 行在调用方事务内 `FOR UPDATE` 加锁并 +1，`AuditEvent` 按同一 cursor 追加；`source_event_id` 让重发成为 no-op（`:34-39`） |
| 业务 schema 版本校验 | `storage/database.py:30` | `verify_business_schema` 读 `huntweave.alembic_version` |
| checkpoint schema | `harness/checkpoints.py` | schema `huntweave_checkpoint`，`migrate_checkpoints()`(:25) 建表、`verify_checkpoint_schema()`(:34) 校验 `max(v)` 等于库自带迁移数−1 |
| agentd 单实例 | `api/agentd.py:57` | advisory lock `486735944` |
| checkpoint 迁移锁 | `harness/checkpoints.py:27` | advisory lock `486735943` |
| 公共 `reason_code` 载体 | `contracts/errors.py:1` | `ServiceError(reason_code, status_code, retry_after)`，由 `api/app.py:84-90` 渲染成 `{"reason_code": ...}` |

## 4. 旧值 / verified 对象映射（0.1 的映射部分）

### 4.1 已存在与尚不存在

| 分类 | 对象 | 证据 |
| --- | --- | --- |
| **已存在**（有 ORM 类、有迁移、有写入方） | `Run`、`ResearchTask`、`AgentSession`、`Decision`、`ToolCall`、`ToolResult`、`Evidence`、`ReconciliationDecision` | §3.1 的 8 个类 |
| **尚不存在**（`class X` 在 `backend/src` 匹配数为 0） | `Claim`、`ClaimEvaluation`、`Attempt`、`AttemptOutcome`、`Finding`、`EvidenceBinding`、`Review`、`HumanDecision`、`SeverityAssessment`、`AdmissionDecision`、`FrozenManifest` | 逐名核验，全部 0 |
| **尚不存在**（#44 需要） | `Host`、`Service`、`WebEndpoint` | `WebEndpoint` 与 `Host` 为 0；`Service` 的唯一命中是 `ServiceError` |
| **尚不存在**（0006 §10 未点名，但同属新对象） | `PreconditionGate`、`GraphProjection`、`GraphRevision`、`Observation`、`ReportRevision`、`Coverage`、`EngagementMode`、`OobReceiver` | 全部 0 |

### 4.2 映射规则与「不产生迁移事实」

规则表（含必要条件与「不能推造的事实」）以 **0006 §0.1** 为唯一来源，本文件不复制。落成可执行形式的版本在 `backend/src/huntweave/contracts/phase0.py` 的 `LEGACY_VALUE_MAPPINGS`（11 条），每条带 `required_inputs`、`forbidden_fact`，且 `executable=False`。

**本机现存数据（只读查询，2026-10-11）：**

| 表 | 行数 | 取值 |
| --- | --- | --- |
| `runs` | 46 | `draft`20 / `waiting`10 / `cancelled`9 / `closed`5 / `paused`2；`reason_code` 全为 `NULL` |
| `research_tasks` | 52 | — |
| `tool_calls` | 52 | `succeeded`45 / `cancelled`7（**无 `unknown`、无 `incomplete`、无 `denied`**） |
| `evidence` | 52 | — |
| `audit_events` | 667 | — |
| `projects` | 41 | — |
| `reconciliation_decisions` | **0** | 没有任何操作员裁定 |
| `retention_decisions` | **0** | — |

因此：

1. **没有旧 Finding 数据。** 0006 §0.1 的 `suspected`/`supported`/`verified`/`inconclusive`/`refuted`/`admitted`/`stale` 是**设计文档的取值**，库里既无 Finding 对象也无这些值。**本切片不生成任何迁移事实**，也不因此判定任何主张。
2. **`unknown` / `incomplete` 不转为主张判定。** 二者是 `ToolCall` 的执行事实（`PROJECT §8.2`，`contracts/execution.py:425` 的 `ExecutionRecord.status`）。`contracts/phase0.py` 的 `legacy_mapping_can_produce_claim_verdict()` 对它们一律返回 `False`，并由检查固定（验证记录 0023 §C）。
3. **现存 `outcome=undetermined` 不是 A1 未决。** 库里连一条都没有；定义在 `contracts/orchestration.py:12`，语义是「调用是否发生仍未决」。A1 的 `undetermined` 尚无载体。
4. **`verified` 不全局替换。** 逐对象盘点：旧 Finding 证据标签（无数据）、工具验证结果（`ExecutionResult.summary`/`status`）、能力就绪（`Capabilities.real_execution_ready`）、机器审阅（无载体）、人工裁定（`ReconciliationDecision.outcome` 与未来的 `HumanDecision`）。新名称分别是 claim confirmed、`auto_reviewed`、`independent_reviewed`、`human_confirmed`（0006 §0.2）。

## 5. 六轴字段与引用方向（0.3）

六轴是**领域维度，不要求一轴一表**。下表把 0006 §1 的每一轴落到真实载体上；`contracts/phase0.py` 的 `AXES` 是同一内容的可检查版本。

| 轴 | 取值 | 已存在的载体 | 尚不存在的载体 | 引用方向 | 谁写 |
| --- | --- | --- | --- | --- | --- |
| **A1** 主张判定 | `undetermined`/`supported`/`refuted`/`confirmed` + `lapsed` | **无** | `Claim`、`ClaimEvaluation` | **不引用任何轴**（尤其不引用 A4） | `runs` |
| **A2** 复审 | `requested`/`in_review`/`completed`/`on_hold` × `auto_reviewed`/`independent_reviewed`/`human_confirmed`/`disputed`/`sla_escalated` | **无** | `Review`、`HumanDecision`、队列目的记录 | 指向 A1（确认记录被 A1 引用） | `runs` |
| **A3** 尝试 | `active`/`awaiting_model`/`awaiting_prep`/`blocked`/`terminated_for_constraint`/`ended` | `tool_calls`（调用的那一半）、`tool_results`、`runs`/`research_tasks` 生命周期 | `Attempt`、`AttemptOutcome` | 不引用 A1/A4 | `runs`、Runner |
| **A4** 准入 | `pending_review`/`admitted_current`/`exited_invalid`/`revoked`/`expired` | **无** | `Finding`、`AdmissionDecision`、`EvidenceBinding` | **单向引用 A1**（及 A2 的 Review/HumanDecision 版本） | `runs` |
| **A5** 证据 | `proposed`/`collected`/`referenced`/`superseded`/`challenged`/`gc_pending` | `evidence`、`tool_results`、`retention_decisions` | `EvidenceBinding`、带 `validity_until` 的保留保护记录 | 不引用 A1/A4 | Runner（采集）、`runs`（引用/争议/保留） |
| **A6** 资源 | 模型/搜索/准备/复审/控制槽、全局物理额度、同 IP 占用；IP `normal`/`quarantine`/`degraded` | `budget_reservations`、`tool_calls.runtime`、`retention_decisions` | 物理执行额度账本、同 IP 占用记录 | 不引用 A1/A4 | `runs`、Runner |

### 5.1 共用与不建表的说明

- **A3 与 A6 共用 `tool_calls`**：调用的生命周期（A3）与它占用的执行事实（A6）是同一行的不同列（`status`/`observation` 对 `runtime`/`progress`）。不为 A6 另建表。
- **A5 与 A3 共用 `evidence`→`tool_calls`**：证据按 `call_id` 绑定，不复制调用事实。
- **A1/A2/A4 目前无表**，且**本切片不为它们建表**：它们属于 P2-D（#52/#56/#60/#64/#67/#70），迁移编号在合入时串行分配（§9）。
- **A4→A1 是单向引用，A1 不引用 A4**（0006 §2、§11）。本节把它写成可检查断言：`contracts/phase0.py` 的 `axis("A4").references == ("A1",)` 且 `axis("A1").references == ()`。

## 6. 确认等级与要求集版本（0.5）

**字段尚不存在**，本切片固定的是口径与落点，不是实现：

| 事项 | 现状 | 落点 |
| --- | --- | --- |
| `required_confirmation_level ∈ {auto, independent, human}` | 代码匹配数 **0** | 由版本化契约规定，随 P2-D2（#56）的判据契约落地 |
| 默认等级映射（7 类主张） | 0006 §3.1 为唯一来源；本切片不复制、不改写 | P2-D2（#56）实现；契约覆盖须按规则版本变更审核 |
| 「证据要求已满足 / 确认级就绪 / `confirmed`」三分 | 口径已由 [ADR-0026 §4](../adr/0026-contract-alignment-readiness-cidr-and-queue.md) 与 [0008 §3.5.1–3.5.2](./0008-coverage-mode.md) 固定 | P2-D2/D3（#56/#60）；本切片只固定词汇 |
| 要求集版本（`requirement_set_versions`） | 无载体 | P2-D2（#56） |
| 判据版本（`criteria_versions`） | 无载体 | P2-D2（#56） |
| 准入政策版本 | 无载体 | P2-D4（#64） |
| 完成规则版本 | 无载体（0006 §4 要求「结束提交固定完成规则版本」） | 归 P2-B（#43/#49/#50）与 P2-D6（#70）在实现时锁定 |

**锁定的词汇**（`contracts/phase0.py` 的 `CONFIRMATION_LEVELS` / `CLAIM_VERDICTS` / `CLAIM_CLASSES`）：取值集合固定，但**没有**任何切片可以在实现时自行增删；新增须改 0006 并复审。

## 7. FrozenManifest 输入 schema（0.4）

### 7.1 schema 版本

`contracts/phase0.py` 新增 `FROZEN_MANIFEST_SCHEMA_VERSION = 1`（**仅新增**，不改任何既有字段含义）。它是**输入清单的版本**，不是运行时 manifest 的 `schema_version`；运行时值为 P2-D6（#70）所有。

### 7.2 完整输入清单

机器可读版本为 `contracts/phase0.py` 的 `MANIFEST_INPUTS`（8 组、49 个字段；`manifest_gaps()` 另报 43 个缺口）；下表是同一清单的阅读版。`状态` 列：`有`＝本构建可生产，`部分`＝有载体但不是 manifest 需要的那件事，`无`＝无载体，须由后续切片创建。**`无` 与 `部分` 是缺口记录，不是占位值**：早于 P2-D6 冻结的 manifest 必须如实记「该输入缺失」，不得为它编造值。

| 组 | 字段 | 类型 | 状态 | 载体 / owner |
| --- | --- | --- | --- | --- |
| 身份 | `manifest_id` | UUID | 无 | P2-D6(#70) |
| 身份 | `schema_version` | int | 有 | `FROZEN_MANIFEST_SCHEMA_VERSION` |
| 身份 | `run_id` | UUID | 有 | `runs.id` |
| 身份 | `run_version` | int | 部分 | `run.version` 存在；冻结时取值不存在 |
| 身份 | `scope_version` | int | 有 | `runs.scope_version` |
| 身份 | `frozen_at` | datetime | 部分 | `clock_timestamp()` 存在；无 manifest 写入它 |
| 身份 | `authoritative_source` | `Literal['versioned_record']` | 部分 | `audit_events` 是增量投影来源；权威源声明不存在 |
| 规则 | `contract_versions` | dict[str,int] | 部分 | `EXECUTION_POLICY_VERSION` 是唯一版本化契约 |
| 规则 | `criteria_versions` | dict[str,int] | 无 | P2-D2(#56) |
| 规则 | `requirement_set_versions` | dict[str,int] | 无 | P2-D2(#56) |
| 规则 | `admission_policy_version` | int | 无 | P2-D4(#64) |
| 规则 | `completion_rule_version` | int | 无 | P2-B / P2-D6 |
| 主张 | `claim_id` / `claim_version` | UUID / int | 无 | P2-D2(#56) |
| 主张 | `evaluation_id` / `evaluation_version` | UUID / int | 无 | P2-D2(#56) |
| 主张 | `verdict_at_freeze` / `lapsed_at_freeze` | str / bool | 无 | P2-D2(#56) |
| 主张 | `confirmation_level_met` | `Literal['auto','independent','human']` | 无 | `required_confirmation_level` 不存在 |
| 主张 | `confirming_review_id` | UUID \| None | 无 | P2-D3(#60) |
| 材料 | `evidence_ids` | list[UUID] | 有 | `evidence.id` |
| 材料 | `evidence_collected_versions` | dict[UUID,int] | 部分 | `evidence.metadata_json`（自由 JSON） |
| 材料 | `evidence_hashes` | dict[UUID,str] | 有 | `ExecutionEvidence.sha256` |
| 材料 | `bindings` / `fragment_versions` | list[EvidenceBinding] / dict[str,int] | 无 | P2-D1(#52) |
| 材料 | `integrity_at_freeze` | dict[UUID,str] | 部分 | `truncated`/`redacted` 在视图上，未冻结 |
| 材料 | `validity_until` | datetime \| None | 无 | P2-D5(#67) |
| 材料 | `retention_until` | datetime \| None | 部分 | 保留制品的 `expires_at`；证据行没有 |
| 材料 | `retention_protection_refs` | list[str] | 部分 | `retention_decisions`（固定/删除） |
| 裁定与准入 | `review_ids` | list[UUID] | 无 | P2-D3(#60) |
| 裁定与准入 | `human_decision_ids` | list[UUID] | 无 | P2-D4(#64) |
| 裁定与准入 | `severity_assessment_ids` | list[UUID] | 无 | P2-D4(#64) |
| 裁定与准入 | `admission_decision_ids` | list[UUID] | 无 | P2-D4(#64) |
| 范围与统计 | `coverage_records` | list[Coverage] | 无 | P2-F(#61) |
| 范围与统计 | `denominator_version` | int | 无 | 须与 `total_basis` 同时存在 |
| 范围与统计 | `total_basis` | `Literal['exact','estimate','unknown']` | 无 | 先出现在读响应上 |
| 范围与统计 | `primary_anchor_version` | int | 无 | 无锚点版本 |
| 范围与统计 | `settlement_reference` | str | 部分 | `BudgetReservation.id` |
| 范围与统计 | `legacy_summary_version` | int | 部分 | `runs.reason_code` + `interruption_records`；无版本化摘要 |
| 图与环境 | `graph_revision` | str | 无 | P2-F(#61) |
| 图与环境 | `projection_schema_version` | int | 部分 | `PostgresSaver` 的库迁移数派生，未持久化 |
| 图与环境 | `filter_scope` | dict[str,Any] | 无 | P2-F(#61) |
| 图与环境 | `snapshot_artifact_hashes` | dict[str,str] | 无 | P2-D6(#70) |
| 图与环境 | `processed_cursor` | int | 有 | `event_cursors.cursor` |
| 图与环境 | `environment_versions` | dict[str,str] | 部分 | `tool_calls.runtime` 的 `image_digests` |
| 图与环境 | `tool_artifact_versions` | list[ToolVersionView] | 部分 | `ToolVersionView` 读当前状态，非冻结 |
| 时效与保存 | `manifest_validity_until` | datetime \| None | 无 | **空值须明确含义**，不填假日期 |
| 时效与保存 | `manifest_retention_until` | datetime \| None | 无 | 同上 |
| 时效与保存 | `per_input_invalidation_rules` | dict[str,str] | 无 | 0006 §9 要求每项输入各自声明失效规则 |

### 7.3 不进入冻结 manifest 的内容

按 0006 §3.2 与 ADR-0016，下列内容**不是**冻结输入（`contracts/phase0.py` 的 `NOT_IN_MANIFEST`）：

| 不进入 | 原因 |
| --- | --- |
| 队列容量/背压策略配置 | 0006 §3.2：策略配置不塞进已冻结 manifest；交付可固定**当时的队列快照引用** |
| 队列积压实时值 | manifest 只保存冻结时事实，不实时回写积压 |
| 保留策略阈值（`RetentionLimits`） | 部署运行策略，不是某次交付的输入 |
| 就绪门槛结果（ADR-0010 四项） | 现在重新读取，不把过去某刻的就绪当冻结事实 |
| 物理额度/份额设置 | ADR-0016 的部署配置 |
| 工作区/文件系统路径 | 部署位置 |
| 模型供应商凭据 | 永不出现在 manifest、日志或模型上下文 |

### 7.4 缺口汇总

49 个字段中 `available` **6**、`partial` **13**、`absent` **30**。30 个 `absent` 全部有明确 owner（P2-B/D/E/F 切片），没有一项是「待定归属」。**`partial` 的 13 项是本切片最需要注意的一类**：载体存在，但 manifest 需要的语义（冻结那一刻的事实）不存在——例如 `run_version` 有列而没有冻结值、`tool_artifact_versions` 有读模型而没有冻结快照。

## 8. 单一写入口、有界 `dual_read`、漂移与回退读

### 8.1 单一写入口

- **业务变化的唯一写入口是 `runs`**（`runs/service.py`、`runs/orchestration.py`、`runs/suspension.py`、`runs/retention.py`）。Runner 提供执行与停止事实，不写业务判断。
- **事件游标的唯一写入口是 `runs/events.py:append_event`**（`:26`）。两个写入方（编排与保留）共用它，顺序由调用方事务内的行锁决定。
- **P2 的新轴沿用这条规则**：A1–A6 的业务变化由 `runs` 提交；不在 Runner、前端或投影里新增第二写入口。投影/图只读（ADR-0015）。

### 8.2 有界 `dual_read`

| 事项 | 定义 |
| --- | --- |
| 权威 | **新接口以新轴为权威**；旧接口只由新记录生成兼容视图，不持有可写状态 |
| 旧读取路径 | 仅限「尚未转换的来源」：`tool_calls`/`tool_results`/`evidence`/`reconciliation_decisions` 的既有读接口（`api/app.py` 的 Run 快照、时间线、证据读取） |
| 隔离 | 未转换来源走**独立的转换读取路径**，并在响应上显示缺口（`available=false` + `reason_code` 的既有形制，如 `api/app.py:93-105` 的 `unobserved_runtime`） |
| 保存 | 旧原值/来源版本与**映射规则版本**（`PHASE0_INVENTORY_SCHEMA_VERSION`）一并保存 |
| 漂移 | 比较两种读取的**事实语义**，产出漂移清单 |
| 退出条件 | ①来源盘点完整；②无未解释漂移；③消费者已迁移；④回退读路径本身已验证。四条全满足才退出该路径 |
| 未决转换 | 可留存，但**不参与当前确认**（不得因为「旧读取能看到」就当已确认） |
| `dual_write` | **本切片不引入**。若实施确需，必须另定事务与单一写入口方案，并单独评审（0006 §0.1 末尾） |

### 8.3 漂移清单的产出方式

漂移清单是**可重复运行的检查产物**，不是人工列表：`backend/tests/test_phase0_contracts.py` 把「文档 ↔ 代码」的一致性做成断言（表清单、迁移单一 head、manifest 输入、原因码来源），`docs/validation/0023` 记录逐项结果。任何一项不一致都会让检查失败，而不是等到评审时被人发现。

### 8.4 转换幂等

- 转换作业的幂等键是**旧对象的主键 + 映射规则版本**：同一旧记录在同一规则版本下重复转换**不产生第二条事实**。
- 现有 `append_event` 的 `source_event_id`（`runs/events.py:34-39`）就是这种幂等的一个已实现例子：重发即 no-op。转换写入 P2 事实时应复用同一模式，而不是新增去重表。
- 幂等**只**保证「不重复」；它不把 `unknown`/`incomplete` 变成判定，也不把缺失输入补成事实。

### 8.5 回退读策略

| 情形 | 读法 |
| --- | --- |
| 新记录缺失（该轴尚无载体） | 走 §8.2 的隔离转换读路径；响应**显式**给出缺口与原因码，不返回空对象冒充「无数据」 |
| 缺口显示 | 复用既有形制：`available: false` + `reason_code`（`RunRuntimeView`/`RetentionView`），或 `missing_reason`（`EvidenceView`） |
| 新旧都有 | 权威为新；旧值保留为历史原值，**不覆盖**，界面同时给出 current/frozen 版本（0006 §9） |
| 读数不一致 | 记入漂移清单；未解释前**不退出** `dual_read`，也不据此确认任何主张 |

## 9. 共享迁移顺序与契约所有者

### 9.1 现状

- 当前 head：`0008_retention_decisions`；`BUSINESS_REVISIONS` 与之一致（`storage/database.py:15`）。
- 本切片**不新增迁移**：第 5 条验收要求的是**登记顺序**，不是现在就把后续切片的表建出来。
- **迁移编号由协调人在合入时串行分配**（`docs/agents/p2-execution-batches.md` §4、§7）。

### 9.2 迁移编号分配建议

下表是**建议**，实际编号以合入顺序为准；同批内不得并行新增同一 `down_revision`。

| 建议编号 | 切片 | 预期建/改的内容（据 0006 与各 Issue 标题） |
| --- | --- | --- |
| `0009_*` | #43（P2-A1） | 事务外模型请求与独立任务/会话/决策身份：`research_tasks`/`agent_sessions`/`decisions` 的扩展列 |
| `0010_*` | #44（P2-B1） | 服务事实、入口身份、跨 Run 只读血缘：**新建** `Host`/`Service`/`WebEndpoint`（0006 §10 未列，本切片确认其不存在） |
| `0011_*` | #45（P2-E2） | 事件保留、过期游标与可靠快照补拉：`audit_events`/`event_cursors` 的保留期与过期语义 |
| `0012_*` 起 | #52 / #56 / #60 / #64 / #67 / #70（D 线） | `Finding`、`EvidenceBinding`、`Claim`/`ClaimEvaluation`、判据/要求集、`Review`/`HumanDecision`、`SeverityAssessment`/`AdmissionDecision`、证据有效性与保留保护、manifest/报告版本 |

**约束**（三条，后续切片都必须遵守）：

1. 每次只往前走一个 revision，`down_revision` 指向**合入时的当前 head**；
2. 新增迁移的 revision id **同时**加入 `storage/database.py` 的 `BUSINESS_REVISIONS`（同一提交）；
3. 不得删除或重写已有迁移（`login_buckets` 的建/删是两个各自成立的 revision，保留原样）。

### 9.3 合入时的单一 head 核对规程

```powershell
# 1) 链上每个文件只声明一个 revision / down_revision，且 parent 都存在
Select-String -Path backend/migrations/versions/*.py -Pattern '^revision|^down_revision'
# 2) 当前 head 与 BUSINESS_REVISIONS 一致
Select-String -Path backend/src/huntweave/storage/database.py -Pattern 'BUSINESS_REVISIONS'
# 3) 机械核对由检查承担（本切片新增，无需数据库）
cd backend; & <venv>\python.exe -m pytest tests/test_phase0_contracts.py -q -m "not integration"
# 4) 需要真实库时（合入演练），在一次性独立项目内执行 alembic upgrade head 后再查
#    SELECT version_num FROM huntweave.alembic_version;
```

第 3 步的 `test_the_migration_chain_is_linear_and_has_exactly_one_head` 会在出现两个 head、悬挂 `down_revision`、或 head 不在 `BUSINESS_REVISIONS` 里时失败。

### 9.4 契约所有者登记

| 面 | 独占所有者 | 其他切片 |
| --- | --- | --- |
| `contracts/phase0.py`（本切片新增） | **#38（本切片）** | 只消费；改字段须先改本文件与验证记录 |
| `contracts/runs.py`、`contracts/capabilities.py` | P2-B/D（字段/版本） | 只消费 |
| `contracts/orchestration.py`（视图） | P2-B（#43/#49/#50） | 只消费 |
| `contracts/execution.py`（动作/票据） | P2-E 执行线（#39/#59/#62/#63） | 只消费 |
| `contracts/retention.py`（保留视图） | P2-D/E（#67） | 只消费 |
| `contracts/errors.py`（原因码载体） | **共享面**：新增原因码由本批唯一所有者提交，其他分支只消费（`p2-execution-batches.md` §4） |
| Alembic 迁移链 | 各切片自己提交，**编号由协调人合入时串行分配** | — |
| `api/` 装配与路由 | 集成人协调（批内唯一改动者） | 只消费 |
| 六轴**字段**设计 | P2-B/D（0003 §8.1、0006 §11 末段） | — |
| 六轴**资源**（A6） | P2-E（#57/#73） | — |
| 六轴 **UI** | P2-F（#61/#65） | — |
| `docs/specs/0006`、`docs/specs/0003` | **#38（本切片，仅增量）** | 后续切片按需申请 |

## 10. 后续每切片的实施评审清单

本切片**不**替代后续切片的实施评审；**关闭 #38 不等于全部 P2 开工评审通过**（Issue #38 正文验收标准 5 与 0003 §8.1 均如此要求）。每个切片开工前按 PROJECT §14 逐项回答：

1. **接口/字段**：本切片新建或修改的字段，是否与 0006 §1 的六轴语义、§2 的引用方向（A4→A1 单向）一致？是否落进本文件 §9.4 登记的所有者？
2. **来源与旧值**：本切片消费的旧值，是否在本文件 §4.2 的规则表里有对应行？没有数据时**不得**生成迁移事实。
3. **迁移与回退**：新增迁移的 `down_revision` 是否为合入时的当前 head？`BUSINESS_REVISIONS` 是否同一提交更新？`downgrade` 是否真的可回退？
4. **失败/恢复**：本切片新增的失败路径，原因码是否来自 §11 的真实清单（不得新造）？`unknown`/停止未确认是否仍阻止释放物理额度？
5. **冻结与 `dual_read`**：本切片新增的读取，是否满足 §8 的退出条件？是否引入了第二写入口或 `dual_write`？
6. **验收夹具**：是否有可重复运行的检查（优先 `backend/tests/`，`-m "not integration"`）？夹具是否引用规则/用例版本？
7. **未决事项**：本切片未锁定的配置，是否记录在案并指明 owner，而不是留给下一个切片猜？

## 11. 原因码

**不发明新原因码。** 权威清单是代码本身：`contracts/errors.py:1` 的 `ServiceError` 加上执行端拒绝类型（`execution/ledger.py:74,82`、`execution/archive.py:133`、`execution/sandboxprofile.py:22`、`execution/sandbox.py:108` 的 `HaltReason`）。本切片只**登记来源**，不改动词表。逐条来源见验证记录 0023 §D；机械清单由 `backend/tests/test_phase0_contracts.py::test_error_samples_carry_the_single_error_envelope_and_no_prose` 与既有的 `tests/test_reason_codes.py` 共同把守。

**提案新增（须批准后才可用）**：本切片**没有**提出新原因码。若后续切片确需（例如「证据要求未明确」这类新缺口），必须显式标注为**提案**、说明谁批准（0006 §3.1 的契约版本变更须明确审核），并在同一提交里加入前端 `workspace.ts` 的 `messages` 映射与 `tests/test_reason_codes.py` 的相应条目。

## 12. 固定样例（第 3 条验收）

固定样例不得只存在于文档里：`backend/tests/data/phase0/*.json` 是**可加载**的响应样例，`backend/tests/test_phase0_contracts.py` 让它们真的通过生产契约。

| 样例 | 必失败状态 | 加载成 | 要点 |
| --- | --- | --- | --- |
| `not_ready.json` | 未就绪 | `Capabilities` | `real_execution_ready=false`，四个门槛逐一给出未满足原因 |
| `verdict_unknown.json` | `unknown` | `ToolCallView` | 结果未知是执行事实；`reconciliation=null`，`conditions=["outcome_unsettled"]` |
| `stop_unconfirmed.json` | 停止未确认 | `ToolCallView` | `observation.stop_confirmed=false`，`conditions=["stop_unconfirmed"]`，不得读作额度已归还 |
| `version_conflict.json` | 版本冲突 | 错误信封 | `409` + `{"reason_code":"version_conflict"}` |
| `evidence_missing.json` | 证据缺失 | `EvidenceView` | `available=false` + `missing_reason`，不返回空内容冒充无数据 |
| `permission_denied.json` | 权限拒绝 | 错误信封 | `401` + `{"reason_code":"authentication_required"}`；权限拒绝与策略拒绝不同码 |
| `invalid_call_state.json` | `invalid_call_state` | 错误信封 | `409`；与 `version_conflict` 区分（代次对但请求本身无意义） |
| `reconciliation_conflict.json` | `reconciliation_conflict` | 错误信封 | `409`；同一次调用只能有一条操作员裁定 |

字段设计归 B/D、资源归 E、UI 归 F：样例只固定**响应形状与原因码**，不预设 B/D/E/F 各自的内部字段。

## 13. 未决事项与限制

- **CIDR 边界未决**（ADR-0026 §3）：保留 IP-only 与 `TARGET_LIMIT = 100`，本切片不改。
- **30 个冻结输入字段无生产者**：owner 已登记（§7.4），实现随对应切片。
- **本切片未做迁移**：没有新建/改写 Alembic revision，没有改动任何现有列。
- **`dual_read` 目前无事可做**：没有任何已转换来源，因此兼容读路径只有定义与检查，没有运行中的双读。
- **未做功能验收**：不启动新栈、不接触外部目标；对现存数据的查询是**只读**的 `information_schema` / `COUNT(*)` / `SELECT`。
- **`docs/STATUS.md`、`PROJECT.md`、`docs/adr/**` 未改**（白名单外），阶段状态仍只在 STATUS 维护。

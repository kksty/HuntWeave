# P2 Phase 0 来源盘点、六轴接口与冻结输入实施评审

日期：2026-10-11（Asia/Shanghai）。对应 [#38](https://github.com/kksty/HuntWeave/issues/38)。本切片是**设计与迁移准备**：不执行生产迁移、不建生产表、不改现有业务行为。交付物是实施评审结论、一份来源盘点/契约交接文件（[0010](../specs/0010-phase0-source-inventory.md)）、**新增**的 `contracts/phase0.py` 契约目录与**新增**的可重复检查（`backend/tests/test_phase0_contracts.py` + 8 个可加载响应样例）。验收方式是「对照模型、迁移历史与固定数据样例审阅」加上机械检查；**不是**功能验收。

本记录是本分支上的第三份产出，前面两份是 0010（契约）与检查夹具（代码）。**本记录不改 0010 的结论；若两者冲突，以本记录的实际命令输出为准**，并按记录约定在「本记录的更正」一节留痕。

## 环境与入口

- 工作目录：`D:\code\huntweave-wt\38-phase0-contracts`（worktree，从已含 #37 的 `origin/main` `1e5b0eb` 切出）；分支 `codex/38-phase0-contracts`；分支起点即 `origin/main`，因此 `origin/main..HEAD` 的 diff 完全由本切片产生。主仓库 `D:\code\HuntWeave` 未改动，未切换分支，未做 `git worktree` 操作。
- 后端虚拟环境复用主仓库：`D:\code\HuntWeave\backend\.venv\Scripts\python.exe`（pytest 9.1.1）。所有 pytest/ruff/mypy 命令在 **worktree 的 `backend\` 内**运行（pytest 按 rootdir 解析 `pythonpath = ["src"]`）。
- **未启动任何 Docker 栈。** 本机常驻 `huntweave`（`huntweave-app-1`/`-runner-1`/`-postgres-1`，Compose 项目 `huntweave`）与另一会话的 `huntweave-i21-concurrency-postgres-1`（`127.0.0.1:18400`）。按本票约束，未对默认项目名执行 `up/down/stop/restart`，未清理其卷或网络。对现存数据的核对全部是**只读**的 `docker exec ... psql -tAc "SELECT ..."`（`information_schema`、`pg_constraint`、`pg_indexes`、`COUNT(*)`、`SELECT`），没有一条写语句。
- 未接触任何外部目标；`lab/` 靶场与探针均未运行。
- 盘点基线：Issue #38 正文声明 2026-10-11 / `main` `4f0d063`；本 worktree 实际起点为 `origin/main` 的 `1e5b0eb`（含 #37 与 #77 的后续提交）。
- 本分支提交（`origin/main..HEAD`，最终清单见文末「本记录的更正」若两轴评审后追加提交）：

| 提交 | 主题 |
| --- | --- |
| `contracts: 新增 Phase 0 六轴/冻结输入/旧值映射契约目录` | `backend/src/huntweave/contracts/phase0.py` |
| `tests: 新增 Phase 0 来源盘点夹具与固定响应样例` | `backend/tests/test_phase0_contracts.py`、`backend/tests/data/phase0/*.json` |
| `docs: 新增 Phase 0 来源盘点与契约交接，增量修订 0006/0003` | `docs/specs/0010-*.md`、`0006 §10`、`0003 §8.1` |
| `docs: 验证记录 0023 并登记 Phase 0 盘点结论` | 本记录 + `docs/validation/README.md` 索引行 |

## 实施评审（PROJECT §14）

PROJECT §14 要求每个 P2 切片开工前对照实际代码与前置能力完成实施评审，检查接口/字段、迁移与回退、失败/恢复、验收夹具与待锁定配置。本切片的评审对象是 **「0006 §10 / 0003 §8.1 的描述是否与真实代码、迁移历史与现存数据一致」**，结论表如下（逐项证据见后续小节）。

| # | 复核项 | 命令/来源 | 结论 |
| --- | --- | --- | --- |
| 1 | 全部 ORM 类与 `__tablename__` | `storage/models.py` 全文 | **19 个类 / 18 个 `__tablename__`**（第 19 个类是抽象 `Base`）；18 张有 ORM 类的表全部在 `huntweave` schema |
| 2 | 实际迁移链与单一 head | 8 个迁移文件的 `revision`/`down_revision` | 线性 8 revision、**单一 head `0008_retention_decisions`**，与 `BUSINESS_REVISIONS` 一致 |
| 3 | 迁移实际建出的表/列/索引 | 迁移文件 DDL + 真实库 `pg_attribute`/`pg_indexes`/`pg_constraint` | 19 张业务表 + `alembic_version`；与 ORM 声明逐列一致 |
| 4 | raw SQL 建、无 ORM 类的表 | grep `INSERT INTO`/`CREATE TABLE`/schema 名 | **仅 `huntweave.runtime_processes`**（`api/agentd.py:63` 写、`orchestration.py:1408` 与 `database.py:45` 读） |
| 5 | `contracts/*.py` 现有模型与版本常量 | grep 模块级大写常量 | `EXECUTION_POLICY_VERSION=1`（`runs.py:23`）、`BUSINESS_REVISIONS`、`SUPPORTED_PROFILE_VERSION=1`、`TARGET_LIMIT=100`、4 个保留默认值、`protocol_version="1"`、`config_version="p0-b-v1"` |
| 6 | 事件游标与 checkpoint schema owner | `runs/events.py`、`harness/checkpoints.py`、`storage/database.py` | 游标唯一写入口 `append_event`(`events.py:26`)；checkpoint 归 `huntweave_checkpoint`，由库自带迁移建表 |
| 7 | 0006 §10 首段对象清单 | 与 #1/#3 对比 | **不完整**：只列 8 个，真实另有 14 项（见下） |
| 8 | Claim/Attempt/Finding/Review/准入/manifest 是否存在 | `class X` 逐名 grep | **全部不存在**（匹配数 0） |
| 9 | #44 需要的 `Host`/`Service`/`WebEndpoint` | 同上 | **全部不存在**（`class Service` 唯一命中是 `ServiceError`） |
| 10 | 旧 Finding 取值与实际数据 | 真实库 `COUNT(*)`/`DISTINCT` | **没有任何 Finding 记录或旧取值**；`reconciliation_decisions` 0 行；`tool_calls.status` 仅 `succeeded`/`cancelled` |
| 11 | 六轴是否已有载体 | 逐轴对照真实表 | A1/A2/A4 **无载体**；A3 只有调用半边；A5/A6 部分载体 |
| 12 | 冻结输入是否可生产 | `MANIFEST_INPUTS` + 源码 token 机械核对 | 43 项中 `available` 8 / `partial` 16 / `absent` 19 |

**评审判定：本切片可开工并可合入；后续切片可在 0010 的契约上并行，但不得假设本文件之外的旧数据存在。** 三处必须带进实施的缺口：A4→A1 单向引用无载体（0010 §5）、19 个冻结输入无生产者（0010 §7.3）、`dual_read` 目前没有已转换来源（0010 §8）。

### 0006 §10 与真实代码不一致之处（逐处）

原文（`docs/specs/0006-state-model-and-delivery.md:165`）：「现有源码保存 Run、ResearchTask、AgentSession、Decision、ToolCall、ToolResult、Evidence 和 ReconciliationDecision；没有完整 Claim、Attempt、Finding、Review、准入或 frozen manifest 表。现存核对 `outcome=undetermined` 是『调用是否发生仍未决』，不是 A1 未决主张，禁止按同名迁移。」

| # | 原文的断言 | 实际 | 判定 |
| --- | --- | --- | --- |
| a | 「现有源码保存 [8 个对象]」 | 8 个全部存在，但**清单不完整**：真实 schema 另有 `projects`、`authorization_scopes`、`access_key_state`、`web_sessions`、`budget_reservations`、`execution_outbox`、`event_cursors`、`audit_events`、`interruption_records`、`retention_decisions`、`runtime_processes` 共 11 张业务表，加 `alembic_version` 与已删的 `login_buckets`，以及 `huntweave_checkpoint` 的 4 张 | **不准确（不完整）** |
| b | 「没有完整 Claim、Attempt、Finding、Review、准入或 frozen manifest 表」 | 成立：`class Claim`/`Attempt`/`Finding`/`Review`/`FrozenManifest` 匹配数均 0，`__tablename__` 也无对应项 | **准确** |
| c | 「现存核对 `outcome=undetermined`」 | 语义成立（`contracts/orchestration.py:12`），但**库里 0 行**，因此「现存」二字在当前数据上没有对应物 | **语义准确、数据上无实例**；已在本切片补记为「0 行」 |
| d | （未提及）`Host`/`Service`/`WebEndpoint` | 确实不存在，但 0006 §10 未点名；#44 需要它们 | **遗漏**；已在 0010 §4.1 补记 |
| e | （未提及）`runtime_processes` 是唯一 raw SQL 表、`login_buckets` 已删、checkpoint 在独立 schema | 三项均属实且对盘点有影响 | **遗漏**；已在 0010 §3.3 补记 |

本切片对 0006 §10 的处理是**增量补记**（在 §10 明确指出首段清单只是设计期概述、完整清单以 0010 §3 为准，并注明 Findings 记录为 0 行），**未删除或改写任何既有口径**。

### 修正后的准确清单

`0006 §10` 原文「现有源码保存 Run、ResearchTask、AgentSession、Decision、ToolCall、ToolResult、Evidence 和 ReconciliationDecision」在 **19 张业务表**中只列到 **8** 个，**漏 14 项**，按三类计：

| 类别 | 项目 | 数量 |
| --- | --- | --- |
| 未列出的业务表 | `projects`、`authorization_scopes`、`access_key_state`、`web_sessions`、`budget_reservations`、`execution_outbox`、`event_cursors`、`audit_events`、`interruption_records`、`retention_decisions`、`runtime_processes` | 11 |
| 非业务/历史对象 | `alembic_version`、已删的 `login_buckets` | 2 |
| 独立 schema 的库表 | `huntweave_checkpoint` 的 `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_migrations` | 1（按「类」计） |

另有两处遗漏与一处需限定：`Host`/`Service`/`WebEndpoint` **确实不存在**（#44 需要，0006 §10 未点名）；「现存核对 `outcome=undetermined`」的语义正确，但真实库中该表 **0 行**，「现存」二字在当前数据上没有实例。

## 来源盘点：实际命令与输出

### A) 迁移链与单一 head

```powershell
cd D:\code\huntweave-wt\38-phase0-contracts\backend
Select-String -Path migrations\versions\*.py -Pattern '^revision|^down_revision'
```

实际输出（原文）：

```
0001_runtime.py:6:revision = "0001_runtime"
0001_runtime.py:7:down_revision = None
0002_identity_runs.py:7:revision = "0002_identity_runs"
0002_identity_runs.py:8:down_revision = "0001_runtime"
0003_orchestration.py:7:revision = "0003_orchestration"
0003_orchestration.py:8:down_revision = "0002_identity_runs"
0004_reconciliation.py:7:revision = "0004_reconciliation"
0004_reconciliation.py:8:down_revision = "0003_orchestration"
0005_run_execution_profile.py:13:revision = "0005_run_execution_profile"
0005_run_execution_profile.py:14:down_revision = "0004_reconciliation"
0006_call_runtime.py:19:revision = "0006_call_runtime"
0006_call_runtime.py:20:down_revision = "0005_run_execution_profile"
0007_drop_login_throttle.py:21:revision = "0007_drop_login_throttle"
0007_drop_login_throttle.py:22:down_revision = "0006_call_runtime"
0008_retention_decisions.py:20:revision = "0008_retention_decisions"
0008_retention_decisions.py:21:down_revision = "0007_drop_login_throttle"
```

head 判定：revision 集合减去被引用为 `down_revision` 的集合 = `{0008_retention_decisions}`（唯一）；根 = `{0001_runtime}`（唯一）。`storage/database.py:15` 的 `BUSINESS_REVISIONS = ("0008_retention_decisions",)` 与 head 一致。

### B) 真实库的表、列、索引、约束（只读）

```powershell
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc `
  "SELECT table_schema||'.'||table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc "SELECT version_num FROM huntweave.alembic_version"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc `
  "SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='huntweave' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc `
  "SELECT indexname||' :: '||indexdef FROM pg_indexes WHERE schemaname='huntweave' ORDER BY 1"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc `
  "SELECT conname||' :: '||pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace='huntweave'::regnamespace ORDER BY 1"
```

实际输出（结构摘录，完整清单见 0010 §3.3）：

```
huntweave.access_key_state        huntweave.agent_sessions        huntweave.alembic_version
huntweave.audit_events            huntweave.authorization_scopes  huntweave.budget_reservations
huntweave.decisions               huntweave.event_cursors         huntweave.evidence
huntweave.execution_outbox        huntweave.interruption_records  huntweave.projects
huntweave.reconciliation_decisions huntweave.research_tasks       huntweave.retention_decisions
huntweave.runs                    huntweave.runtime_processes     huntweave.tool_calls
huntweave.tool_results            huntweave.web_sessions
huntweave_checkpoint.checkpoint_blobs      huntweave_checkpoint.checkpoint_migrations
huntweave_checkpoint.checkpoint_writes     huntweave_checkpoint.checkpoints
--- alembic_version ---
0008_retention_decisions
```

`huntweave` schema 表数 = **20**（19 张业务表 + `alembic_version`）。逐列核对结论：**真实库列与 ORM 声明逐列一致**，未发现 ORM 有而库没有、或库有而 ORM 未声明的列。索引 33 个（含 pkey/unique），`ck_runs_status` 与 `ck_runs_versions` 存在，外键 32 条全部存在（含 `tool_calls.replaces_call_id` 的自引用 `fk_tool_calls_replaces_call_id`）。

### C) 现存数据（只读，用于「没有旧数据」的判定）

```powershell
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc `
  "SELECT 'runs',count(*) FROM huntweave.runs UNION ALL SELECT 'tool_calls',count(*) FROM huntweave.tool_calls UNION ALL SELECT 'reconciliation_decisions',count(*) FROM huntweave.reconciliation_decisions UNION ALL SELECT 'evidence',count(*) FROM huntweave.evidence UNION ALL SELECT 'research_tasks',count(*) FROM huntweave.research_tasks UNION ALL SELECT 'audit_events',count(*) FROM huntweave.audit_events UNION ALL SELECT 'projects',count(*) FROM huntweave.projects UNION ALL SELECT 'retention_decisions',count(*) FROM huntweave.retention_decisions"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc "SELECT status,count(*) FROM huntweave.tool_calls GROUP BY 1 ORDER BY 1"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc "SELECT status,count(*) FROM huntweave.runs GROUP BY 1 ORDER BY 1"
docker exec huntweave-postgres-1 psql -U postgres -d huntweave -tAc "SELECT reason_code,count(*) FROM huntweave.runs GROUP BY 1 ORDER BY 1"
```

实际输出（原文）：

```
runs|46
tool_calls|52
reconciliation_decisions|0
evidence|52
research_tasks|52
audit_events|667
projects|41
retention_decisions|0
--- tool_calls 状态 ---
cancelled|7
succeeded|45
--- runs 状态 ---
cancelled|9
closed|5
draft|20
paused|2
waiting|10
--- runs reason_code ---
|46
```

**判定：没有旧 Finding 数据，也没有 `unknown`/`incomplete` 调用。** 因此 0006 §0.1 的每一条转换规则在当前数据上都**不执行**，本切片不产生任何迁移事实。`runs.reason_code` 46 行全为 `NULL`，说明「带原因的受限结束」在当前库中同样没有实例。

## 契约目录：`contracts/phase0.py`（仅新增）

新增文件 `backend/src/huntweave/contracts/phase0.py`（722 行），**未修改 `contracts/` 下任何既有文件的任何字段含义**。它导出四组数据，供后续切片消费并供检查机械核对：

| 导出 | 内容 |
| --- | --- |
| `AXES` / `AxisSpec` | 六轴的取值集合、已存在载体、尚不存在载体、引用方向、写入方、必须分开的事实 |
| `MANIFEST_INPUTS` / `ManifestField` | 8 组 **43 个**冻结输入，每个带 `type`、`availability ∈ {available,partial,absent}`、`source`、`search` |
| `NOT_IN_MANIFEST` | 7 类**不进入**冻结 manifest 的内容（队列策略、积压实时值、保留阈值、就绪门槛、额度设置、路径、凭据） |
| `LEGACY_VALUE_MAPPINGS` / `LEGACY_OBJECTS_PRESENT` / `LEGACY_OBJECTS_ABSENT` | 11 条旧值映射规则（全部 `executable=False`）、8 个已存在对象、14 个不存在对象 |
| 常量 | `PHASE0_INVENTORY_SCHEMA_VERSION=1`、`FROZEN_MANIFEST_SCHEMA_VERSION=1`、`CLAIM_VERDICTS`、`REVIEW_PROCESSING_STATES`、`REVIEW_RESULTS`、`ATTEMPT_STATES`、`ADMISSION_STATES`、`IP_RESOURCE_STATES`、`CONFIRMATION_LEVELS`、`CLAIM_CLASSES` |
| 函数 | `manifest_field_names()`、`manifest_gaps()`、`axis(name)`、`legacy_mapping_can_produce_claim_verdict(old_value)` |

**破坏性改动：无。** `contracts/phase0.py` 是新增文件；`contracts/` 下既有 6 个文件在本分支**零改动**（见「共享面影响」）。

## 固定样例的实际加载验证

样例文件：`backend/tests/data/phase0/*.json`（8 个）。每个样例自带 `kind`（`response`/`error`）、`materializes`（加载成哪个生产契约 + HTTP 状态）、`reason_code`、`state.must_fail`、`describes`、`source`。

命令：

```powershell
cd D:\code\huntweave-wt\38-phase0-contracts\backend
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m pytest tests/test_phase0_contracts.py -q -m "not integration"
```

实际输出：

```
..............................                                           [100%]
30 passed in 0.21s
```

逐个样例的加载方式与断言（都由上述检查实际执行）：

| 样例 | 加载成 | 断言 |
| --- | --- | --- |
| `not_ready.json` | `Capabilities.model_validate` | `real_execution_ready is False`；`mode == "demonstration"`；`gates` 的 gate 名集合恰为 `ReadinessGateName` 的 4 项；每个未就绪门都有 `reason_code` |
| `verdict_unknown.json` | `ToolCallView.model_validate` | `status == "unknown"`；`reconciliation is None`；`conditions == ["outcome_unsettled"]`；`observation.stop_confirmed is False`；`legacy_mapping_can_produce_claim_verdict("unknown") is False` |
| `stop_unconfirmed.json` | `ToolCallView.model_validate` | `stop_confirmed is False`；`"stop_unconfirmed" in conditions`；`status` 不属于 `{succeeded,failed,cancelled}` |
| `version_conflict.json` | `ErrorEnvelope.model_validate` | `payload` 键集合恰为 `{"reason_code"}`；`reason_code == "version_conflict"` |
| `evidence_missing.json` | `EvidenceView.model_validate` | `available is False`；`missing_reason` 非空；`demonstration is False` |
| `permission_denied.json` | `ErrorEnvelope.model_validate` | `reason_code == "authentication_required"` |
| `invalid_call_state.json` | `ErrorEnvelope.model_validate` | `reason_code == "invalid_call_state"`（与 `version_conflict` 区分） |
| `reconciliation_conflict.json` | `ErrorEnvelope.model_validate` | `reason_code == "reconciliation_conflict"` |

另有两条反向断言：每个 `kind == "error"` 的样例，其 `reason_code` **必须**在源码扫描到的原因码集合里（不允许发明新码）；每个 `kind == "response"` 的样例必须 `reason_code is None`。`ErrorEnvelope` 用 `extra="forbid"`，并有专门一条检查证明多带一个字段会 `ValidationError`。

## 完整原因码清单（来源文件 + 行号）

原因码的权威定义只有一处：`contracts/errors.py:1` 的 `ServiceError(reason_code, status_code, retry_after)`，由 `api/app.py:84-90` 渲染为 `{"reason_code": ...}`。执行端另有四个自己的拒绝类型：`execution/ledger.py:74`（`RunnerRejected`）、`:82`（`RunnerUnavailable`）、`execution/archive.py:133`（`ArchiveRejected`）、`execution/sandboxprofile.py:22`（`SandboxRejected`），以及 `execution/sandbox.py:108` 的 `HaltReason`。
扫描命令（正则锚定在这五个类型与 `ServiceError` 的调用上，避免把普通小写标识当原因码）：

```powershell
# 完整脚本见本记录「命令与输出」的可复现形式：对 backend/src/**/*.py 逐行匹配
#   (?:ServiceError|RunnerRejected|RunnerUnavailable|SandboxRejected|ArchiveRejected)\(\s*"([a-z][a-z0-9_]*)"'
# 并另行收集 "reason_code": "..." / reason_code = "..." / reason = "..." 的赋值点
```

**结果：176 处站点、去重后 96 个不同原因码**（下表按文件给出计数与全部站点 `码@行`）：

| 源文件 | 站点数 | 原因码（`码@行`） |
| --- | --- | --- |
| `access/service.py` | 6 | `access_key_missing@65`、`@107`；`authentication_required@104`、`@136`；`csrf_invalid@128`；`invalid_access_key@67` |
| `api/app.py` | 8 | `access_key_missing@331`、`frontend_unavailable@694`、`invalid_event_cursor@527`、`invalid_request@241`、`origin_invalid@343`、`runner_unavailable@252`、`storage_unavailable@319`、`@356` |
| `execution/archive.py` | 5 | `evidence_archive_unwritable@235`、`evidence_artifact_too_large@162`、`evidence_path_escapes_root@241`、`evidence_storage_full@234`、`@251` |
| `execution/ledger.py` | 14 | `call_id_conflict@352`、`call_not_found@440`、`@470`、`@481`、`control_lease_expired@448`、`control_lease_too_long@360`、`execution_ticket_expired@358`、`invalid_control_lease@455`、`parameters_hash_mismatch@356`、`runner_state_already_owned@147`、`scope_policy_mismatch@404`、`stale_lease_generation@392`、`@442`、`@472` |
| `execution/real.py` | 3 | `action_not_real@110`、`@157`、`sandbox_runtime_unreachable@161` |
| `execution/retention.py` | 4 | `retention_artifact_unknown@355`、`@582`、`retention_version_unknown@362`、`@580` |
| `execution/sandbox.py` | 46 | `action_command_invalid@880`、`control_lease_too_long@1284`、`endpoint_not_expressible@597`、`evidence_name_rejected@928`、`evidence_storage_failed@937`、`evidence_too_large@930`、`invalid_control_lease@1282`、`ownership_mismatch@1572`、`retention_artifact_in_use@1559`、`retention_artifact_not_retained@1561`、`sandbox_authorization_mismatch@960`、`sandbox_egress_unverified@1277`、`@1279`、`@1704`、`sandbox_gateway_not_ready@1636`、`@1640`、`sandbox_gateway_policy_failed@1699`、`sandbox_image_unavailable@1023`、`sandbox_instance_active@999`、`sandbox_instance_creation_failed@1142`、`sandbox_instance_interrupted@997`、`sandbox_instance_mismatch@923`、`sandbox_instance_not_running@877`、`@1212`、`@1232`、`@1268`、`sandbox_instance_reclaimed@1155`、`sandbox_instance_unknown@1844`、`sandbox_platform_networks_unknown@1655`、`sandbox_profile_not_applied@1775`、`sandbox_resource_missing@859`、`@889`、`@902`、`@1634`、`@1697`、`@1870`、`sandbox_resources_unaccounted@1017`、`sandbox_reverting@993`、`sandbox_runtime_unreachable@1025`、`@1272`、`sandbox_session_unknown@1850`、`sandbox_stop_unconfirmed@1799`、`@1803`、`scope_denied@1004`、`@1111`、`@1220` |
| `execution/sandboxprofile.py` | 29 | `sandbox_profile_image_not_pinned@388`、`@391`、`@393`、`sandbox_profile_invalid@162`、`@169`、`@171`、`@188`、`@226`、`@315`、`@320`、`@327`、`@333`、`@339`、`@345`、`@347`、`@360`、`@370`、`@378`、`@413`、`@420`、`@424`、`@453`、`@455`、`@458`、`sandbox_profile_not_isolated@209`、`@211`、`sandbox_profile_privileged_tool@411`、`sandbox_profile_unknown@167`、`sandbox_profile_unsupported_version@191` |
| `execution/server.py` | 4 | `call_not_found@488`、`execution_profile_unknown@190`、`real_execution_disabled@181`、`sandbox_management_disabled@336` |
| `runs/inputs.py` | 3 | `invalid_ports@88`、`@92`、`unexpected_custom_ports@79` |
| `runs/orchestration.py` | 36 | `action_not_in_profile@546`、`call_not_found@881`、`event_cursor_ahead@1471`、`evidence_not_found@1500`、`execution_reconciliation_required@1224`、`execution_record_mismatch@642`、`execution_stop_unconfirmed@973`、`@1188`、`@1228`、`invalid_call_state@893`、`invalid_control@1252`、`invalid_event_cursor@1464`、`invalid_evidence_range@1496`、`invalid_run_state@1182`、`@1184`、`@1208`、`@1215`、`@1222`、`lease_stale@297`、`reconciliation_conflict@888`、`reconciliation_evidence_contradicted@969`、`@982`、`reconciliation_evidence_missing@957`、`@967`、`@980`、`reconciliation_lease_active@971`、`run_not_found@119`、`@127`、`@1467`、`scope_denied@87`、`@90`、`@94`、`@96`、`task_not_found@291`、`version_conflict@891`、`@1179` |
| `runs/service.py` | 18 | `authorization_expired@88`、`@190`、`authorization_not_started@192`、`authorization_required@71`、`execution_profile_mismatch@132`、`idempotency_conflict@121`、`invalid_authorization_window@86`、`invalid_idempotency_key@110`、`invalid_run_state@176`、`project_name_required@43`、`project_not_found@84`、`real_execution_not_ready@38`、`run_not_found@158`、`@172`、`scope_not_found@103`、`@125`、`version_conflict@127`、`@174` |

另有 **37 个**原因码在源码中以「赋给 `reason_code` 字段/`reason` 局部量」的形式出现，其中 **30 个不在上表的 96 个里**（覆盖 `PROJECT §12.2` 的执行期原因与输入行级原因）。二者合计 **126 个不同原因码**（所有赋值的去重总数是 125，因为 `evidence_missing` 与 `result_incomplete` 同时出现在两类里，取并集后为 125；把「raise 里 96 个」与「赋值里独有的 30 个」相加即 126，差异只来自这两个两种形式都用的码）。它们是同一词汇表的一部分，**同样不是本切片发明的**：

| 原因码 | 来源 |
| --- | --- |
| `action_failed`、`execution_timeout`、`operator_cancelled` | `execution/real.py:262`、`:260`、`:330`/`:489` |
| `budget_exhausted`、`execution_unreachable`、`evidence_incomplete`、`result_incomplete` | `runs/orchestration.py:439`、`:857`、`:794`、`:1152`/`:1194` |
| `evidence_hash_mismatch`、`evidence_missing`、`evidence_output_truncated`、`operator_stopped` | `execution/ledger.py:419`、`:421`、`:709`/`:719`、`:532`/`:537` |
| `egress_unverified`、`revert`、`sandbox_revert_blocked` | `execution/sandbox.py:1351`、`:1402`、`:1446` |
| `environment_unsupported`、`runner_state_unavailable` | `execution/capabilities.py:99`/`:97`、`api/supervisor.py:16`、`execution/healthcheck.py:9` |
| `invalid_ip`、`ipv6_environment_unsupported`、`protected_address`、`target_limit_exceeded` | `runs/inputs.py:52`、`:50`、`:44`、`:56` |
| `migration_failed`、`readiness_failed`、`required_process_exited`、`scheduler_unavailable`、`startup_failed` | `storage/migrate.py:32`、`api/healthcheck.py:23`、`api/supervisor.py:61`、`api/agentd.py:115`、`api/supervisor.py:65` |
| `request_too_large`、`runner_authentication_required`、`retention_unsupported`、`execution_not_implemented` | `access/body_limit.py:24`、`execution/server.py:276`、`api/app.py:136`、`execution/server.py:492` |

### 本切片提案新增的原因码

**无。** 本切片没有提出任何新原因码，因此不需要批准人。若后续切片需要新码（例如 0006 §3.1 的「验证要求未明确」缺口），必须①显式标注为**提案**；②说明批准人（契约版本变更须明确审核）；③同一提交内加入 `frontend/src/workspace.ts` 的 `messages` 映射与 `tests/test_reason_codes.py` 的 `NOT_REASON_CODES`/`UNDISPLAYED` 处置。

## 机械一致性检查

本切片把「文档 ↔ 代码」一致性做成了可重复运行的检查，而不是一次性核对。

```powershell
cd D:\code\huntweave-wt\38-phase0-contracts\backend
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m pytest tests/test_phase0_contracts.py -q -m "not integration"
```

检查项与它们各自把守的结论（30 项，全通过）：

| 检查 | 把守的结论 |
| --- | --- |
| `test_every_table_has_either_an_orm_class_or_a_recorded_raw_sql_owner` | 迁移建出的每张表要么有 ORM 类，要么被登记为 raw-SQL-only（现为 `runtime_processes`）；ORM 不会声明迁移不建的表 |
| `test_the_migration_chain_is_linear_and_has_exactly_one_head` | 单一根、单一 head、无悬挂 `down_revision`、head ∈ `BUSINESS_REVISIONS` |
| `test_the_inventory_document_lists_the_tables_the_code_really_has` | **0010 §3.3 的表清单与真实 schema/ORM 完全一致**；已删的 `login_buckets` 保留在清单里；checkpoint 的 4 张表被提及 |
| `test_the_inventory_document_records_no_object_the_code_does_not_have` | 0010 逐名记录了 8 个已存在对象与 14 个不存在对象 |
| `test_the_inventory_document_lists_every_frozen_manifest_input` | 0010 用 `\`字段名\`` 覆盖全部 43 个冻结输入 |
| `test_a_field_claimed_available_or_partial_has_a_carrier_in_the_real_source` | 8 个 `available` + 16 个 `partial` 的载体 token **真的存在于 `backend/src`** |
| `test_a_field_claimed_absent_is_genuinely_nowhere_in_the_real_source` | 19 个 `absent` 的字段名**真的不在** `backend/src` 中（P2 实现后此检查会失败，迫使目录与实现同提交更新） |
| `test_every_sample_loads_into_the_contract_it_names` | 8 个样例都通过其声明的生产契约，且名字/来源/描述自洽 |
| `test_error_samples_carry_the_single_error_envelope_and_no_prose` | 错误样例的键集合恰为 `{"reason_code"}`，且该码在源码中真实存在 |
| `test_the_required_must_fail_states_each_have_a_loadable_sample` | 8 个必失败状态与样例一一对应，无重复无遗漏 |
| `test_a_not_ready_response_states_every_gate_of_the_execution_boundary` | 未就绪响应逐一给出 ADR-0010 的四门 |
| `test_an_unknown_call_is_not_read_as_a_claim_verdict` | `unknown` 不是 A1 verdict |
| `test_a_stop_the_execution_side_has_not_confirmed_holds_capacity` | 停止未确认不读作额度已归还 |
| `test_the_legacy_mappings_generate_nothing_because_no_record_carries_the_old_values` | 11 条映射全部 `executable=False`，且每条都有必要输入与禁止事实 |
| `test_execution_facts_never_become_a_claim_verdict`（4 参数） | `unknown`/`incomplete`/`undetermined`/`any lifecycle status` 一律不产生 verdict |
| `test_claim_shaped_old_values_are_mapped_to_a_verdict_but_never_executed`（4 参数） | `supported`/`verified`/`refuted`/`suspected` 映射到 A1 但都不执行 |
| `test_an_unknown_old_value_is_refused_rather_than_guessed_at` | 未知旧值 `KeyError`，不猜 |
| `test_the_six_axes_keep_a4_pointing_at_a1_and_never_the_reverse` | **A4→A1 单向、A1 不引用任何轴**；六轴无自有表名 |
| `test_the_axes_that_have_no_carrier_say_so_instead_of_borrowing_one` | A1/A2/A4 的 `existing_carriers` 为空 |
| `test_the_frozen_manifest_inputs_are_a_closed_list_with_honest_availability` | 43 个字段名唯一、状态取值合法、缺口非空、5 个关键字段仍为 `available` |
| `test_the_queue_policy_stays_out_of_the_frozen_manifest` | 队列策略配置不进入 manifest |
| `test_the_confirmation_levels_and_claim_classes_are_the_locked_vocabulary` | 三个词汇表取值被固定 |
| `test_a_sample_cannot_load_into_a_laxer_contract_than_the_production_one` | `extra="forbid"` 真的生效 |
| `test_the_reason_code_scan_finds_the_codes_the_samples_depend_on` | 扫描本身有效（6 个代表码都能找到并有来源） |
| 其余 6 项 | 样例的必失败状态覆盖、错误/响应分类、来源非空等结构断言 |

## 逐条验收结论

| # | 验收标准（Issue #38 正文） | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 按 0.1→0.5 顺序记录来源盘点、旧值/verified 对象映射、六轴字段和引用方向、FrozenManifest 输入 schema、确认等级与要求集版本 | **通过** | 0010 按 0.1→0.5 组织：§3 来源盘点（0.1）、§4 旧值/verified 映射（0.1/0.2）、§5 六轴+引用方向（0.3）、§7 冻结输入 schema（0.4）、§6 确认等级与要求集版本（0.5）。机器可读版本在 `contracts/phase0.py`；检查 30 项通过 |
| 2 | 区分已存在的 `Run`/`ResearchTask`/`ToolCall`/`ReconciliationDecision` 与尚不存在的 `Claim`/`Finding`/`Review`；没有旧数据就不生成迁移事实，`unknown`/`incomplete` 不转为主张判定 | **通过** | 0010 §4.1 两张清单；`LEGACY_OBJECTS_PRESENT`(8)/`LEGACY_OBJECTS_ABSENT`(14)；真实库计数（§C）显示 `reconciliation_decisions` 0 行、无 Finding 对象与旧取值；11 条映射全部 `executable=False`；`unknown`/`incomplete` 的 verdict 判定为 `False`（检查 4+4 项） |
| 3 | 形成公开接口、原因码、版本冲突、权限和失败响应的固定样例；字段设计归 B/D、资源归 E、UI 归 F，不为每轴强建一表 | **通过** | 8 个样例全部通过生产契约（§「固定样例的实际加载验证」）；原因码全部取自源码（**176 处 raise 站点 / 96 个不同码 + 30 个仅以赋值形式出现的码**，逐个带文件:行号）；归属登记在 0010 §9.4 与 §12 末段；六轴无自有表（检查 `test_the_six_axes_keep_a4_pointing_at_a1_and_never_the_reverse` 断言载体名不以轴号开头） |
| 4 | 定义单一写入口、有界 `dual_read`、漂移清单、转换幂等与回退读策略；不默认引入 `dual_write` | **通过** | 0010 §8：单一写入口（`runs` 业务 + `events.py:26` 游标）；`dual_read` 的四条退出条件；漂移清单＝可重复运行的检查产物；转换幂等（旧主键+规则版本，复用 `source_event_id` 模式）；回退读复用 `available=false`+`reason_code`/`missing_reason` 形制；**未引入 `dual_write`**，代码中 `dual_write` 匹配数 0（`contracts/phase0.py` 只在文档字符串里提到该词） |
| 5 | 登记共享迁移顺序、契约所有者和后续每切片的实施评审清单；不得把本票关闭当作全部 P2 开工评审通过 | **通过** | 0010 §9.1–§9.3 迁移顺序与编号建议、§9.4 契约所有者、§10 七项实施评审清单；0010 §1 与 §10 首段、`0003 §8.1` 的增量修订均明写「关闭 #38 不表示全部 P2 开工评审通过」 |

**总结论：5 条全部通过。** 通过的含义限定为「契约与盘点已可据以开发、且与既存模型/迁移/数据一致」，**不表示任何 P2 能力已实现或已验收**。

## 三项本地检查

命令（在 worktree 的 `backend\` 下运行）：

```powershell
cd D:\code\huntweave-wt\38-phase0-contracts\backend
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m pytest -m "not integration" -q
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m ruff check src tests
& D:\code\HuntWeave\backend\.venv\Scripts\python.exe -m mypy --config-file pyproject.toml src
```

实际输出：

| 检查 | 结果 |
| --- | --- |
| pytest | `283 passed, 8 skipped, 41 deselected in 32.22s`（`-m "not integration"` 收集 291 项，41 项集成用例被 deselect，8 项因前端产物/浏览器依赖跳过） |
| ruff | `All checks passed!` |
| mypy | `Success: no issues found in 51 source files` |

修改前基线：本切片**只新增、不改既有行为**（15 个文件：12 个新增 + 3 个纯增量修改的文档），没有删除任何既有用例。修改前的 `origin/main` `1e5b0eb` 上，非集成检查为 **253 通过 + 8 跳过**（实测：`git stash push -u` 后实跑，再 `git stash pop` 还原）；本分支新增 30 项 Phase 0 检查，得到 283 通过 + 8 跳过，**253 + 30 = 283**，跳过数与收集范围均未变。**运行集合只增不删**：没有删除、替换或跳过任何既有用例。

## 两轴评审与本记录的更正

本切片按仓库约定做了 **Spec + Standards** 两轴评审。评审在本分支自验之后执行，**核对了真实文件而不是复述正文**，并做了以下两类验证：

1. **逐项事实核对**：真实库的 20 张表名、`pg_attribute` 逐列、33 个索引、32 条外键、4 条 CHECK/唯一约束；`alembic_version`；`class X` 逐名匹配数；11 个 ORM 之外的对象；43 个冻结输入字段名；原因码扫描；8 个样例的加载；行数（`runs` 46 / `tool_calls` 52 / `reconciliation_decisions` 0 / `retention_decisions` 0 / `audit_events` 667 / `projects` 41）。
2. **变异验证（mutation testing）**：把 5 个错误分别注入代码，确认新检查**真的会失败**，不是恒真断言。

| 变异 | 注入 | 结果 |
| --- | --- | --- |
| M1 | `models.py` 把 `__tablename__ = "runs"` 改成 `runs_mutated`（ORM 声明了迁移不建的表） | **2 failed**, 28 passed |
| M2 | `contracts/phase0.py` 把 `manifest_id` 改成 `brand_new_field`（0010 未记录该输入） | **1 failed**, 29 passed |
| M3 | 把 `fragment_versions` 从 `absent` 改成 `available`（无载体却声明可生产） | **1 failed**, 29 passed |
| M4 | `version_conflict.json` 的码改成 `totally_invented_code`（发明新码） | **3 failed**, 27 passed |
| M5 | `0008` 的 `down_revision` 改成 `0006_call_runtime`（链出现两个 head） | **1 failed**, 29 passed |
| M0 | 全部还原 | **30 passed** |

变异注入仅作用于本 worktree 的文件，注入后立即还原，`git status --short` 与注入前一致（无残留）。

### 发现的缺陷与处置

| 轴 | 缺陷 | 严重度 | 处置 |
| --- | --- | --- | --- |
| Standards | 本记录首稿把原因码统计写成「96 处站点、96 个不同码」，把站点数与去重数混为一谈；实际是 **176 处站点 / 96 个不同码**，另有 37 个赋值形式（30 个为独有），合计 126 个不同码 | 应修 | 已按实跑重写该段，并写明 96+30=126 与并集 125 的差异来源 |
| Standards | 本记录首稿称「修改前基线为 253 项级（历史记录为 254）」，**未实测**该基线 | 应修 | 已用 `git stash push -u` 在 `origin/main` 起点实跑得 `253 passed, 8 skipped, 41 deselected`，再 `git stash pop` 还原，并写明 253+30=283 |
| Standards | `docs/validation/README.md` 的 0023 索引行首稿写成裸文本 `[0023 ...]`（无 `(...)` 链接），与同表其他行不一致 | 应修 | 已补 `./0023-phase0-source-inventory.md` |
| Standards | 本记录首稿把共享面影响写成「15 个文件里 13 个是新增」——实际是 **15 个文件：12 个新增 + 3 个修改**（`0003`、`0006`、`validation/README.md`） | 应修 | 已按 `git diff --cached --stat` 的实数改写 |
| Standards | `docs/specs/0010` 的 §7.2 逐字段表首稿有几行「有/部分」状态字段只写了 owner，**没写真实载体**（例如 `evidence_ids` 未写 `evidence.id`）；字段清单本身没有归属，读者无法据此实现 | 应修 | 已把「载体 / owner」列补全为 43 行都有确切载体或 owner |
| 两轴 | **未发现**越界问题：未放宽真实执行门槛、未接受 CIDR、未引入 `dual_write`、未为六轴各建表、未删除或改写既有 0006/0003 口径、未改任何现有契约字段语义、未新增或改写 Alembic 迁移、未启动 Docker 栈、未写业务数据 | — | 记录为未发现问题的范围 |

### 复核过的机械一致性

本记录的每个新/改文件都做了以下检查，全部通过：

- **链接与锚点**：`0010`、`0023`、`validation/README.md`、`0006`、`0003` 五份文件中的**全部** markdown 链接与 `#anchor` 逐个解析，坏链数 **0**（含中文标题锚点）。
- **编码与空白**：5 个新/改文档与 2 个新代码文件**均无 BOM、无行尾空白**；`git diff --cached --check` 退出码 0、无警告。
- **行尾**：`.gitattributes` 为 `* text=auto` 且 `*.py text eol=lf`，索引内全部为 LF（`git ls-files --eol` 显示 `i/lf`），与既存文件一致；工作区的 CRLF 是 Windows `core.autocrlf=true` 的既有行为，不是本切片引入。
- **无被忽略产物入 Git**：本分支没有 `runtime/`、`.venv/`、`node_modules/`、`__pycache__/` 或任何二进制。

## 共享面影响

| 面 | 是否改动 | 说明 |
| --- | --- | --- |
| `contracts/` 新增模型 | **是（新增 1 个文件）** | `backend/src/huntweave/contracts/phase0.py`。**仅新增**：既有 6 个契约文件（`runs.py`/`execution.py`/`orchestration.py`/`retention.py`/`capabilities.py`/`errors.py`）零改动，无既有字段语义变化，**无破坏性改动** |
| Alembic 迁移 | **否** | 未新增、未改写任何 revision；`migrations/versions/` 零改动。第 5 条要求的是**登记顺序**（0010 §9.2），实现时由协调人串行分配编号 |
| `api/` 装配与路由 | **否** | `backend/src/huntweave/api/**` 零改动 |
| 前端 | **否** | `frontend/**` 零改动 |
| `deploy/` | **否** | 未新增夹具到 `deploy/`；检查全部是纯源码级 pytest，不需要起栈 |
| `PROJECT.md`、`docs/adr/**`、`docs/STATUS.md` | **否** | 白名单外，未改 |
| `docs/specs/0006`、`docs/specs/0003` | **是（仅增量）** | 0006 §10 加一段「0010 是 0.1–0.5 的实际盘点，首段清单只是设计期概述」+ §0.1 表前加「库里 0 条 Finding 记录、当前不产生迁移事实」；0003 §8.1 加一句指向 0010 并重申不替代各切片评审。**未删除任何既有口径** |
| `docs/validation/` | **是** | 本记录 + `README.md` 一行索引；未改他人记录 |
| 数据库 | **否** | 只有只读查询；未执行迁移、未插入/更新/删除任何行 |

## 未达成与限制

- **未做功能验收**：不启动新栈、不跑集成用例、不做浏览器验收。所有「通过」只覆盖契约、盘点与源码级检查。
- **未在真实库上演练迁移**：`alembic upgrade head` 未运行（本切片不新增迁移，且共享部署正被其他会话使用）。单一 head 由静态检查与真实库的 `version_num` 两侧确认，**未做升级/降级演练**。
- **真实库是共享部署**：核对对象是常驻 `huntweave` 项目的 Postgres。只读查询不影响其他会话，但该库的 46 个 Run 来自历史开发验证，**不是本切片产生的数据**；本记录不据此判定任何业务结论。
- **`dual_read` 只有定义，无运行中的双读**：没有任何已转换来源，兼容读路径无法实测，只能验证其边界与退出条件被写清。
- **43 个冻结输入中 19 个无生产者**：owner 已登记（0010 §7.4），实现随对应切片；本切片不建表。
- **`search` token 是启发式**：`test_a_field_claimed_absent_is_genuinely_nowhere_in_the_real_source` 靠字段名不出现在源码中作证。这是有效的必要条件而非充分条件——一个 `absent` 字段若被实现成同名变量会被正确抓到，但若被实现成另一个名字且目录未更新，则要等到 `partial`/`available` 的载体 token 检查或人工评审才能发现。
- **本记录的完整原因码清单来自正则扫描**：176 处 raise 站点（96 个不同码）加 30 个仅以赋值形式出现的码。正则锚定在 5 个拒绝类型与 `reason_code`/`reason` 赋值上，因此**可能漏掉**以其他变量名传递的原因码（例如经函数参数转发）。既有的 `tests/test_reason_codes.py` 覆盖了另一个方向（前端映射闭合），两者互补但都不宣称穷尽。
- **未复核 CI**：本分支未推送，未触发 `checks`。
- **0010 §9.2 的迁移编号是建议**：实际编号由协调人按合入顺序分配，可能与建议不同。

## 待人工确认

1. **迁移编号分配**：0010 §9.2 建议 `0009_*` 给 #43、`0010_*` 给 #44、`0011_*` 给 #45，D 线从 `0012_*` 起。请协调人在合入时按实际顺序确认或更正。
2. **`0006 §10` 首段清单不完整的处置**：本切片选择「增量补记 + 指向 0010」而不是改写原文（0006 的既有口径不得删除）。若协调人希望 0006 §10 首段直接列出完整清单，需要一次单独的规格修订。
3. **`0003 §8.1` 与 `0006 §10` 的交叉引用**：本轮在两处都加了指向 0010 的句子。若认为规格正文不宜引用盘点件，可只保留 `0003 §8.1` 一处。
4. **`contracts/phase0.py` 的长期归属**：本切片把它登记为「#38 独占」。若后续认为它应并入某个 P2-D 契约文件，需要一次明确的迁移决定（会涉及 `MANIFEST_INPUTS` 的 owner 变更）。
5. **两轴评审**：见「两轴评审与本记录的更正」一节。

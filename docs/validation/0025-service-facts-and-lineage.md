# P2-B1 服务事实、入口身份与跨 Run 只读研究血缘（#44）实施与验证记录

状态：实现、检查、集成与两轴评审已完成，**待合入**。日期：2026-10-11。工作树 `D:\code\huntweave-wt\44-p2-b1-facts-and-lineage`，分支 `codex/44-p2-b1-facts-and-lineage`；实现起点 `main` `c8033c8`，重放到 `main` `d9e5561`（已含 #21 与 #43）。对应 [#44](https://github.com/kksty/HuntWeave/issues/44)，依据 [0003 §5](../specs/0003-agent-research.md)、[0006 §10](../specs/0006-state-model-and-delivery.md)、[0010](../specs/0010-phase0-source-inventory.md)、[ADR-0015](../adr/0015-graph-semantics-and-projection-boundary.md)、[PROJECT §4.2/§5](../../PROJECT.md)。

本记录只写**本切片实际做了什么、实际跑了什么**。阶段与能力状态只在 [STATUS](../STATUS.md) 维护。

**本切片的前一会话把实现留在工作区未提交**（无提交、无验证记录）。本轮先以一次提交固定该实现、再重放到含 #21/#43 的主线、补完集成人负责的共享面、跑真库检查、做两轴评审并按评审修订。首稿结论未被静默改写，评审的发现与处置见第 6 节。

## 1. 环境与入口

| 项 | 值 |
| --- | --- |
| Python | 复用主仓库虚拟环境 `D:\code\HuntWeave\backend\.venv\Scripts\python.exe`；worktree 内不建 venv |
| 工作目录 | worktree 的 `backend/`（pytest 按 rootdir 解析 `pythonpath=src`） |
| 纯检查 | `python -m pytest -m "not integration" -q -p no:cacheprovider` |
| 真实数据库检查 | 一次性 Compose 项目 `hw-review43-pg` 的 `postgres`（`deploy/compose.yaml` + `deploy/compose.isolated-db.yaml`，`127.0.0.1:18931`），**不是**常驻项目 `huntweave`；本轮由 `0009` 升级到 `0010` 后使用 |
| 连接方式 | `HUNTWEAVE_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:18931/huntweave` 加 app/checkpoint/migrator 口令文件、`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` |
| 未触碰 | 常驻项目 `huntweave`（`huntweave-app-1`/`-runner-1`/`-postgres-1`）、#45 的 `hw-e2-retention-postgres-1`(#18455) 均未启停、未清理 |
| 未接触 | 任何外部目标；全部检查只用 `192.0.2.0/24` 文档保留地址、`example` 系主机名与固定假执行 |

## 2. 本切片交付什么

四个记录（ADR-0015）与三个身份：

- **身份**：`contracts/facts.py` 用程序计算 `host_key` / `service_key` / `web_entry_key`；入口键含 scheme、Host/SNI 与 path，因此同一地址端口上的两个应用是两行，而 `service_key` 保持 `IP + transport + port` 的粗粒度。`ObservationWrite` 等写入契约**没有** key 字段 —— 键不可能由调用方提供。
- **观察**：`observations` 追加式；`0010` 装了 `BEFORE UPDATE` 触发器，改写 `content`/`observed_at` 被数据库拒绝。「当前算什么」（更正/撤回/矛盾）由指向它的记录派生，不写回原行。
- **四个记录**：`service_bindings`（实际绑定）、`service_coverage`（覆盖）、`service_navigation`（导航主锚点，带 Run 版本）、`service_settlements` + `service_cost_shares`（成本归因）。
- **血缘**：`research_lineage_references` 冻结来源快照与 hash，检查项目归属、来源版本与保留期，并**没有**可以交出凭据或授权的字段。
- **线索**：`address_clues` 只登记地址与它在授权内与否，**不写任何 scope 行**。

## 3. 集成期做的改动（实现会话按约定没有碰的部分）

实现会话把「`api/app.py` 的装配」留给集成人，且实现基线早于 #21/#43。本轮补完如下：

| # | 问题 | 处置 |
| --- | --- | --- |
| I1 | `0010_service_facts.down_revision` 仍指 `0008_retention_decisions`，`BUSINESS_REVISIONS` 写成 `("0008…", "0010…")`（跳过 0009） | 迁移改为 `down_revision = "0009_planning_attempt_identity"`（docstring 的 `Revises:` 同步），`BUSINESS_REVISIONS` 改为线性三元组；`database.py` 的冲突按线性链解决 |
| I2 | 12 条 facts 路由**在生产线路上不可达**：`create_facts_router` 只被检查文件引用 | `api/app.py` 加 `create_facts_router(database)` 的 `include_router`（12 条路由与既有 39 条内联路由零重叠，因此是加一行，不是删重复） |
| I3 | `docs/specs/0010-phase0-source-inventory.md` 仍写「`Host`/`Service`/`WebEndpoint` **确实不存在**」，且 §3.3 没有 11 张新表 | §3.3 补 11 行；业务表 19→30、遗漏项 14→25；§1 复核项 1/3/7 与 §3.1/§3.2 各加「集成期补记」，把「不存在」标为 **Phase 0 当时**的事实。基线 head 一句**保持 0008**（表清单守卫正是读它算允许范围，改了会削弱守卫）；§3.1 的 `models.py` 行号不回填，避免写未核对的数字 |
| I4 | `ATTRIBUTION_ANCHOR = "through"`、`ATTRIBUTION_PASSED_THROUGH = "anchor"`：**名字与取值互换**，而 `runs/facts.py` 按名字使用（primary 用 ANCHOR），于是每条分担都带相反标签 | 按名字语义把两个取值换回；`test_one_call_bound_to_two_services_is_charged_once_and_reported_twice` 由 RED 转 GREEN |
| I5 | 集成夹具 `_call()` 建 `Decision` 时不给 `task_id`，而 #43 的 `0009` 把该列置为 `NOT NULL` → 25 项检查 `NotNullViolation` | 补 `task_id`（decision 属于独立任务，这是 #43 固定的语义） |
| I6 | `client` 夹具自己又挂了一次 facts router，而 `create_app` 现在也挂载 → FastAPI 报重复 operation id，且检查可能对着产品并不提供的一套路由通过 | 改为只走 `create_app` 组装出的应用 |

## 4. 迁移

新增 **`0010_service_facts`**，`down_revision = "0009_planning_attempt_identity"`，与 `BUSINESS_REVISIONS` 的三元组一致；链仍线性、单一 head。建 11 张表 + `observations` 的 `BEFORE UPDATE` 触发器/函数（`downgrade()` 先丢触发器与函数再丢表，顺序正确）。

在一次性库上**真实升级**并回读：

```
alembic_version:             0010_service_facts      （升级前 0009_planning_attempt_identity）
information_schema 命中新表: 11 / 11
```

## 5. 检查清单与命令输出

```
$ cd <worktree>/backend
$ python -m pytest -m "not integration" -q -p no:cacheprovider
362 passed, 16 skipped, 81 deselected
# 16 项跳过 = 15 项「需要一次性 PostgreSQL 才能求值」+ 1 项「本检出没有控制台构建产物」
#   （test_reason_codes.py:312）。本 worktree 没有 frontend/dist，故后者被跳过。
$ $env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"; python -m pytest -m "not integration" -q
377 passed, 1 skipped, 83 deselected           # 上面 15 项数据库跳过项真跑并通过，只剩控制台构建那 1 项

$ python -m ruff check src tests
All checks passed!

$ python -m mypy --config-file pyproject.toml src
Success: no issues found in 55 source files

$ python -m pytest tests/test_service_facts_integration.py -q
29 passed                                       # 27 项原有 + 2 项本轮按评审新增的守卫

$ python -m pytest -m "integration" --ignore=tests/test_startup_integration.py -q
78 passed, 378 deselected                       # 全量真库：本切片 29 + #21/#43 回归 39 + 其余既有

$ python -m pytest tests/test_phase0_contracts.py -q      # 迁移链、表清单、六轴、冻结输入守卫
35 passed
$ python -m pytest tests/test_reason_codes.py -q
3 passed, 1 skipped
```

`deselected` 由 54 升到 81（+27）几乎全部是本切片新增的集成标记检查。`test_concurrency_stress.py` 以最小非零账本规模复跑通过（取值矩阵归 #32）。

**未跑的项**：容器内检查（`deploy/compose.verify.yaml` 的 checks 容器）、靶场探针、浏览器验收。本切片没有执行端行为，前端未接入（归 P2-F/#65）。

## 6. 两轴评审与按评审的修订

Standards 与 Spec 两轴各由**独立会话**执行（首稿实现会话已不在，故不存在自评审）。评审的发现与处置：

| # | 发现 | 判定 | 处置 |
| --- | --- | --- | --- |
| R1 | **跨服务成本归因在公开 API 上不可达**：`SettlementWrite` 没有承载其余服务的字段，`record_settlement` 也不传 `extra_service_keys`，验收 3 的「跨服务分别记录…成本归因」只能经服务层调用 | 必修（缺失验收项） | 契约新增 `extra_service_keys`，路由逐个校验后透传；新增公开路由守卫检查 |
| R2 | **coverage 不校验 `entry_key`**：只查 `service_key_value`，`cover()` 直接存调用方给的 entry_key，可把入口记到从未服务过它的服务名下 | 必修（调用方提供键） | 校验 entry 属于本 Run 项目**且**属于所指服务，否则 `invalid_request`；覆盖 mismatch 与跨项目两种拒绝 |
| R3 | **`NavigationWrite.service_key_value` 是死字段**：`navigate()` 用 `entry.service_key`，完全忽略调用方声明的服务 | 必修 | 校验两者一致，不一致即 `invalid_request` |
| R4 | observation 去重注释声称唯一约束兜住 NULL；实际 `source_call_id IS NULL` 既不匹配也不触发约束 | 必修（不实断言） | 注释改为陈述真实范围（不命名调用的读取不去重）；**未**改行为，因为改动等于给「无调用来源的观察」新定义身份 |
| R5 | `Observation` 模型与 `0010` 迁移 docstring 声称「数据库拒绝**任何**改动已观测内容的语句」，实际触发器只覆盖 `UPDATE` | 必修（不实断言） | 改为「拒绝重写；不拒绝删除」，并说明本构建没有删除观测的路径 |
| R6 | `DEFAULT_LINEAGE_RETENTION_DAYS = 30` **无人读取**；0006 §5 禁止由保留期推导有效期，该常量正是那种推导 | 必修（Speculative Generality） | 删除 |
| R7 | `_retention_limit` 的 `max(limit, now)` 不可达（前一分支已返回），`session` 参数未使用；函数实际只返回来源 Run 的授权到期 | 判断项 | 收敛为它真正做的事并写明「本构建没有独立保留期、不做默认期回退」；调用点同步 |
| R8 | `models.py` 与 `runs/facts.py` 仍写「分担标记 `primary`/`shared`」，实际存储值是 `anchor`/`through` | 必修（文档与取值不一致） | 措辞统一为 `anchor`/`through` |
| R9 | 读路由（`/projects/{id}/facts` 等）只验全局密钥，不校验会话与项目的归属 | **按平台口径判为不成立** | `PROJECT §3.2` 明确不建账号体系与 RBAC、单用户单全局密钥；`main` 上没有任何路由做会话↔项目归属校验。跨项目的**数据**边界由服务层强制，且已有检查覆盖（`test_a_reference_never_crosses_a_project_boundary`、`test_a_cover_for_a_key_from_another_project_is_refused`、`test_a_correction_cannot_reach_another_projects_observation`） |
| R10 | 重复的成员校验（服务/入口属主判断写了四份）、手写视图字典与 `contracts` 中未被实例化的 `*View` 重复、`record_clue` 为一次逻辑写开第二个事务、`ClueDecision.joins_the_scope` 无人读、`record_clue(anchor=…)` 使服务层自身的范围计算在生产中成死代码、更正身份只比 `service_key` 不比 `entry_key` | 判断项，**未改** | 均属结构性问题而非违反文档标准；改动会扩大本切片范围。逐条登记在“未达成与限制”，建议另开维护票 |
| R11 | `runs/service.py` 把 `mode`/`execution_profile` 写进冻结的 `ScopeSnapshot`（属 #43 期的授权语义），与本切片的「服务事实」无关 | 范围观察 | 该改动是授权快照语义变更，不由本切片引入也不由本切片撤回；登记在第 8 节请维护者确认归属 |

### 变异验证

R1 与 R2 各自把校验写错一次，确认对应守卫变红：

| 变异 | 把什么写错 | 目标检查 | 结果 |
| --- | --- | --- | --- |
| M1 | 结算路由不再透传 `extra_service_keys` | `test_a_cross_service_settlement_is_reachable_through_the_public_api` | RED `1 failed in 0.61s` |
| M2 | coverage 不再校验 entry 与 service 是否同一主体 | `test_the_public_api_refuses_a_service_and_an_entry_that_are_not_the_same_subject` | RED `1 failed in 0.56s` |

I4（归属标签）与 I5（夹具 `task_id`）的变异证据是**自带的**：修复前对应检查分别是 `1 failed` 与 `25 errors`，修复后转绿。

## 7. 逐条验收结论

| # | 验收标准（Issue #44） | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 程序计算 Host/Service/WebEndpoint 身份，保留连接 IP、transport、port、Host/SNI/path；不同入口不被过度合并 | **通过** | `contracts/facts.py` 的 `host_key`/`service_key`/`web_entry_key`；`ObservationWrite` 等契约无 key 字段（`test_the_api_computes_the_identity_instead_of_accepting_one`）；纯检查覆盖同一入口的不同拼写折叠为一、同址两个应用为两条、scheme/path/port 任一不同即不同入口、Host 与地址寻址不合并、IDNA 与非 ASCII 主机归一、zone id 拒绝 |
| 2 | 观察不可覆盖原文，保存 Run/call/时间/访问条件/解析版本；矛盾、更正与撤回并存可查 | **通过（带一处限制）** | `0010` 的 `BEFORE UPDATE` 触发器在真库上拒绝改写 `content`/`observed_at`；同一调用同一事实重复观察为一条、同一事实的第二个 Run 是第二条（`test_the_same_reading_taken_twice_by_one_call_is_one_record`、`…by_a_second_run…`）；更正跨主体/跨项目被拒；冻结快照不产生第二条观察。**限制**：不命名调用的观察不做去重（R4），且触发器不约束 `DELETE`（R5） |
| 3 | 研究关系有明确来源与版本；跨服务分别记录绑定、覆盖、导航主锚点、成本归因，不按主锚点重复结算 | **通过**（公开 API 的可达性由 R1 补上） | 四张表分离；一次调用绑两个服务只结算一次、报告两次，归因为 `anchor`/`through`（`…charged_once_and_reported_twice`，本轮修正标签后转绿）；绑定按调用+服务去重；覆盖区分「直接验证」与「经过」；移动主锚点不写覆盖也不写成本；过期 Run 版本被拒 |
| 4 | 历史引用显式关联、检查项目归属/权限/保留期；只读固定版本，不继承凭据、当前授权、本轮实证或覆盖 | **通过** | `research_lineage_references` 冻结快照 + hash；跨项目、来源版本不符、材料已过保留期均被拒；契约没有可交出凭据或授权的字段；冻结快照不成为第二条观察；`test_a_cover_for_a_key_from_another_project_is_refused` 覆盖跨项目键 |
| 5 | 新增地址只作为线索，不扩大授权；查询与历史关联 API 受服务端鉴权与版本约束 | **通过** | `address_clues` 只登记地址与「在授权内与否」，不写 scope（纯检查覆盖重定向到新地址仍是线索、发现的域名在 IP-only 授权之外、授权地址的非授权端口仍记为界外）；`test_every_facts_route_refuses_an_unauthenticated_caller` 覆盖全部 facts 路由；版本约束见 `test_a_stale_run_version_is_refused…` |

## 8. 共享面影响

- **`api/app.py`**：本切片唯一改动的一行装配（I2）。**没有**重复路由，无需删除内联处理器。
- **`contracts/facts.py`**：新增（本切片独占），本轮按 R1 增 `SettlementWrite.extra_service_keys`。
- **`storage/database.py`**：`BUSINESS_REVISIONS` 线性三元组（I1）。
- **`docs/specs/0010-phase0-source-inventory.md`**：表清单与 Phase 0 结论补记（I3）。**基线 head 一句未改**，因此表清单守卫的允许范围没有被放宽；`test_phase0_contracts.py` 35 项保持通过。
- **原因码：零新增。** `scope_denied` / `version_conflict` / `run_not_found` / `invalid_request` / `invalid_ip` 全部复用；`tests/test_reason_codes.py` 与 `frontend/src/workspace.ts` 无需改动。
- **`runs/service.py`**：见 R11（`ScopeSnapshot` 增加 `mode`/`execution_profile`），归属请维护者确认。
- **未改**：`PROJECT.md`、`docs/STATUS.md`、`docs/adr/**`、`docs/specs/0003-agent-research.md`、`frontend/**`、`deploy/**`。

## 9. 未达成与限制

1. **未跑容器内检查、靶场探针与浏览器验收**：本切片没有执行端行为；前端未接入。
2. **公开 API 的读面与写面不等宽**：本轮已让跨服务结算可达（R1），但 R10 登记的重复成员校验、未被实例化的 `*View`、`record_clue` 的第二事务、更正身份只比 `service_key` 等结构性项**未改**。
3. **`DELETE` 不受 append-only 触发器约束**（R5）：`0003 §4` 的要求是「不可覆盖原文」，`UPDATE` 保护已满足；删除只由迁移与运维拥有，本构建没有删除观测的路径。
4. **不命名调用的观察不去重**（R4）：这类记录没有生产者身份，平台不为它编造一个。
5. **`_retention_limit` 用授权到期当保留上限**（R7）：本构建没有独立的保留期来源，也不做默认期回退；`0006 §5` 禁止把它读成「证据有效期」。
6. **没有把 11 张新表映射进六轴的载体表**：`0010` 记录的是真实 schema，六轴载体判断仍归 `0006 §10` 与后续切片。
7. **`docs/STATUS.md` 与 `docs/validation/README.md` 的索引行由集成人在合入时回接**；本记录不宣布阶段状态。

## 10. 待人工确认

1. **迁移顺序**：本切片已按 #43→#44→#45 顺序改为 `down_revision = "0009_planning_attempt_identity"`；#45 的 `0011` 需改为 `0010_service_facts`，合入时核对单一 head。
2. **R9 的判定**：把「读路由不校验项目归属」按单操作员平台口径判为不成立（依据 `PROJECT §3.2`）。若维护者认为即使单用户也要求会话↔项目绑定，这是一条**新**要求，应另开票而不是塞进 #44。
3. **R11 的归属**：`ScopeSnapshot` 增加 `mode`/`execution_profile` 是授权语义变更，请确认它随 #44 合入、还是拆到别的票。
4. **R10 的后续**：建议为「重复成员校验/未实例化视图/`record_clue` 双事务/更正身份范围」开一张维护票，而不是在本切片返工。

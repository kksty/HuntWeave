# P2 多切片并行的集成交接（2026-10-11，第二轮）

本文件是**一次协调会话的交接记录**，供下一个会话接续。它只记「当前进行到哪、还差什么、按什么顺序合」；阶段、能力与下一实施项仍只在 [STATUS](../STATUS.md) 维护，验收要求见 [0003 §8–9](../specs/0003-agent-research.md) 与 [PROJECT §14](../../PROJECT.md)，分批顺序见 [P2 切片分批执行计划](./p2-execution-batches.md)。

> **本文件先前的版本有三处判断错误，已更正**：① `#43` 的 rebase 状态并非「目录已不在、必须按 reflog 重建」——它是**可继续**的；② `#45` **不是**「加一行 `include_router`」的活，`api/app.py` 里有两条**必须删除**的重复路由；③ `#43` 的合成遗留了三处**只在集成后才可见**的缺陷，其中一处使移植过来的背压语义**零验证**。逐条见第 9 节。

## 1. 已合入 `main`

| 票 | 合入点 | 交付 | 验证记录 |
| --- | --- | --- | --- |
| [#37](https://github.com/kksty/HuntWeave/issues/37) 契约对齐 | `1e5b0eb` | [ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md)、`PROJECT.md` 0.9.16、四组矛盾口径更正 | [0020](../validation/0020-contract-alignment.md) |
| [#33](https://github.com/kksty/HuntWeave/issues/33) V-A token 层与字体自托管 | `3ca3684` | `frontend/src/tokens.css`（唯一色值来源）、五个 OFL 字族自托管、`npm run check:design`（16 项，**已接入 CI**） | [0021](../validation/0021-visual-tokens.md) |
| [#38](https://github.com/kksty/HuntWeave/issues/38) P2 Phase 0 | `c8033c8` | [0010 来源盘点与契约交接](../specs/0010-phase0-source-inventory.md)、`contracts/phase0.py`、8 个必失败样例 | [0023](../validation/0023-phase0-source-inventory.md) |
| [#21](https://github.com/kksty/HuntWeave/issues/21) 多活跃 Run 并发压力验收 | `42cf892` | `contracts/resources.py` 版本化资源政策、同 IP 串行、额度耗尽背压、`claim()` 不再白占轮次 | [0022](../validation/0022-concurrency-quota.md) |
| [#43](https://github.com/kksty/HuntWeave/issues/43) P2-A1 事务外模型请求 | `2ca7ca8` | 三段协议 `begin_planning → 事务外模型判断 → commit_planning`、独立任务/会话/决策身份、`0009_planning_attempt_identity`；合入后又补了 `#21` 压力夹具的 `task_id` 与 `0003` 的 `plan()` 引用（`d9e5561`） | [0024](../validation/0024-model-outside-transactions.md) |
| [#44](https://github.com/kksty/HuntWeave/issues/44) P2-B1 服务事实与血缘 | `8a0d239`（**`main` 现 head**） | Host/Service/WebEndpoint 程序计算身份、追加式观察、ADR-0015 的四记录、跨 Run 冻结血缘、线索不扩授权、`0010_service_facts`（11 张表） | [0025](../validation/0025-service-facts-and-lineage.md) |

`main` 现 head：`55f050c`，比 `origin/main` **ahead 21**，**尚未 push**。GitHub 侧的 Issue 状态**一律未动**（未评论、未关闭、未改标签）：推送与关闭由维护者决定。

`main` 上的纯检查（`cd backend`，`python -m pytest -m "not integration" -q`）：**394 passed, 15 skipped, 92 deselected**；真库上（除需活栈的 startup）**87 passed**；`test_phase0_contracts.py` **35 passed**；ruff、mypy 干净。**注意**：`test_reason_codes.py` 会读本检出已构建的控制台产物，因此改过原因码或前端文案后**必须重建 `frontend/dist`**（本轮就因产物陈旧红过一次，`npm run build` 后转绿）。

> **`#45` 已合入。** Spec 轴评审曾发现一个阻塞项：`published` 被实现成「本页走到了哪」而不是契约写明的「a *fresh* read can actually continue through」（真库实测 600 条事件、`limit=500` 时给出 `committed=600, published=500`）。**已修并在保留有界代价的前提下留守卫**：`cursors` 仍是本页，`published` 改由 `_contiguous_end` 结算（两条索引聚合的常路径，只有真存在洞时才逐行走）。由此失效的夹具与措辞一并更正（`event_history_writer_in_flight` → `event_history_missing_position`）。取舍理由与证据见 [0026](../validation/0026-event-retention-and-resync.md) 第 6b、10 节；**若维护者更愿意放弃「走满连续区间」语义，需要同时改契约说明、视图与记录**。
>
> 仍未处理的是 [0026](../validation/0026-event-retention-and-resync.md) 第 9 节列的结构性项（清理的两段式持锁、409 不带水位、没有快照身份、夹具无漂移检查、`page_events` 一函数两职、`stream_probe` 自建 `AccessService` 等），建议另开维护票。

## 2. 进行中的分支：**没有了**

三支（`#43`、`#44`、`#45`）都已合入 `main` 并**退役**（worktree 与分支都已删除，安全快照 tag 也已删除）。`git worktree list` 现在只剩主检出。

分支命名 `codex/<编号>-<slug>`，worktree 在 `D:\code\huntweave-wt\<编号>-<slug>`；开工时按此约定新建。

`#44` 与 `#45` 都已合入并退役（worktree 与分支都已删除）。`#45` 的 5 个提交依次是：前一会话的实现原样固定 → 集成补完（迁移线性化、删两条同路径内联路由、修 `created_at` 覆写与三处夹具缺陷）→ 按 Standards 轴修三处不实陈述 → 验证记录 0026 → 按 Spec 轴修 `published` 语义并更正由此失效的夹具与措辞。

## 3. 合入顺序与迁移链（已完成，链尾 `0011_event_retention`）

三支的迁移已按 `0009_planning_attempt_identity`（#43）→ `0010_service_facts`（#44）→ `0011_event_retention`（#45）线性化，`main` 上现在是 **`0001`–`0011` 共 11 个 revision、单一 head = `0011_event_retention`**，`BUSINESS_REVISIONS` 为四项线性元组，且 `0009 → 0010 → 0011` 都在一次性库上真实升级并回读过。

`tests/test_phase0_contracts.py::test_the_migration_chain_is_linear_and_has_exactly_one_head` 会抓分叉（现 35 项全绿）。注意同一文件的表清单守卫读 `0010-phase0-source-inventory.md` 里**声明为基线的 head**（仍保持 `0008`，这是有意留的），改那句话会同时改守卫允许的表范围——两者要一起改。

## 4. `#43` 的合成：已做完，但本轮查出三处漏项

`#43` 删除了旧的整段 `plan()`；而 `#21` 此前把**物理额度与背压**逻辑插进了那个旧 `plan()`。所以重放不是二选一，而是**把 #21 的额度语义移植进 #43 的新结构**。逐条核对（均在 `runs/orchestration.py::_dispatch`），**语义全部在场**：锁先于计数、与 `ToolCall`/`BudgetReservation`/`Outbox` 同一短事务；背压写去重事件（`str(decision_id) + ":backpressure"`）后 `return None`、不留下任何行；受控重派走同一额度门（额度门在 `replacing` 分支之后）；`_reserved` 承载预算语义；「决策已提交但调用未预留」在 `begin_planning` 的已提交分支可续跑。

同一轮查出并修复三处**只在集成后才可见**的缺陷，细节与证据见 [0024](../validation/0024-model-outside-transactions.md)：

- **D1（最严重）**：`#43` 删了 `plan()`，但 `#21` 的 `test_concurrency_integration.py`（5 处）与 `test_concurrency_stress.py`（3 处）仍在调用它。两个文件都是 `pytestmark = integration`，纯检查与 CI 都跑 `-m "not integration"`，ruff 看不到属性、mypy 只扫 `src` —— 于是**移植过来的背压/额度语义当时没有任何检查在跑**。已改为经 `tests/_planning.py` 的 `plan_step` 走三段协议。
- **D2**：`begin_planning` 的续跑分支直接 `return self._dispatch(...)`（形状里没有 `kind`），而唯一消费者 `harness/graph.py` 读 `handoff["kind"]` → 在「答案已提交、call 因额度被扣下、额度已归还」这条续跑路径上抛 `KeyError`。**这正是交接文档点名「丢了会把 Run 卡死」的那条语义。** 已包成 `{"kind": "reuse", "decision": ...}`，额度仍未归还时返回 `None`。
- **D3**：`#43` 自己的 `settle()` 夹具不带 `ExecutionObservation`；按 `#21` 收紧后的规则「已结算」≠「已确认停止」，Run 因此不再可领取。已补上执行端的停止陈述。

D1 与 D3 是同一类：**实现期的夹具按 `c8033c8` 的语义写，重放到语义已变的主线后，检查要么静默消失（D1）、要么变红（D3）**。下一个会话处理 `#44`/`#45` 时，默认预期同类问题：**先 `grep` 被 `#43`/`#21` 改动的符号（`plan(`、`.model`、`settle`、`ExecutionObservation`、`EventPage`、`history(`），再跑真库检查。**

## 5. `#44` 集成清单（已核对，可直接执行）

- **`api/app.py`：加两行即可，无路由冲突。** `create_facts_router(engine: Callable[[], Engine])` 有 12 条全新路由，与 `main` 现有 39 条内联路由**零重叠**：`from huntweave.api.facts import create_facts_router` + `app.include_router(create_facts_router(database))`。
- **迁移**：见第 3 节。`BUSINESS_REVISIONS` 当前被写成 `("0008...", "0010...")`（**跳过了 0009**），必须改。
- **原因码：零新增**，`contracts/errors.py` 与 `frontend/src/workspace.ts` 都不用改。
- **`contracts/phase0.py`**：把 `Host`/`Service`/`WebEndpoint` 从 `LEGACY_OBJECTS_ABSENT` 移入 `LEGACY_OBJECTS_PRESENT`（已做），并声明 11 张新表：`hosts`、`services`、`web_endpoints`、`observations`、`service_bindings`、`service_coverage`、`service_navigation`、`service_settlements`、`service_cost_shares`、`research_lineage_references`、`address_clues`（与 `models.py`、迁移三处一致）。
- **`docs/specs/0010-phase0-source-inventory.md` 要改（协调人负责，`#44` 自己没改）**：`§3.3` 表补 11 行；`:15` 的「`Host`/`Service`/`WebEndpoint` **确实不存在**」改为存在；`:13`「另有 14 个对象」与 `:58`/`:77` 的「19 张业务表」按新的真实值改（对象 14→25、业务表 19→30）；`:73` 的「当前单一 head = `0008_retention_decisions`」随迁移线性化更新。
- **两处待处理的实现问题**（评审判断项，可留可改，但不要当成已确认正确）：`runs/facts.py` 的 `_retention_limit` 里 `max(limit, now)` 分支不可达（死代码）；`contracts/facts.py` 的 `ATTRIBUTION_ANCHOR = "through"` 与 `ATTRIBUTION_PASSED_THROUGH = "anchor"` **名字与取值互换**。
- **测试**：`tests/test_service_facts.py` 24 项纯检查；`tests/test_service_facts_integration.py` 29 项（`integration` 标记）。注意它的集成用例**自己挂载 router**（因为 `app.py` 当时没有 include），接线后应改为走打包好的 app，否则它证明不了生产路由集合。

## 6. `#45` 集成清单（已核对，可直接执行）

- **`api/app.py` 不是「加一行」，必须删两条内联路由。** `create_events_router(settings, database, mode_reader=None)` 注册 4 条路由，其中 `GET /api/v1/runs/{run_id}/event-history`（`app.py:460-466`）与 `GET /api/v1/runs/{run_id}/events`（`app.py:518-562`，SSE）与内联处理器**路径+方法完全相同**。两者都注册时 FastAPI 取先注册的（内联那条），**切片会被静默架空**。删掉这两段，再加 `from huntweave.api.events import create_events_router` + `app.include_router(create_events_router(settings, database, capabilities_probe.current))`（`mode_reader` 要传能力探针，SSE 心跳的 `mode` 从这里取）。
- **删除后 `app.py` 变成未使用的导入**：`asyncio`、`time`、`json`、`AsyncIterator`、`StreamingResponse`、`EventPage`。**`SQLAlchemyError` 必须保留**（异常处理器仍在用），`run_in_threadpool` 也保留（中间件仍在用）；`Request` 保留。
- **迁移**：见第 3 节。`BUSINESS_REVISIONS` 当前被写成 `("0008...", "0011...")`（**跳过 0009/0010**），必须改。
- **原因码**：新增 `event_cursor_expired`（`runs/events.py`，409）。`contracts/errors.py` 里**没有任何原因码常量**（只有 `ServiceError`），所以「三处同步」的机械部分是 `tests/test_reason_codes.py` 的候选扫描——它会自动收录；`frontend/src/workspace.ts` 的文案已加（worktree 第 156 行）。**仍应人工确认 `test_reason_codes.py` 真的收录了它**（跑一次该文件即可），别只依赖「机械收录」的说法。
- **`#43` 的连带损伤**：`tests/test_event_retention_integration.py:326` 调用被删除的 `orchestration.plan(...)`，rebase 后会 `AttributeError`（且它被 `integration` 标记挡住，纯检查看不见）。
- **ruff 有 5 处 E501**：4 处在新增文件（`0011_event_retention.py` 3 处、`test_event_retention_and_resync.py` 1 处），1 处是既有的 `0007_drop_login_throttle.py:7`（不是本票引入，按仓库现状处理）。
- **配置未文档化**：新增 `HUNTWEAVE_EVENT_RETENTION_KEEP`（默认 `0` = 不清理），`deploy/compose.yaml` 与 `README.md` 都**没有**记录它。README 的保留策略表只列了 `HUNTWEAVE_RETENTION_*`。
- **前端未接**：`frontend/src/RunConsole.vue:178,185` 仍消费旧的 `{events, next_cursor, gap}` 形状与旧 SSE 帧。`EventHistoryView` 比旧的 `EventPage` 宽（`extra="forbid"`），所以旧客户端容忍；但前端换到新帧属另一票，别在本票顺手扩大范围。

## 7. 共享面所有权（沿用，并补充本轮实测）

- `api/app.py` 的**路由装配归集成人**：实现会话在 `api/` 下写导出 router 工厂，把要加的 `include_router` 一行写进报告，**不改 `app.py`**。本轮实测：**加一行前必须先确认有没有重复路由**（`#45` 就是反例）。
- `contracts/errors.py` 的原因码是共享面。本轮确认：该文件**没有原因码常量**，权威载体是 `backend_candidates()` 扫描的源码用法 + `tests/test_reason_codes.py` + `frontend/src/workspace.ts` 的 `messages`。新增原因码时三处都要看。
- `docs/specs/0010-phase0-source-inventory.md` 与 `contracts/phase0.py` 的事实维护归集成人（见第 5 节）。
- Alembic 迁移链、`contracts/` 共享模型、前端路由表：同 [分批执行计划](./p2-execution-batches.md) 第 4 节。

## 8. 遗留环境与清理

- **一次性** Compose 项目仍在运行（都不是常驻项目）：
  - `hw-review43-pg`：postgres，`127.0.0.1:18931`，已 migrate 到 `0009_planning_attempt_identity` —— `#43` 集成期用的库。**下一轮做 `#44`/`#45` 的迁移线性化与集成检查时可以直接复用它**（它就是为此建的）。
  - `huntweave-i44-facts`：postgres，`127.0.0.1:18777` —— `#44` 的一次性库。
  - `hw-e2-retention`：postgres，`127.0.0.1:18455` —— `#45` 的一次性库。
  - 各自清理：`docker compose --project-name <name> -f <config files> down -v`；**不要**用日常项目名。
- 常驻项目 `huntweave`（`huntweave-app-1`/`-runner-1`/`-postgres-1`，端口 8000）全程**只读**，未启停、未清理。
- 连接一次性库跑集成检查的环境变量（本轮实测可用）：

  ```powershell
  $env:HUNTWEAVE_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:18931/huntweave"
  $env:HUNTWEAVE_APP_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\app_db_password"
  $env:HUNTWEAVE_CHECKPOINT_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\checkpoint_db_password"
  $env:HUNTWEAVE_MIGRATOR_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\migrator_db_password"
  $env:HUNTWEAVE_RUNNER_TOKEN_FILE = "D:\code\HuntWeave\runtime\secrets\runner_token"
  $env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
  ```

  worktree 里没有 `.venv`，用主仓库的 `D:\code\HuntWeave\backend\.venv\Scripts\python.exe`，并在 **worktree 的 `backend/` 目录内**运行（pytest 按 rootdir 解析 `pythonpath=src`）。
- 两个安全快照 tag（`wip-44-preintegration`、`wip-45-preintegration`）在两支交付并删除分支/工作树后一并删除。

## 9. 本文件先前的错误（逐条更正）

| # | 先前版本的说法 | 实际 | 影响 |
| --- | --- | --- | --- |
| 1 | 「rebase 目录已不在，所以它是一个被中断的 rebase 残留，不是可继续的 rebase 状态」；建议「以 `HEAD` 重建提交」 | `.git/worktrees/43-…/rebase-merge` **完整存在**，停在 3/6，`stopped-sha = 3c891c4`；冲突标记已在工作区消除但未 `git add`。正确做法是 `git add` 后以 `rebase-merge/message` 重建该提交并 `rebase --continue` | 按旧说法重建会丢掉已解决的合成结果；本轮按正确路径走完 6 步 |
| 2 | 第 5 节只写「`#44` 已提供 `include_router` 那一行」，对 `#45` 未提 `app.py` | `#45` 有两条**重复路由必须删除**，否则切片被静默架空；`#43` 则确实不需要新入口 | 只加 include 会让 `#45` 看起来通过而实际走旧内联处理器 |
| 3 | 第 4 节把「`tests/test_execution_quota.py` + #43 身份/协议检查同时通过」当作「这次合成的真正验收」 | 那一条**不足以**验收：#21 真正守住背压/额度语义的是两个 `integration` 文件，而它们当时在调用已删除的 `plan()`，**从未跑过** | 合成一度带着 D1/D2 两处缺陷进入「检查已通过」状态 |

## 10. 未决与待用户决定（不变）

- **CIDR 是否纳入范围**（`0008 §6` 的 5 项待确认，[ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md) 记为唯一未决项）：未决定前保留 IP-only 与 `TARGET_LIMIT = 100`，C-F（[#30](https://github.com/kksty/HuntWeave/issues/30)）不含 CIDR 展开。**只阻塞 C-F**。
- **P1 阶段与里程碑**：P1 切片已全部交付，但**真实执行仍未开放**——[ADR-0010](../adr/0010-real-execution-boundary-and-gate.md) 四项门槛尚缺 `profile_revalidation` 与 `deployment_revert`，产品继续拒绝创建真实 Run。门槛补齐归 [#39](https://github.com/kksty/HuntWeave/issues/39)。**阶段退出与里程碑关闭是维护者决定**，本文件不代为宣布。
- **推送与关闭 Issue**：`main` 已 ahead 15 且未 push；GitHub 侧一律未动（未评论、未关闭、未改标签）。这是维护者的动作。

## 11. 后续可执行前沿（不变）

`#45` 合入后：`#46`/`#47`（维护收尾，等 `#40`–`#42`）、`#49`–`#52`（第二层），以及关键路径上的 `#39`→`#59`→…。可选票 `#48`（Linux 宿主复验）在未被领取前不进入任何一批的可执行前沿；`#32` 是 **P1 期的实测票**（三个热点：`agentd` 顺序推进、`ledger._persist()` 全量序列化、事件游标行锁），与 `#21` **串行使用验收环境**、口径不同。

## 12. 本轮结束时的环境与遗留（下一会话直接可用）

- **一次性数据库**（不是常驻项目，`huntweave` 全程只读）：
  - `hw-review43-pg`：postgres，`127.0.0.1:18931`，schema 已升到 **`0011_event_retention`**。`#45` 的续作直接用它即可。
  - `hw-e2-retention`：postgres，`127.0.0.1:18455`（`#45` 自己的那份，未被本轮使用）。
  - `#44` 的 `huntweave-i44-facts` 已随其 worktree 退役**清理完毕**（容器、卷、网络）。
- **连接方式**（本轮实测可用，worktree 内没有 `.venv`，用主仓库的）：

  ```powershell
  $env:HUNTWEAVE_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:18931/huntweave"
  $env:HUNTWEAVE_APP_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\app_db_password"
  $env:HUNTWEAVE_CHECKPOINT_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\checkpoint_db_password"
  $env:HUNTWEAVE_MIGRATOR_DB_PASSWORD_FILE = "D:\code\HuntWeave\runtime\secrets\migrator_db_password"
  $env:HUNTWEAVE_RUNNER_TOKEN_FILE = "D:\code\HuntWeave\runtime\secrets\runner_token"
  $env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"
  ```

  在 **worktree 的 `backend/` 目录内**运行（pytest 按 rootdir 解析 `pythonpath=src`）。
- **同一个一次性库不能被两个检查进程同时使用**：`conftest.py` 的 autouse 夹具会清空 Run 相关表，并发跑会出现**假失败**（本轮出现过一次 8 项假失败）。要并行就各起一个项目/端口。
- 安全快照 tag（`wip-44-preintegration`、`wip-45-preintegration`）已删除：两支的工作都已提交在分支上。
- 分支与 worktree 的清理规则：合入后立即 `git worktree remove` ＋ `git branch -d`（`#43`、`#44`、`#45` 都已办理）。
- 两个一次性库（`hw-review43-pg`、`hw-e2-retention`）与 `#44` 的 `huntweave-i44-facts` 都已清理：三支合入后不再需要，现在只剩常驻项目 `huntweave`。

## 13. 浏览器验收（本轮首次执行）与一个未结的发现

三支的验证记录都写明「未跑浏览器验收」，本轮**补跑了**：用 README 的一次性栈流程（`huntweave-p0-checks`，`127.0.0.1:18000`，`docker compose … build app` ＋ `up -d --wait`），`npx playwright install chromium`，然后 `npm run test:e2e:full`。

**结果：14 passed / 1 failed / 1 skipped。** 栈与卷已 `down -v` 清理干净，常驻项目未受影响。

唯一失败是 [\#79](https://github.com/kksty/HuntWeave/issues/79)，要点：

- `authorized-run.spec.ts:158`（暂停 → 重载 → 恢复 → 证据 → 人工结束）在**合入后确定性失败**，在**合入前通过**（A/B：新代码跑一次性栈、旧代码跑常驻栈，spec 文件未被本批改动）。
- 失败是 `暂停 Run` 按钮在 5s 内点不到。查一次性栈的库：该 Run 到达 **`waiting`/`awaiting_human`**、12 个调用全部 `succeeded`、216 条事件、自动阶段约 **6.96s**——**终态正确**，所以直接原因是自动阶段快于测试的 `actionTimeout(5s)`，而不是暂停功能坏掉。
- 取消用例（`:214`）通过，因为 `canCancel` 覆盖到 `waiting`；而 **`暂停 Run` 只有这一条用例覆盖**，窗口收窄后这条操作路径实际上没有可复现的验收了。
- 已排除：`FakeParameters(duration_ms=…)` 合入前后完全一致。
- **未定位**：为什么自动阶段变快了（候选：模型调用移出行锁事务后调度轮次不再被串行化；`#21` 的 `claim()` 不再让在飞 Run 白占轮次）。

**下一会话若要继续这件事，先判断 #79 是「产品缺陷」还是「测试的时序假设过期」**——终态正确、按钮条件未变，我倾向后者，但那要由维护者定，且无论如何都该补一条不依赖时序的暂停检查。**另注意：这些记录里的「未跑容器内检查与靶场探针」仍然成立**（本轮只补了浏览器验收）。


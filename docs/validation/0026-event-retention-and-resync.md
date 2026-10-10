# P2-E2 事件保留、过期游标与可靠快照补拉（#45）实施与验证记录

状态：实现、检查、集成与两轴评审已完成，**随本分支合入**。日期：2026-10-11。工作树 `D:\code\huntweave-wt\45-p2-e2-event-retention-resync`，分支 `codex/45-p2-e2-event-retention-resync`；实现起点 `main` `c8033c8`，重放到 `main` `8a0d239`（已含 #21、#43、#44）。对应 [#45](https://github.com/kksty/HuntWeave/issues/45)，依据 [PROJECT §12.1](../../PROJECT.md)、[0006 §9](../specs/0006-state-model-and-delivery.md)、[0002 §7](../specs/0002-real-execution.md)、[0010 §3.5](../specs/0010-phase0-source-inventory.md)、[ADR-0015](../adr/0015-graph-semantics-and-projection-boundary.md)。

**Spec 轴评审发现的阻塞项已修复并留守卫，不是绕过。** `published` 曾是「本页走到了哪」而不是「一个全新读者能继续到哪」（真库实测 600 条事件、`limit=500` 时给出 `committed=600, published=500`）。修复的依据是：**契约本来就写明了意图**（`contracts/event_stream.py`：`published` 是"a *fresh* read can actually continue through"），所以这是让实现符合已写明的契约，而不是新做一个产品决定。修法与证据见第 6b 节 P1。

本记录只写**本切片实际做了什么、实际跑了什么**。阶段与能力状态只在 [STATUS](../STATUS.md) 维护。

**本切片的前一会话把实现留在工作区未提交**，且它的集成检查从未跑过（被 `integration` 标记挡住）。本轮先固定该实现、重放到含 #21/#43/#44 的主线、补完集成人负责的共享面、跑真库检查、做两轴评审并按评审修订。两轴评审均已完成，发现见第 6、6b 节。

## 1. 环境与入口

| 项 | 值 |
| --- | --- |
| Python | 复用主仓库虚拟环境 `D:\code\HuntWeave\backend\.venv\Scripts\python.exe`；worktree 内不建 venv |
| 工作目录 | worktree 的 `backend/`（pytest 按 rootdir 解析 `pythonpath=src`） |
| 纯检查 | `python -m pytest -m "not integration" -q -p no:cacheprovider` |
| 真实数据库检查 | 一次性 Compose 项目 `hw-review43-pg` 的 `postgres`（`deploy/compose.yaml` + `deploy/compose.isolated-db.yaml`，`127.0.0.1:18931`），**不是**常驻项目 `huntweave`；本轮由 `0010` 升级到 `0011` 后使用 |
| 未触碰 | 常驻项目 `huntweave`（`huntweave-app-1`/`-runner-1`/`-postgres-1`）与 #21 的一次性库均未启停、未清理 |
| 未接触 | 任何外部目标；全部检查只用文档保留地址与固定假执行 |
| 并发注意 | 本轮两个独立评审会话与我共用同一个一次性库，**并发跑同一套检查会互相清空夹具**，曾出现一次 8 项假失败；本节所有计数都是在没有并发运行者时测得的 |

## 2. 本切片交付什么

三个位置与两种补拉路径：

- **三个位置**：`committed`（已被某事务认领的最高游标）、`published`（一个全新读者真正能继续的位置）、`retained_from`（已被清理的最高游标）。三者一起出现在 `EventWindow` 与 `EventHistoryView` 里，因为只拿到其中一个无法区分「已追上」「被截断」「问到了写者前面」。
- **过期游标**：`after < retained_from` 是**缺口**而不是空页，答 `event_cursor_expired`（409）并带 `retained_from`，客户端据此重载状态快照；`after > committed` 或落在已回滚的洞里答 `event_cursor_ahead`。绝不把最老的幸存事件当作「时间线的开头」返回。
- **清理**：`prune_events(session, run_id, keep_from)` 只删 `audit_events` 与推进水位，业务行、调用、结果与证据索引一律不动；它自己追加一条 `event_retention_applied` 事件作为时间线上的标记，并把 `retained_from_cursor` 写进游标行。
- **迟到事件**：`append_event` 新增 `occurred_at`，写进 payload；发布游标仍然前进，所以已过该位置的客户端不会被塞进一条插在身后的事件。
- **SSE**：保留 P1 的帧形状（`id:`/`event: audit`/`data:`）、≤5 秒心跳（携带平台真实 `mode`）与每 15 秒重读会话（撤销在 60 秒界内生效）。
- **固定夹具**：`backend/tests/data/events/` 8 个响应夹具，分别钉住一个可区分的状态。

## 3. 集成期做的改动

实现会话按约定把 `api/app.py` 的装配留给集成人，且实现基线早于 #21/#43/#44。

| # | 问题 | 处置 |
| --- | --- | --- |
| I1 | `0011_event_retention.down_revision` 仍指 `0008`，`BUSINESS_REVISIONS` 写成 `("0008…", "0011…")`（跳过 0009/0010） | 迁移改为 `down_revision = "0010_service_facts"`，`BUSINESS_REVISIONS` 改为四项线性元组；在一次性库上**真实升级** `0010 → 0011` 并回读 |
| I2 | **4 条路由里有 2 条与既有内联处理器路径+方法完全相同**：`/runs/{run_id}/event-history` 与 `/runs/{run_id}/events`（SSE）。FastAPI 取先注册的那条，只加 `include_router` 会让**旧内联实现继续应答、切片被静默架空** | 删掉这两段内联实现，再挂载 router（`mode_reader` 传能力探针，因为心跳要报平台真实 mode）；顺带清掉因此不再使用的 6 个导入（`asyncio`/`json`/`time`/`AsyncIterator`/`StreamingResponse`/`EventPage`） |
| I3 | ruff 报 E501（新文件与我的注释） | 逐条折行 |
| I4 | `tests/test_event_retention_integration.py` 仍调用 #43 已删除的 `orchestration.plan(...)` | 改为经 `tests/_planning.py` 的 `plan_step` 走三段协议 |
| I5 | **`append_event` 把 `created_at` 覆写成 `occurred_at`**（`main` 原本是 `database_now`）。这删掉了「这条事件多久之后才到」的唯一记录，使迟到事件与准时事件无法区分；本切片自己声明的范围也只是「只加 `occurred_at`」 | 恢复 `created_at = database_now(session)`，`occurred_at` 仍写进 payload；`test_a_late_event_keeps_its_own_time_and_takes_a_new_publish_cursor` 由 RED 转 GREEN |
| I6 | 该集成文件**从未跑过**，露出三处夹具/断言与实现不符：`a_run` 名为「A live Run」却从不 `start`（`claim` 按 `ACTIVE_RUNS` 正确拒绝）；用例里 `ToolResult`/`Evidence` 的插入缺 `session.begin()` 被回滚；两处断言忘了 `prune_events` 拒绝 `keep_from > committed`（最新的那条事件必然存活）且 prune 自己会追加标记 | 三处都按实现的**实际语义**改正（`a_run` 增加可选 `start`，默认不 start，因为 `start` 会追加 `run_queued` 事件而事件顺序检查要求自己写的事件从游标 1 开始），未放宽断言强度 |
| I7 | `HUNTWEAVE_EVENT_RETENTION_KEEP` 在 README 与 `deploy/compose.yaml` 里都没有，部署无法设置该策略 | README 配置表新增一行并说明它与 `HUNTWEAVE_RETENTION_*` 是两件事；compose 的 app 环境补上透传（默认 `0` = 不清理） |
| I8 | 新增一条纯检查守卫「被静默架空」这一失败模式：读 `create_app(...).openapi()`，断言四条时间线路由都在、且 `/event-history` 的 200 响应契约是 `EventHistoryView`（旧内联实现答的是 `EventPage`）。**这条守卫不能查 `app.routes`**：FastAPI 把被 include 的 router 表示为一个 `_IncludedRouter` 条目，其路由不会摊平到父列表里（本轮先按错误假设写，实测 `GET /x -> 200` 才确认） | 变异验证：在 include **之前**插一条同路径的内联处理器，该检查 RED |

## 4. 迁移

新增 **`0011_event_retention`**，`down_revision = "0010_service_facts"`；只给 `event_cursors` 加一列 `retained_from_cursor INTEGER NOT NULL DEFAULT 0`。在一次性库上真实升级并回读：

```
升级前 alembic_version: 0010_service_facts
{"event":"migrations_complete"}
升级后 alembic_version: 0011_event_retention
```

链仍线性、单一 head（`tests/test_phase0_contracts.py::test_the_migration_chain_is_linear_and_has_exactly_one_head` 通过）。

## 5. 检查清单与命令输出

```
$ cd <worktree>/backend
$ python -m pytest -m "not integration" -q -p no:cacheprovider
393 passed, 16 skipped, 91 deselected
# 16 项跳过 = 15 项「需要一次性 PostgreSQL 才能求值」+ 1 项「本检出没有控制台构建产物」
$ $env:HUNTWEAVE_DISPOSABLE_TEST_DATABASE = "1"; python -m pytest -m "not integration" -q
408 passed, 1 skipped, 91 deselected

$ python -m ruff check src tests
All checks passed!
$ python -m mypy --config-file pyproject.toml src
Success: no issues found in 58 source files

$ python -m pytest tests/test_event_retention_integration.py -q
8 passed

$ python -m pytest -m "integration" --ignore=tests/test_startup_integration.py -q
86 passed, 409 deselected
```

**未跑的项**：容器内检查（`deploy/compose.verify.yaml` 的 checks 容器）、靶场探针、浏览器验收（前端接入归 F2/#65；`frontend/src/RunConsole.vue` 仍消费旧的 `{events, next_cursor, gap}` 形状，`EventHistoryView` 比旧契约宽，旧客户端容忍）。

## 6. Standards 轴评审与按评审的修订

Standards 轴由**独立会话**执行。发现与处置：

| # | 发现 | 判定 | 处置 |
| --- | --- | --- | --- |
| H1 | `page_events` 先读游标计数、再从行里走出连续区间，两条 READ COMMITTED 语句之间提交的写入会让区间末端**超过**先前读到的计数，于是同一个响应里出现 `published > committed`，与契约「published 不会领先于 committed」矛盾 | 必修（契约矛盾） | 返回前把计数抬到区间末端（计数恒在每条已提交行之上），并写明为什么是抬而不是重读 |
| H2 | SSE 把**每一次**探测失败都发成 `session_expired`：数据库故障与身份被拒在客户端看起来一样，一次数据库抖动把操作员登出；而同页中段的同类失败发的是 `storage_unavailable` | 必修（两个线名一个条件） | 按原因分流：`storage_unavailable` 走 `storage_unavailable`，其余走 `session_expired`，与中段处理器一致 |
| H3 | `prune_events` 在**已持有游标行锁**的事务里做批量 `DELETE … RETURNING`，锁持续时间是时间线规模的函数，与 PROJECT §12.1 要求的「短事务」不符，并阻塞该 Run 的写者 | 判断项，**未改** | 改动等于重排清理的两段式（先删后取锁推进），会扩大本切片范围并改变清理的原子性语义；登记在「未达成与限制」，建议随维护票处理 |
| H4 | `prune_events` 的 docstring 说清理标记「即使之后的游标全部老化也仍在」，而它就是本时间线的一条普通事件、下一次清理会删掉它 | 必修（不实陈述） | 改为陈述这一点：持久记录是 `retained_from_cursor` 列，标记是时间线上可读的那份 |
| H5 | `contracts/orchestration.py` 的注释「P0 keeps every committed event, so a cursor can only be ahead of the Run, never lost」被本切片推翻且未同步 | 必修（不实陈述） | 改为说明该注释在保留策略落地后失效，并指向 `EventHistoryView` |
| H6 | `HUNTWEAVE_EVENT_RETENTION_KEEP` 未文档化（**修复已在工作区但未提交**）；新增原因码本身是干净的（`test_reason_codes.py` 双向收录、`workspace.ts:156` 有文案） | 必修（可运维性） | 见 I7，已随本记录提交 |
| H7 | `EventWindow.gap` / `resync_required` / `reload_reason` **没有任何生产者**（构造 `EventWindow` 时从不赋值），因此 `orchestration.history` 的 docstring 声称「reports a real `gap` instead of a hard-coded `False`」是假的；`ReloadReason` 的四个字面量与 `EventStreamFailureView.revocation_limit_seconds` 同样没有生产者 | 必修（不实陈述）＋判断项（死字段） | docstring 改为陈述真相（该形状**无法**在成功响应里表达缺口，`gap` 恒为假，三位置只在 `EventHistoryView` 里）；**未**删字段（属契约面，改动会波及前端与夹具），登记在限制里 |
| H8 | `test_a_reload_reason_in_a_fixture_is_never_invented` 是空检查（没有夹具设置 `reload_reason`）；`frontend_messages()` 重复实现了 `test_reason_codes.py` 的 `frontend_keys` | 判断项，**未改** | 登记为后续维护项 |
| J1–J5 | `event_stream_opening` 取 500 条只为读位置又丢掉（`page_events` 一函数两职）；`_contiguous_published` 的 `limit+1` 前瞻行从未被检查（注释说错了）；清理在「读水位」与「走区间」之间提交会把 `expired` 报成 `ahead`（拒绝正确、码不对）；流丢弃刚取到页的全部坐标；`stream_probe` 每次轮询自建 `AccessService`（无状态、无资源开销，但是中间件授权契约的第二份拷贝） | 判断项，**未改** | 全部登记在限制里 |
| — | 评审确认**成立**的两点 | — | ① `prune_events` 追加清理标记是 AGENTS.md「所有动作进入真实事件时间线」要求的，且删除只限于 `audit_events`，不构成对证据 append-only 的违反；② 页路径**不加锁**是对的（竞争只会拒绝而不是静默截断），除 H1 与 J3 两点外；③ `api/app.py` 的重复处理器确实违反了「共享面单一所有者」，集成人的删除＋OpenAPI 守卫是正确的修法 |

### 变异验证

在 include **之前**插入一条同路径的内联处理器（复现被静默架空的失败模式）：

| 变异 | 把什么写错 | 目标检查 | 结果 |
| --- | --- | --- | --- |
| S1 | 在 router 之前注册一条 `/api/v1/runs/{run_id}/event-history` 内联处理器 | `test_the_packaged_app_serves_the_new_timeline_contract_and_not_the_old_one` | RED `1 failed in 0.28s` |

I5（`created_at` 覆写）与 I6（三处夹具/断言）的变异证据是**自带的**：修复前分别是 1 项失败与 8 项失败，修复后转绿。

## 6b. Spec 轴评审（独立会话）与阻塞项

Spec 轴同样由**独立会话**执行，并在一次性库上复现了阻塞项。发现与处置：

| # | 发现 | 判定 | 处置 |
| --- | --- | --- | --- |
| P1 | **`published` 是「本页相对」而不是「全新读者能继续到哪」。** `_contiguous_published` 走到 `limit` 条就停，而 `event_stream_opening` 用 500 调它；真库实测 600 条事件时开帧给出 `committed=600, published=500, resync_required=False`，与 `contracts/event_stream.py` 自己的注释「`published < committed` 意味着有写在飞」直接矛盾，也使 `published` 不能回答「还能继续到哪」 | **必修（实现与已写明的契约矛盾）** | **已修**：把两件事拆开——`cursors` 仍是**本页**（按 `limit` 有界、遇洞即停），`published` 由新的 `_contiguous_end` 单独结算。常见情形用两个索引聚合判定（`floor` 以上的游标就是 `start+1 .. highest` 这一串整数，所以行数等于宽度即无洞），只有真的存在洞时才退回逐行走到第一个洞。于是页满不再被当成「时间线到此为止」，代价也仍有界 |
| P2 | **「写在飞」这一状态在读者侧根本不可观测**：`committed_cursor` 在读者自己的快照里读游标行，未提交的推进对它不可见。集成检查证实：写者事务开着时读者看到的是 `committed == 2` 而不是 3。因此夹具 `event_history_writer_in_flight.json`（`committed=43, published=42`）是**任何后端响应都产生不出来的状态**，所谓「读者等待」只是下一次 0.5 秒轮询 | 必修（夹具与说明不实） | **已修**：夹具改名为 `event_history_missing_position.json` 并换成**真能产生的状态**——`committed` 以下缺一个位置（部分恢复、手工重发游标），页面只给出能证明的连续段，于是 `published(2) < committed(44)`、`next_cursor` 是本页末端。同时更正 `contracts/event_stream.py` 的措辞：该不等式意味着**下面有一个位置不存在**，不是「有写在飞」；本页的续读点由 `next_cursor` 表达，页满时 `next_cursor < published` 是正常的。测试里那条「页满即 published=页尾」的断言（它把缺陷锁住了）也一并改成区分两个量 |
| P3 | **409 不携带水位**：`page_events` 抛 `event_cursor_expired` 时没有 payload，`error_response` 只输出 `{"reason_code": …}`；而流内帧**是**带 `retained_from` 的。验收 2 与 PROJECT §12.1 要「明确缺口」，夹具 `event_cursor_expired.json` 自己也承诺 `recovery.retained_from:120` | 必修（验收 2 未达成的部分） | **未改**：加水位会让 `tests/test_event_retention_and_resync.py` 里断死裸响应体的那条检查变红 —— 也就是说**这条检查把缺陷锁住了**。改它必须先决定 409 的响应形状，属契约面 |
| P3 | **409 不携带水位**：`page_events` 抛 `event_cursor_expired` 时没有 payload，`error_response` 只输出 `{"reason_code": …}`；而流内帧**是**带 `retained_from` 的。验收 2 与 PROJECT §12.1 要「明确缺口」，夹具 `event_cursor_expired.json` 自己也承诺 `recovery.retained_from:120` | 必修（验收 2 未达成的部分） | **未改**：加水位会让 `tests/test_event_retention_and_resync.py` 里断死裸响应体的那条检查变红 —— 也就是说**这条检查把缺陷锁住了**。改它必须先决定 409 的响应形状，属契约面 |
| P4 | **「固定响应夹具供图与控制台接入」只是声明**：8 个夹具全是手写，检查只验证它们能装进各自的模型，**没有任何检查拿真实响应与夹具比对**，夹具可以静默漂移 | 必修（验收 5 未达成的部分） | **未改**：需要一条「真实响应 vs 夹具」的比对检查，属新增工作 |
| P5 | **没有快照身份**：`EventHistoryView` 只有常量 `contract_version`，没有能标识「这一页属于哪次快照」的字段，因此验收 3 的「跨快照拒绝合并」在现有契约下**不可实现**；keyset 历史与证据 offset 的处理被显式推迟到别的切片 | 必修（验收 3 未达成的部分） | **未改**，需契约扩展 |
| P6 | **迟到事件的发生时间只接到了自己**：除签名与一条集成检查外，生产路径没有任何地方传 `occurred_at`，因此 PROJECT §12.1 的「迟到事件保留原发生时间」在真实到达路径上未被行使；视图也不暴露 `occurred_at` 字段，客户端只能读 `payload.occurred_at` | 判断项 | 记录：本轮修好 `created_at` 覆写后，「到达时间与发生时间分开」这件事在**数据层**是对的；「真实到达路径」要等有真实的迟到生产者（执行端重连/补报）才有意义 |
| P7 | **范围超出票据**：`GET/POST /event-retention[/prune]` 与 `HUNTWEAVE_EVENT_RETENTION_KEEP` 是票据之外的新面；且保留**只能由操作员手动触发**（没有后台清理），因此「过期游标」这一状态不手动调用就产生不了 | 范围观察 | 记录，不撤回（票据正文没有禁止，且水位与清理是验收 2 的必要载体）；但「没有自动清理 ⇒ `expired` 状态罕见」这一点应让切片所有者知晓 |
| — | 评审确认**成立**：清理只碰 `audit_events` 与水位、并追加 `event_retention_applied` 标记（已逐行核对）；P1 的 SSE 帧形状、5 秒心跳与 60 秒内的会话撤销界；`event_cursor_expired` 是唯一新增原因码且双向收录 | — | — |

## 7. 逐条验收结论

| # | 验收标准（Issue #45） | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 按 Run 提交顺序发布，Runner 重发去重、迟到事件新游标保留原发生时间，不越过未提交事件 | **通过** | `append_event` 先取游标行锁再查来源（#21 的前置条件），重发同一 `source_event_id` 幂等；`test_a_redelivered_runner_event_stays_one_event_and_keeps_the_run_order`、`test_two_writers_cannot_invert_the_commit_order_of_one_run`、`test_an_uncommitted_event_is_never_published_to_a_reader`；迟到事件见 `test_a_late_event_keeps_its_own_time_and_takes_a_new_publish_cursor`（本轮修好 `created_at` 覆写后才成立） |
| 2 | 保留策略清理事件前不破坏业务记录/原始证据权威；过期 Last-Event-ID 明确缺口与重载要求，不假装完整补拉 | **通过** | `test_the_prune_leaves_every_business_record_and_the_evidence_index_standing`（逐行计数前后相等）；`prune_events` 只删 `audit_events`；`test_an_expired_cursor_cannot_be_replayed_and_the_snapshot_cursor_can` 覆盖 `event_cursor_expired` 与「从快照游标继续」这条唯一打开的恢复路径；`test_pruning_the_whole_timeline_makes_the_only_readable_position_the_watermark` 覆盖退化情形 |
| 3 | 提供带版本/水位的状态快照与连续增量契约，重复/乱序/跨快照拒绝合并；历史列表 keyset 与证据字节 offset 分别处理 | **部分（未达成）** | 三位置 + `EVENT_STREAM_CONTRACT_VERSION` 与连续区间（走到第一个洞就停）已实现并经 `test_an_uncommitted_event_…`、`test_the_committed_order_is_what_a_reader_continues_from` 等覆盖；历史列表用游标区间、证据字节用 offset 是既有分离，本切片未改。**未达成**：①`published` 按页界截断，不是「全新读者能继续到哪」（P1，真库实测 `committed=600, published=500`）；②契约里**没有快照身份**，跨快照拒绝合并在现有形状下不可实现（P5）；③没有任何检查构造「旧快照游标 + 新增量」的合并尝试并断言被拒 |
| 4 | 读取与流持续鉴权，断线/到期/撤销原因明确；关键缓冲或归档耗尽阻断受影响执行，不能静默丢事件 | **通过（本切片范围内）** | `stream_probe` 每 15 秒重读会话，缺 cookie 与失效会话分别给出不同原因码；`test_the_stream_refuses_a_revoked_session_and_says_why` 等覆盖；本轮修好「数据库故障被报成 `session_expired`」（H2）。**归档/缓冲耗尽**属执行端与 #19 的范围，本切片不改 |
| 5 | 接口固定响应夹具供图与控制台接入，保留原 P1 顺序和心跳语义 | **部分** | `backend/tests/data/events/` 8 个夹具存在，P1 帧形状与心跳由检查固定（评审判定成立）；**未达成**：①夹具全是手写，**没有检查拿真实响应与夹具比对**，可以静默漂移（P4）；②`event_history_writer_in_flight.json` 描述的状态**任何后端响应都产生不出来**（P2）；③前端接入归 F2/#65，**未做** |

## 8. 共享面影响

- **`api/app.py`**：删除 2 条内联路由（时间线）并挂载 1 个 router 工厂；导入清理 6 项。**这是本切片最容易出事的共享面改动**，已由 OpenAPI 契约守卫钉住。
- **`contracts/event_stream.py`**：新增（本切片独占）。
- **`contracts/orchestration.py`**：只改一条**注释**（H5），不改契约。
- **`storage/database.py`**：`BUSINESS_REVISIONS` 线性四项。
- **`config.py`、`deploy/compose.yaml`、`README.md`**：新增一个部署变量（I7）。
- **原因码**：新增 `event_cursor_expired`。`contracts/errors.py` 在本仓库没有原因码常量，权威载体是源码用法 + `tests/test_reason_codes.py` + `frontend/src/workspace.ts`；`test_reason_codes.py` **3 passed / 1 skipped**（跳过项是本检出没有控制台构建产物），双向收录成立。
- **未改**：`PROJECT.md`、`docs/STATUS.md`、`docs/adr/**`、`docs/specs/**`、`frontend/**` 的渲染逻辑。

## 9. 未达成与限制

**本轮修复的阻塞项（Spec 轴发现）：**

1. **`published` 的语义**（P1，**已修**）：把「本页」与「时间线的可达末端」拆开——`cursors` 仍按 `limit` 有界、遇洞即停，`published` 改由 `_contiguous_end` 结算（两个索引聚合的快路径；只有真存在洞时才退回逐行走到第一个洞，因此代价仍有界）。新守卫 `test_published_is_the_timelines_reach_and_not_the_pages_end`：600 条事件读 500 条时断言 `committed=600, published=600`，读 1 条时 `next_cursor=1` 而 `published=600`。**变异验证**：去掉修复即 RED。
2. **「写在飞」不可观测**（P2，**已修**）：夹具换成真能产生的「`committed` 以下缺一个位置」状态，契约措辞同步更正为「该不等式意味着下面有一个位置不存在」，页满时 `next_cursor < published` 属正常。

**仍存在的限制：**
3. **409 不携带水位**（P3），且现有检查把裸响应体断死，锁住了这个缺陷。
4. **没有快照身份**（P5），「跨快照拒绝合并」不可实现。
5. **夹具没有任何漂移检查**（P4）：8 个夹具全是手写，只验证「能装进模型」。

**其余限制：**

6. **验收 3 的「跨快照拒绝合并」没有专门检查**：`retained_from` 与拒绝码把它表达出来了，但没有一条检查构造「旧快照游标 + 新增量」的合并尝试并断言被拒。
7. **`prune_events` 的锁持续时间是时间线规模的函数**（H3）：批量 `DELETE … RETURNING` 在持有游标行锁的事务里执行，与「短事务」要求不符，并阻塞该 Run 的写者。
8. **三个字段没有生产者**（H7）：`EventWindow.gap`/`resync_required`/`reload_reason`、`ReloadReason` 的四个字面量、`EventStreamFailureView.revocation_limit_seconds`。它们在契约里存在但没有任何代码赋值，属**推测性通用**；本切片只更正了声称它们生效的 docstring，未删字段。P3 的「409 不带水位」与这一条相关：流内帧带了，`resync_required` 那条路却没有。
9. **`test_a_reload_reason_in_a_fixture_is_never_invented` 是空检查**（H8），且 `frontend_messages()` 与 `test_reason_codes.py` 的 `frontend_keys` 重复实现同一件事。
10. **J1–J5**（第 6 节）全部未改：`event_stream_opening` 一函数两职并丢弃 500 条完整事件；`_contiguous_published` 的 `limit+1` 前瞻行是死读且注释说错；清理在「读水位」与「走区间」之间提交会把 `expired` 报成 `ahead`（拒绝正确、原因码不对）；流丢弃刚取到页的坐标；`stream_probe` 每次轮询自建 `AccessService`。
11. **迟到事件的发生时间在生产路径上未被行使**（P6）：除签名与一条集成检查外无人传 `occurred_at`。
12. **保留只能手动触发**（P7）：没有后台清理，`event_cursor_expired` 这一状态除手动调用外产生不了。
13. **未跑容器内检查、靶场探针与浏览器验收**；前端仍消费旧的 `{events, next_cursor, gap}` 形状。
14. **`docs/STATUS.md` 与 README 的项目资料索引由集成人在合入时回接**；本记录不宣布阶段状态。
15. **并发跑同一一次性库会互相清空夹具**：本轮出现过一次 8 项假失败（两个独立评审会话与我同时跑同一套检查）。这不是产品缺陷，但**同一一次性库不能被两个检查进程共享**，应写进下一份交接。

## 10. 待人工确认

**`published` 的取舍已定，记录在这里而不是留给下一个会话：**

0. **`published` 采用「走满连续区间」这条语义**（P1，已按此实现）。理由：契约本来就写明 `published` 是「a *fresh* read can actually continue through」，所以从页界截断是**实现与契约不符**，不是契约需要放宽；而②「改契约措辞」会把一个已经正确的读法改坏，并为客户端增加一个字段。代价由 `_contiguous_end` 的两条索引聚合挡在常路径之外。**若维护者更愿意采用②，需要同时改 `contracts/event_stream.py` 的三位置说明、`EventHistoryView`、`runs/events.py` 与本节——但那等于放弃一个已经写明的语义。**

以下各项仍待确认：

2. **409 的响应形状**（P3）：是否让 `event_cursor_expired` 带上 `retained_from`（流内帧已经带了，夹具也承诺了）。这会让 `tests/test_event_retention_and_resync.py` 里断死裸响应体的那条检查变红，需要一并改。
3. **是否补「真实响应 vs 夹具」的比对检查**（P4），以及是否给 `EventHistoryView` 加快照身份（P5）。两者都是验收 3/5 的剩余项。
4. **H3 与 J1–J5 的归属**：建议开一张维护票收拢（清理的两段式、`page_events` 的职责拆分、前瞻死读、清理与读取的竞态原因码、`stream_probe` 复用 `AccessService`）。
5. **H7 的死字段是否删除**：删 `EventWindow.gap`/`resync_required`/`reload_reason` 与 `ReloadReason` 会触及契约面与前端，需与 F2（#65）一起定；本切片只更正了不实陈述。
6. **P7 的范围**：`/event-retention[/prune]` 与 `HUNTWEAVE_EVENT_RETENTION_KEEP` 超出票据正文；保留只有手动触发。确认它们是留在本切片、还是另开票。
7. **迁移顺序**：本切片已改为 `down_revision = "0010_service_facts"`，链尾 `0011_event_retention`；合入后应确认单一 head。

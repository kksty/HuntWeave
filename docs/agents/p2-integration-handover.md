# P2 多切片并行的集成交接（2026-10-11）

本文件是**一次协调会话的交接记录**，供下一个会话接续。它只记「当前进行到哪、还差什么、按什么顺序合」；阶段、能力与下一实施项仍只在 [STATUS](../STATUS.md) 维护，验收要求见 [0003 §8–9](../specs/0003-agent-research.md) 与 [PROJECT §14](../../PROJECT.md)，分批顺序见 [P2 切片分批执行计划](./p2-execution-batches.md)。

## 1. 已合入并关闭（`main` 上已有）

| 票 | 合入点 | 交付 | 验证记录 |
| --- | --- | --- | --- |
| [#37](https://github.com/kksty/HuntWeave/issues/37) 契约对齐 | `1e5b0eb` | [ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md)、`PROJECT.md` 0.9.16、四组矛盾口径更正 | [0020](../validation/0020-contract-alignment.md) |
| [#33](https://github.com/kksty/HuntWeave/issues/33) V-A token 层与字体自托管 | `3ca3684` | `frontend/src/tokens.css`（唯一色值来源）、五个 OFL 字族自托管、`npm run check:design`（16 项，**已接入 CI**） | [0021](../validation/0021-visual-tokens.md) |
| [#38](https://github.com/kksty/HuntWeave/issues/38) P2 Phase 0 | `c8033c8` | [0010 来源盘点与契约交接](../specs/0010-phase0-source-inventory.md)、`contracts/phase0.py`、8 个必失败样例 | [0023](../validation/0023-phase0-source-inventory.md) |
| [#21](https://github.com/kksty/HuntWeave/issues/21) 多活跃 Run 并发压力验收 | `42cf892` | `contracts/resources.py` 版本化资源政策、同 IP 串行、额度耗尽背压、`claim()` 不再白占轮次 | [0022](../validation/0022-concurrency-quota.md) |

`main` 当前 head：`8bfe9e8`（`docs/STATUS.md` 同步：`#21` 已关闭、**P1 切片全部交付、阶段未退出**）。

## 2. 进行中（各有独立 worktree + 分支，**均未 push**）

分支命名 `codex/<编号>-<slug>`，worktree 在 `D:\code\huntweave-wt\<编号>-<slug>`。

**三支的工作都还在工作区里、尚未提交完成**（协调会话按用户要求停止时中断了它们）。接手的会话应把它们当作「**未完成的实现**」来处理：先看代码与报告，跑三条检查，必要时补完，再走评审与合入。

| 票 | 分支 / worktree | 状态 |
| --- | --- | --- |
| [#43](https://github.com/kksty/HuntWeave/issues/43) P2-A1 事务外模型请求 | `codex/43-p2-a1-model-outside-transactions` | **被中断的 rebase 残留**：detached HEAD、`orchestration.py` 有未合并索引项；3 个文件已 stage、其余已改。见下 |
| [#44](https://github.com/kksty/HuntWeave/issues/44) P2-B1 服务事实与血缘 | `codex/44-p2-b1-facts-and-lineage` | 实现与守卫修改都在工作区（`contracts/facts.py`、`runs/facts.py`、`api/facts.py`、`0010_service_facts.py`、两个检查文件均为**未跟踪新增**）；待提交 |
| [#45](https://github.com/kksty/HuntWeave/issues/45) P2-E2 事件保留与补拉 | `codex/45-p2-e2-event-retention-resync` | 同上（`contracts/event_stream.py`、`runs/event_retention.py`、`api/events.py`、`0011_event_retention.py`、固定夹具目录、两个检查文件未跟踪新增）；并已改 `frontend/src/workspace.ts` 加 `event_cursor_expired` 文案 |

### `#43` 的 worktree 处于**未完成的 rebase**（接手时先处理这一处）

`git -C <43 worktree> status` 显示：`HEAD` 处于 **detached**、`backend/src/huntweave/runs/orchestration.py` 仍有 **未合并索引项**（`ls-files -u` 有 stage 1/2/3），另有两处已 stage、其余 6 个文件已修改/新增。**rebase 目录已不在**，所以它是一个**被中断的 rebase 残留**，不是可继续的 rebase 状态。

- **不要** `git rebase --abort` / `--skip` / `reset --hard` 就当作重来——那会丢掉已经解决的部分与三处已 stage 的改动。
- 建议接手方式：先在 `orchestration.py` 里手工消除冲突标记并保留双方语义（见第 4 节），跑绿三条检查后再决定是 `git rebase --continue` 还是直接以 `HEAD` 重建提交。
- **原始 6 个提交在 reflog 里仍可见**（`654da66` / `b81e9e5` / `07f503b` / `3c891c4` / `5df2a64` / `763aca1`，基线 `c8033c8`）；若决定重做，这是可靠的恢复点。

**注意**：worktree 的 `origin/main` ref 可能过期。**判断某分支的真实增量要用它的 merge-base**，不要用 `git diff origin/main..HEAD`（那会把别人已合入的提交显示成「被本分支删除」）。

## 3. 合入顺序与迁移链（重要）

三支各自新增了一条迁移，**都从 `0008_retention_decisions` 起步**，因此合入时必须**线性化**：

| 顺序 | 票 | 迁移 | 合入时要做的 |
| --- | --- | --- | --- |
| 1 | #43 | `0009_planning_attempt_identity` | `down_revision` 已是 `0008_retention_decisions`，**不用改** |
| 2 | #44 | `0010_service_facts` | 把 `down_revision` 改为 `0009_planning_attempt_identity` |
| 3 | #45 | `0011_event_retention` | 把 `down_revision` 改为 `0010_service_facts` |

每步都要同步 `backend/src/huntweave/storage/database.py` 的 `BUSINESS_REVISIONS` 元组顺序，并保持**单一 head**。`tests/test_the_migration_chain_is_linear_and_has_exactly_one_head` 会抓分叉。

## 4. #43 合成的关键点（最需要复核的一处）

`#43` **删除了旧的整段 `plan()`**，拆成 `begin_planning → ModelAdapter.decide → commit_planning`（模型调用**无事务无锁**）；而 `#21` 此前把**物理额度与背压**逻辑插进了那个旧 `plan()`。因此 rebase 的 6 处冲突不是二选一，而是**把 #21 的额度语义移植进 #43 的新结构**。复核时必须确认这些语义都还在：

- 额度检查与「锁定 + 计数 + 写 `ToolCall`」在**同一个短事务**内，**锁先于计数**；
- 被背压时写去重的 `execution_backpressure` 事件、`return None`，**不留下 ToolCall / BudgetReservation / outbox 行**；
- **受控重派也受同一额度门约束**；
- 「预算不超限」语义在新结构里仍有承载（`_reserved` 一带）；
- #21 的「decision 已提交但其 call 不存在（当时被额度拒绝，或进程在两步之间停止）」这个**可续跑**语义没有丢——丢了会把 Run 卡死。

`tests/test_execution_quota.py`（#21）与 #43 的身份/协议检查同时通过，是这次合成的真正验收。

## 5. 共享面所有权（本轮的分配，后续批沿用）

- `api/app.py` 的**路由装配归集成人**：实现会话在 `api/` 下写导出 router 工厂，把要加的 `include_router` 那一行写进报告，**不改 `app.py`**。
  - **待加**（#44 已提供）：`from huntweave.api.facts import create_facts_router` + `app.include_router(create_facts_router(database))`。
- `contracts/errors.py` 的原因码是共享面：新增必须在同一提交同步 `tests/test_reason_codes.py` 与 `frontend/src/workspace.ts` 的 `messages` 映射。#44/#43 已声明**零新增原因码**。
  - **待办（#45 已新增一个，必须补齐三处）**：`#45` 的工作区里 `frontend/src/workspace.ts` 已加 `event_cursor_expired` 的文案，因此**必须**确认 `contracts/errors.py` 的权威载体与 `tests/test_reason_codes.py` 同提交收录该码；`#45` 交付时须逐条列出。这是本轮唯一新增的原因码。
- `docs/specs/0010-phase0-source-inventory.md` 与 `contracts/phase0.py` 的事实维护：`#44` 新建 11 张表（`hosts`/`services`/`web_endpoints`/`observations`/`address_clues`/`research_lineage_references`/`service_bindings`/`service_coverage`/`service_navigation`/`service_cost_shares`/`service_settlements`），并会把 `Host`/`Service`/`WebEndpoint` 从 `LEGACY_OBJECTS_ABSENT` 移入 `LEGACY_OBJECTS_PRESENT`。**集成人要**把表清单补进 0010 §3.3 并修订「确实不存在」的表述。
- `#38` 的表清单守卫由 `#44` 放宽为「`documented ⊆ expected` 保留；`expected - documented` 只允许**基线之后且被 `BUSINESS_REVISIONS` 收录**的迁移所建的表」——即按 revision 顺序判定，不用文件名前缀。

## 6. 交接时确立、后续应继续沿用的做法

1. **每个切片两轴评审**（Standards + Spec）由**独立会话**做；实现会话若无法委派（`maxDepth=1`），其「两轴评审」是自评审，**必须另派独立复核**。#38 的独立复核查出两处自评审漏掉的**契约事实错误**（冻结输入 49 而非 43、样例用编造的门槛原因码），两处都是「检查存在但守不住结论 ⇒ 恒绿」。
2. **新检查要做变异验证**：把结论写错一次，确认它变红，再还原。没有这一步就不知道检查是否真在守结论。
3. **记录可复现性**：验证记录里的计数、hash、行号写**实际值**；引用历史时必须用真实路径，不要猜锚点（本轮踩过 `#81-实施前-phase-0` 与 `§6.2` 两个坏引用）。
4. **不要用 shell 做中文文本的字符串手术**。本轮两次事故都出在这里：PowerShell 的 `Set-Content` 把 UTF-8 写成 ANSI 损坏了 `contracts/resources.py`；我的冲突解决脚本两次**静默丢掉** `docs/validation/README.md` 的索引行、一次把整表写空。**改用 Python 脚本按字节读写，并让脚本在「期望的行不存在」时拒绝写入**。
5. **共享 Docker 验收环境**：常驻项目 `huntweave` 可能正被用户使用。只允许对其**只读**查询；需要写操作时用**一次性独立项目名 + 独立非默认回环端口**，用完只清理自己创建的项目。`deploy/compose.isolated-db.yaml` 的端口现为必填变量（缺变量即失败），这是有意的。

## 7. 未决与待用户决定

- **CIDR 是否纳入范围**（`0008 §6` 的 5 项待确认，[ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md) 记为唯一未决项）：未决定前保留 IP-only 与 `TARGET_LIMIT = 100`，C-F（[#30](https://github.com/kksty/HuntWeave/issues/30)）不含 CIDR 展开。**只阻塞 C-F**。
- **P1 阶段与里程碑**：P1 的切片已全部交付，规格第 5 节 tracer 顺序走完，但**真实执行仍未开放**——[ADR-0010](../adr/0010-real-execution-boundary-and-gate.md) 四项门槛尚缺 `profile_revalidation`（`profile_unvalidated`）与 `deployment_revert`（`revert_path_missing`），产品继续拒绝创建真实 Run。门槛补齐归 [#39](https://github.com/kksty/HuntWeave/issues/39)。**阶段退出与里程碑关闭是维护者决定**，本文件不代为宣布。

## 8. 后续可执行前沿（按依赖，不必等批次同步）

`#43`/`#44`/`#45` 合入后：`#46`/`#47`（维护收尾，等 `#40`–`#42`）、`#49`–`#52`（第二层）、以及关键路径上的 `#39`→`#59`→…。可选票 `#48`（Linux 宿主复验）在未被领取前不进入任何一批的可执行前沿；`#32` 是 **P1 期的实测票**（三个热点：`agentd` 顺序推进、`ledger._persist()` 全量序列化、事件游标行锁），与 `#21` **串行使用验收环境**、口径不同（`#32` 测单进程热点与成本曲线并判断「调参数还是改结构」；`#21` 已做完并发正确性与校准值，且**没有**替 `#32` 下结论）。

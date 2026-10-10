# P2 切片分批执行计划

本文件登记 P2 实施切片的**可执行前沿与分批顺序**，供多分支并行开发使用。它不陈述阶段与本文件之外的状态：阶段、能力与下一实施项仍只在 [STATUS](../STATUS.md) 维护，验收要求见 [0003 §8–9](../specs/0003-agent-research.md) 与 [PROJECT §14](../../PROJECT.md)。

- 票据台账、正文来源与依赖边由**已发布的 Issue** 持有：P2 Phase 0 与 A–F 切片为 [#37](https://github.com/kksty/HuntWeave/issues/37)–[#76](https://github.com/kksty/HuntWeave/issues/76)，覆盖面 C-A…C-F 与视觉 V-A/V-B/V-D 为 [#25](https://github.com/kksty/HuntWeave/issues/25)–[#35](https://github.com/kksty/HuntWeave/issues/35)。
- 阻塞关系使用 GitHub 原生 issue dependencies；本文件只登记**顺序与批次**，不复述每条边的理由。
- 盘点基线：2026-10-11，`main` `4f0d063`。依赖图为无环图（已核验：无悬挂引用、无自环、无重复键、无环）。

## 1. 什么是「可执行前沿」

按 [issue-tracker](./issue-tracker.md)，可执行前沿是**所有阻塞项已关闭、尚未被领取**的开放 Issue。本计划据此分三档，避免把「引用」误当成「门」：

| 档 | 含义 | 领取条件 |
| --- | --- | --- |
| 硬门（hard） | 本票要写的接口/模型/迁移由该前置票产出，未合入就无法开工 | 前置已合入 `main`（不是「已关闭」） |
| 部分门（partial） | 前置只挡本票**部分**验收项，其余项可先做 | 先做未受阻项，受阻项等前置合入后复验 |
| 软引用（soft） | 只是同一主题的后续工作或来源引用，不挡开工 | 随时可领取 |

计划与 GitHub 原生链接的关系：**原生链接是记录，批次是执行顺序。** 当前已发布的 `blocked_by` 边把 A/E/F 若干票指向了开放中的 [#21](https://github.com/kksty/HuntWeave/issues/21) 与 [#32](https://github.com/kksty/HuntWeave/issues/32)（P1 期的验收票，见第 5 节），这些边按第 4 节的口径处理。

## 2. 批次顺序

批次按依赖波次生成：同一批次内的票**互不阻塞**，可同时开工；每批验收合入 `main` 后，下一批的可执行前沿才解锁（[issue-tracker](./issue-tracker.md)：代码先到 `origin/main` 再关闭引用它的 Issue）。

两个附带说明：批号是**建议顺序，不是同步屏障**——批次只是把共享面的使用者分开，票一旦其阻塞项都合入 `main` 就可领取，不必等同批其他票。#37 与 #39–#42 同处波次 0，但 #37 改写 `PROJECT.md` 与规格（共享面），所以先走 B1 并合入，B2 再开。

可选票（[#48](https://github.com/kksty/HuntWeave/issues/48)）的阻塞项来自 `#39`，即使进入可执行前沿也**必须等专用 Linux 宿主与产品决定**，不得因为「前沿里有它」就开工。

| 批 | 票 | 说明 |
| --- | --- | --- |
| B1 | [#37](https://github.com/kksty/HuntWeave/issues/37) | 契约对齐。独占 `PROJECT.md` 与规格文字，必须单独一批 |
| B2 | [#39](https://github.com/kksty/HuntWeave/issues/39) [#40](https://github.com/kksty/HuntWeave/issues/40) [#41](https://github.com/kksty/HuntWeave/issues/41) [#42](https://github.com/kksty/HuntWeave/issues/42) | 真实执行门槛与三条维护/诊断票，与 P2 主线无耦合 |
| B3 | [#38](https://github.com/kksty/HuntWeave/issues/38) [#46](https://github.com/kksty/HuntWeave/issues/46) [#47](https://github.com/kksty/HuntWeave/issues/47) | Phase 0 契约盘点 + 维护收尾。`#38` 是全 P2 的共享契约底座 |
| B4 | [#43](https://github.com/kksty/HuntWeave/issues/43) [#44](https://github.com/kksty/HuntWeave/issues/44) [#45](https://github.com/kksty/HuntWeave/issues/45) | A1/B1/E2：P2 第一个并行三角（编排/事实/事件） |
| B5 | [#49](https://github.com/kksty/HuntWeave/issues/49) [#50](https://github.com/kksty/HuntWeave/issues/50) [#51](https://github.com/kksty/HuntWeave/issues/51) [#52](https://github.com/kksty/HuntWeave/issues/52) | 第二层：多 Worker、Attempt/前提门、任务上下文、证据绑定 |
| B6 | [#53](https://github.com/kksty/HuntWeave/issues/53) [#54](https://github.com/kksty/HuntWeave/issues/54) [#55](https://github.com/kksty/HuntWeave/issues/55) [#56](https://github.com/kksty/HuntWeave/issues/56) [#57](https://github.com/kksty/HuntWeave/issues/57) | 规划、真模型、检索获取、判据、资源分池 |
| B7 | [#58](https://github.com/kksty/HuntWeave/issues/58) [#59](https://github.com/kksty/HuntWeave/issues/59) [#60](https://github.com/kksty/HuntWeave/issues/60) [#61](https://github.com/kksty/HuntWeave/issues/61) | 持久等待、洁净准备、独立复审、图投影（F 线起点） |
| B8 | [#62](https://github.com/kksty/HuntWeave/issues/62) [#63](https://github.com/kksty/HuntWeave/issues/63) [#64](https://github.com/kksty/HuntWeave/issues/64) [#65](https://github.com/kksty/HuntWeave/issues/65) | 会话工具、工具库发布、CVSS 准入、研究工作台（前端起点） |
| B9 | [#66](https://github.com/kksty/HuntWeave/issues/66) [#67](https://github.com/kksty/HuntWeave/issues/67) [#68](https://github.com/kksty/HuntWeave/issues/68) | 最小实证边界、证据有效性、兼容迁移 |
| B10 | [#70](https://github.com/kksty/HuntWeave/issues/70) [#71](https://github.com/kksty/HuntWeave/issues/71) | 报告冻结与复审队列 |
| B11 | [#72](https://github.com/kksty/HuntWeave/issues/72) [#73](https://github.com/kksty/HuntWeave/issues/73) [#74](https://github.com/kksty/HuntWeave/issues/74) | 备份恢复、容量长时验收、交付与工具界面 |
| B12 | [#75](https://github.com/kksty/HuntWeave/issues/75) | 发行交付 |
| B13 | [#76](https://github.com/kksty/HuntWeave/issues/76) | 总集成与发布合并 |

**可选票**（`optional: true`，不作为 P2 首版退出前置，有环境与证据后再领）：[#48](https://github.com/kksty/HuntWeave/issues/48)（Linux 宿主 profile 复验，需专用 Linux 宿主）、[#69](https://github.com/kksty/HuntWeave/issues/69)（按缺口实测选择预装扩容），以及既有的 [#30](https://github.com/kksty/HuntWeave/issues/30)/[#31](https://github.com/kksty/HuntWeave/issues/31)。`#48` 在未被领取前不进入任何一批的可执行前沿。

## 3. 并行工作线

同一批内按工作线分工作树；跨批重叠时，后一批从已含前批全部提交的 `main` 切出。

| 工作线 | 票 |
| --- | --- |
| 契约 | #37 #38 |
| 编排 | #43 #49 #50 #53 #58 |
| 事实/证据 | #44 #45 #52 #56 #60 #64 #66 #67 #68 |
| 模型 | #51 #54 #55 |
| 执行 | #39 #59 #62 #63 |
| 调度/验收 | #57 #73 |
| 投影/前端 | #61 #65 #71 #74 |
| 迁移/运维 | #70 #72 #75 |
| 维护/诊断 | #40 #41 #42 #46 #47 |
| 集成 | #76 |

## 4. 共享面与单人负责规则

以下面在多分支下最容易互相覆盖，每批内**只允许一个分支改动**，由该批的集成人协调；跨批改动必须在合入前与已合入的改动对齐：

- Alembic 迁移链：迁移编号在合入时顺序核对，保持**单一 head**；不并行新增同一 `down_revision`。
- `contracts/` 共享模型与原因码词汇：新增字段由本批唯一所有者提交，其他分支只消费。
- `api/` 装配与路由注册、前端路由表：同上。
- `PROJECT.md`、`docs/specs/`：同一时间只有一个分支改写；#37 单独成批即为此。
- `docs/validation/`：每个切片新增自己的记录文件，不改他人记录。

## 5. 发布状态与已登记的更正

2026-10-11 的发布已创建 #37–#76 全部 40 张票（`ready-for-agent`，正文含依据、验收标准、验证与依赖）。**原生依赖链接在发布中断时尚未写入。**

补登口径：**只有硬门与部分门写入 GitHub 原生 `blocked_by`；软引用写进正文本节所述位置，不进原生链接。** 理由是 [issue-tracker](./issue-tracker.md) 把「可执行前沿」定义为「阻塞项已关闭」，软引用一旦写入原生链接就会把票错误地挡在可执行前沿之外。

**补登结果（2026-10-11 已核验）**：101 条硬门边全部就位，9 条软引用未链接；#35 上另有一条本计划外的既有边（指向已关闭的 #36，保留不动）。当前可执行前沿恰好 8 张票：#37、#39、#40、#41、#42（本计划 B1/B2），以及仍在 P1 的 #21、#32、#33。复核命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/link-issue-dependencies.ps1 -PlanDir <台账目录>   # 只读预演
```

发布正文把 A/E/F 若干票登记为被开放中的 P1 验收票阻塞，这些边与「先开发的切片」冲突，按下表处理：

| 票 | 正文登记的阻塞项 | 实际关系 | 处理 |
| --- | --- | --- | --- |
| [#39](https://github.com/kksty/HuntWeave/issues/39) | #21 | 软引用：#21 是 P1 容量释放的验收，与真实执行门槛的实现无关 | 不作为硬门 |
| [#40](https://github.com/kksty/HuntWeave/issues/40) [#41](https://github.com/kksty/HuntWeave/issues/41) [#42](https://github.com/kksty/HuntWeave/issues/42) | #21 | 软引用：维护/诊断票，`#41` 的结论反而要先于 #21 的容量结论 | 不作为硬门 |
| [#57](https://github.com/kksty/HuntWeave/issues/57) | #21 #32 | 软引用：分池与公平调度可用既有 P1 基线数据实现，#21/#32 的实测数据用于校准阈值 | 不作为硬门 |
| [#69](https://github.com/kksty/HuntWeave/issues/69) | #32 | 软引用：工具基线取舍引用 #32 的数据 | 不作为硬门 |
| [#30](https://github.com/kksty/HuntWeave/issues/30) | #32（既有票） | 软引用，同上 | 不作为硬门 |
| [#76](https://github.com/kksty/HuntWeave/issues/76) | #29 | 硬门：#29 是覆盖矩阵的完整实现，属总集成的交付面 | 保留 |
| [#73](https://github.com/kksty/HuntWeave/issues/73) | #27 #28 | **部分门**：负载矩阵的带外与覆盖分母两格等这两票合入，其余格可先跑 | 先做未受阻格 |
| [#61](https://github.com/kksty/HuntWeave/issues/61) | #45 | **部分门**：投影需要 #45 的保留期与过期游标语义；只读投影实现可先做 | 先做未受阻项 |

另外三处登记问题，不影响可执行前沿：

1. **`#65` 的阻塞项把 P1 视觉票当成硬门。** [#65](https://github.com/kksty/HuntWeave/issues/65) 登记被 [#34](https://github.com/kksty/HuntWeave/issues/34)/[#35](https://github.com/kksty/HuntWeave/issues/35) 阻塞。视觉系统是「必须遵守」，但不是「必须先交付」；若按硬门执行，整条 P2 前端会串在 P1 视觉换肤后面。建议在 #34 落地视觉系统后即认作可开工。
2. **`#4` 的编号不一致（仅台账，不影响已发布正文）。** 台账内部键 `C1`/`C2` 与规格的 C1/C2 对调：`C2` 对应标题 `P2-C1`，`C1` 对应 `P2-C2`。已发布的标题与编号与规格一致（[#51](https://github.com/kksty/HuntWeave/issues/51) = C1、[#54](https://github.com/kksty/HuntWeave/issues/54) = C2），**不要按内部键重命名 Issue**。
3. **P2 里程碑尚未建立。** [issue-tracker](./issue-tracker.md) 约定阶段进度用 milestone 表达，[STATUS](../STATUS.md) 记「P2 里程碑按本文件约定在开工时建立」。本轮发布只加了 `ready-for-agent` 标签。

## 6. 关键路径

按**已写入原生链接的硬门**计算，到总集成的关键链是 11 跳：

```
#37 → #38 → #44 → #52 → #56 → #60 → #64 → #67 → #70 → #72 → #75 → #76
契约   契约   B1     D1     D2     D3     D4     D5     D6     E3     O1     Z
```

由此得到三条排期判断：

1. **B2 的四张票不在关键路径上。** #39 只影响 #48、#59、#75；#40、#41、#42 只影响 #46、#47。它们可以并行跑，但**优先做它们不会提前总集成**。
2. **前端与覆盖面整条线在关键路径之外。** 后端关键链走到 #76 时，#65（研究工作台）只被 #71、#74 消费。因此后端先行、前端集中接入（[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)）与关键路径一致，不需要为提速改动顺序。
3. **尾部收敛最紧。** #76 有 8 个前置（含 4 张前端/覆盖面的票），且 #75 串在它前面。要缩短总时长，先看这一段，而不是加宽前面的批次。

传导阻塞最重的票（直接或间接挡住的后续票数）：#76（40）、#74 与 #73（27）、#75（25）、#72（24）、#70（21）、#71（17）、#29（16）、#69（14）、#66（13）。这些票的延期按同样顺序传导，应优先保证它们的前置不被插队。

## 7. 分支与合并规则

分支命名统一为 `codex/<issue-number>-<slug>`，一个 Issue 一个工作树。**合并顺序由依赖决定，批次只约束共享面**：

1. 从当前 `main` 切出分支与工作树，开工前完成本切片的实施评审（[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)、[PROJECT §14](../../PROJECT.md)）。
2. 每个 Issue 的验收标准是**逐条复选框**。只有全部阻塞项已合入 `main` 后，引用其产物的验收项才可判定通过；其余项可先实现与验证。
3. 自验通过后在 Issue 上留下逐条结论、实现提交、验证记录路径与已知限制，再推送 `origin/main` 并关闭 Issue。
4. 合入顺序按批次；同批内如触达第 4 节的共享面，由集成人串行合入并复验。
5. 合入 `main` 后立即删除该分支与对应工作树（`git worktree remove`），避免下一个会话在旧目录上开工。

批次验收（B12/B13）额外要求：跨分支的迁移编号顺序核对、单一 Alembic head、以及一次集中前端浏览器验收（后端先行、前端集中接入，见 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md)）。

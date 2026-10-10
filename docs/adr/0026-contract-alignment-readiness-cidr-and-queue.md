# ADR-0026：开工契约对齐：可表达不等于可执行、CIDR 边界与确认级队列语义

日期：2026-10-11。状态：已采纳，待实施。**契约澄清性质**，不改写任何既有决定的能力边界。来源：[#37](https://github.com/kksty/HuntWeave/issues/37)。相关：[ADR-0010](./0010-real-execution-boundary-and-gate.md)（四项就绪门槛）、[ADR-0019](./0019-engagement-modes-and-policy-gate.md)（双模式与准入层）、[ADR-0017](./0017-claim-confirmation-and-frozen-delivery.md)（确认等级）、[ADR-0025](./0025-remove-the-research-depth-tier.md)（移除深度档位）。

本 ADR 解决四组互相矛盾的验收文字，使 `coverage` 相关的 [#25](https://github.com/kksty/HuntWeave/issues/25)、[#28](https://github.com/kksty/HuntWeave/issues/28)、[#29](https://github.com/kksty/HuntWeave/issues/29)、[#30](https://github.com/kksty/HuntWeave/issues/30)、[#34](https://github.com/kksty/HuntWeave/issues/34)、[#35](https://github.com/kksty/HuntWeave/issues/35) 可以据以开发。**本文与所引规格同时生效，缺一不可**：单独读 0008 或单独读任一 Issue 正文都会得到矛盾结论。

## 1. 背景：矛盾的原文位置

| 矛盾 | 一方原文 | 另一方原文 |
| --- | --- | --- |
| 可表达 vs 可执行 | [#25 验收 2](https://github.com/kksty/HuntWeave/issues/25)「`engagement_mode` 与 `mode` 互不影响：四种组合都能创建 Run 且语义正确」 | [ADR-0010](./0010-real-execution-boundary-and-gate.md)、[PROJECT §14](../../PROJECT.md)「四项就绪门槛同时满足才允许 `real_execution_ready=true`」 |
| CIDR | [#30 范围 2](https://github.com/kksty/HuntWeave/issues/30)「**CIDR 展开预览**：接受 CIDR，但只做展开、由操作员确认展开结果」、[#30 验收 2](https://github.com/kksty/HuntWeave/issues/30)「CIDR 展开结果在提交前可见、可修正」、[0008 §8 C-F](../specs/0008-coverage-mode.md) | [PROJECT §3.2](../../PROJECT.md)「域名、URL、CIDR、ASN 作为批量目标来源 …首版 IP 输入接口明确拒绝；以后作为独立需求评估」、[PROJECT §4.2](../../PROJECT.md)「首版不接受混入端口、协议、域名、网段或任意命令的行」 |
| 确认级与队列 | [#28 范围 1](https://github.com/kksty/HuntWeave/issues/28)「确认级才进人工队列」、[#28 验收 3](https://github.com/kksty/HuntWeave/issues/28)「只有确认级条目进入人工队列；可疑与未决不占用人工队列」 | [#28 范围 4](https://github.com/kksty/HuntWeave/issues/28)「补一个**待补证（缺条件）**状态」、[0006 §3.2](../specs/0006-state-model-and-delivery.md)「缺证据、分类争议和失效重评也能入队，但注明目的」 |
| 差分对照 | [0008 §5.1](../specs/0008-coverage-mode.md)「**判定必须带差分对照**（带条件与不带条件的可比对）」 | [0005 §6](../specs/0005-capability-claim-criteria.md)「对照要求按主张类型适用，**不机械要求每项主张都执行额外对照动作**；对照可以由同一次采集中的既有观察满足」 |
| V-C 归属 | [0009 §12](../specs/0009-visual-design-system.md) 的 V-C「面板结构、接缝、铆钉与身份元素」没有对应的实施 Issue | [#34 范围 1–3、5](https://github.com/kksty/HuntWeave/issues/34) 已经把面板直角/接缝/铆钉、身份带与任务徽章全部列入 |

## 2. 决定一：可表达不等于可执行

**字段可构造、可持久化、可查询，不等于该 Run 能执行。** 两者是不同层次的判断，必须分别验收。

现状（已核验，见验证记录 `0020`）：`ScopeCreate.mode` 与 `ScopeCreate.execution_profile` 是两个独立字段，契约上没有跨字段约束，因此 `(demonstration, fake-p0-v1)`、`(demonstration, real-lab-v1)`、`(real, fake-p0-v1)`、`(real, real-lab-v1)` 四种组合**都能被构造**。这支持 #25 验收 2 的前半句。但「都能创建 Run」不推出「都能执行」：真实执行另由四层判断阻断。

| 层 | 判断 | 现在的实际状态 |
| --- | --- | --- |
| L1 语法层 | 字段取值合法、跨字段组合可表达 | 四种组合均通过（#25 验收 2 的前半句） |
| L2 部署就绪层 | ADR-0010 四项门槛**同时**成立才允许 `real_execution_ready=true` | `profile_revalidation` 与 `deployment_revert` 为假，`real_execution_ready=false`，请求真实执行的 Run 被拒绝 |
| L3 授权绑定层 | Run 的 `execution_profile` 必须与其授权快照 `ScopeSnapshot` 一致；快照在提交时写入 `policy_version`，**旧 Run 不被当前构建改写** | 旧授权快照的 Run 在升级后保持原策略与结论语义 |
| L4 模式准入层 | `engagement_mode=breach` 的注册表**先为空**；空注册表**不得被解释为「研究完成」** | `breach` 无任何已注册动作，只产生「模式未就绪/无可用动作」，不产生完成结论 |

因此 [#25 验收 2](https://github.com/kksty/HuntWeave/issues/25) 的「四种组合都能创建 Run 且语义正确」应读作：**L1 全部通过；L2–L4 保持阻断不变。** 「语义正确」的验收内容是可表达性与拒绝原因可区分，**不是**四种组合都能跑出真实动作。任何把本轮读成「放宽真实执行」的说法都错误。

四项门槛、旧授权快照与空 `breach` 注册表的阻断**在本轮全部保持有效**，不因模式字段新增而放宽（与 [ADR-0019 后果段](./0019-engagement-modes-and-policy-gate.md)一致）。落点见 [0008 §2.1](../specs/0008-coverage-mode.md)。

## 3. 决定二：CIDR 边界**未决**，现状不变

**这是本 ADR 唯一未决定的部分，全文见 [0008 §6](../specs/0008-coverage-mode.md)。**

- **现在生效的规则不变**：批量目标来源只有操作员的 IP 清单，CIDR/域名/URL/ASN 在输入接口被拒绝（`invalid_ip`）；单 Run 上限 `TARGET_LIMIT = 100`，按去重后的规范化目标计数。
- [#30](https://github.com/kksty/HuntWeave/issues/30) 与 [0008 §8 C-F](../specs/0008-coverage-mode.md) 中「接受 CIDR」「CIDR 展开预览」的验收文字**当前不成立**，不能据以开发。
- 具体边界变更提案、修订点与 5 项待确认项已写入 0008 §6，供操作员决定。
- **在得到明确范围决定之前，不按 #30 正文自行放宽。** 这不是「已拒绝 CIDR」，而是「现状保留、变更待决」。

## 4. 决定三：确认级、证据满足与 `confirmed` 三分

「确认级」是**分流候选状态**，不是 A1 判定值，也不是 A2 的完成事实。三件事必须分别可查询：

| 概念 | 属于 | 含义 | 写入方 |
| --- | --- | --- | --- |
| 证据要求已满足 | 谓词（对固定契约与输入版本计算） | 该主张的展开后有效要求齐备 | 程序 |
| 确认等级就绪（确认级） | 分流候选状态 | 可以进入「确认复核」队列；`required_confirmation_level` 未完成 | 程序 |
| `confirmed` | A1 主张判定 | 达到契约确认等级、必要输入当前有效 | 人工/独立复审按等级 |

**关键判定：覆盖面模式的自动采证不等待人工。** 某项主张所需的自动采证完成后，`StopPolicy` 停止该验证分支并进入独立复审（[0008 §3.6](../specs/0008-coverage-mode.md)）；进入队列只是登记待处理工作，**不阻塞其他检查项、其他资产或其他分支**。服务端执行能力（`exec-cap(server)`）默认 `human` 等级（[0006 §3.1](../specs/0006-state-model-and-delivery.md)），若把「确认级」直接映射成 `confirmed`，或把「等人工确认」实现成「不能继续采证」，该模式会死锁。

**人工／复核队列的目的不止一个**，各目的有独立入口条件与退出路径，任一目的不得阻塞其他目的：

| 队列目的 | 入口条件 | 允许的复核动作 | 退出路径 | 不得变成 |
| --- | --- | --- | --- | --- |
| `confirm` 确认复核 | 证据要求已满足且达确认级 | 确认、退回 | 裁定为 `confirmed` 或回到 `supported` 并列缺口 | 权限内执行前的逐次审批 |
| `evidence_gap` 缺证 | `satisfied=false` 且已登记要补哪条证据 | 要求补证、放弃该分支 | 补证完成或持久化带原因的未决结束 | 因 `satisfied=false` 丢弃重审需求 |
| `classification` 分类澄清 | 分类存在争议 | 澄清分类与适用判据 | 契约明确后重评，或保留未决 | 冻结独立的合法研究 |
| `lapsed_retest` 失效重审 | 必要输入失效、结论 `lapsed` | 登记并决定是否重做 | 用户明确请求后重评；**不自动花费预算或重开目标测试** | 自动恢复执行 |
| `retest` 打回复测 | 已登记「要补哪条证据」 | 有界再验证 | 复用原参数/条件/环境身份并记录差异 | 重跑整个资产 |

队列不静默丢弃；等待人工不占目标执行槽，也不阻止独立研究或自动阶段收尾（[0006 §3.2](../specs/0006-state-model-and-delivery.md)、[PROJECT §12.3](../../PROJECT.md)）。超 SLA 只能提醒或升级，**不能自动 `confirmed`**。

## 5. 决定四：差分对照的适用与不适用归判据

**是否要求差分对照由主张类型的判据规则决定，不由 [0008 §5.1](../specs/0008-coverage-mode.md) 一律要求，也不由模型单方豁免。**

- 对照**可以**由同一次采集中的既有观察满足；**不得为满足形式而追加目标动作**（[0005 §6](../specs/0005-capability-claim-criteria.md)）。
- 复测复用原参数、条件与环境身份并记录差异；对照缺失时结论上限为 `supported`，不是「复测不稳定」。
- 「不适用」必须符合该主张类型的适用条件，并由复审确认、留下记录与依据；**程序校验不适用声明的类别、引用与版本是否齐备**，模型不能仅凭填写理由即豁免。

0008 §5.1 的差分对照因此是「复测必须可比」的**充分条件说明**，而不是一条独立于判据表的全局强制项。

## 6. 决定五：视觉切片归属

- **V-C**（面板结构、接缝、铆钉与身份元素）的验收范围**已被 [#34](https://github.com/kksty/HuntWeave/issues/34) 覆盖**，不另开切片。#34 的 [#33](https://github.com/kksty/HuntWeave/issues/33) 依赖与浏览器验收义务不变。
- **V-D**（模式标注与覆盖状态视图）：**薄接入归 [#35](https://github.com/kksty/HuntWeave/issues/35)，完整矩阵与缺口视图的渲染及下钻归 [#29](https://github.com/kksty/HuntWeave/issues/29)。** 两者不重复定义同一投影协议（[ADR-0023](./0023-two-mode-visualization-projections.md)）。
- **不恢复研究深度档位**（[ADR-0025](./0025-remove-the-research-depth-tier.md)）：研究取向只由测试模式表达、投入额度只由预算表达，两者不互相推导。#35 验收 3 与 #34 范围 8 均按此执行。

落点见 [0009 §12](../specs/0009-visual-design-system.md) 与 [P2 切片分批执行计划 §5](../agents/p2-execution-batches.md)。

## 7. 实施评审结论（本切片开工前）

对照实际代码核验了文档中关于「不存在的字段」「现有实现」的断言（命令与输出见验证记录 `0020`）：

| 断言 | 核验结果 |
| --- | --- |
| 代码中没有 `engagement_mode` | 成立：`backend/src` 中匹配数 **0** |
| 代码中没有 `PolicyGate` | 成立：**0** |
| 代码中没有 `StopPolicy` | 成立：**0** |
| 代码中没有 `required_confirmation_level` | 成立：**0** |
| 存在 `ReadinessGate` 四项门槛 | 成立：`profile_revalidation` / `contract_expressiveness` / `console_consumption` / `deployment_revert` |
| 存在 `mode = demonstration / real` 且与新字段并存 | 成立：`runs.py:87`、`runs.py:101`、`capabilities.py:42` |
| 授权快照写入 `policy_version` 且旧 Run 不被改写 | 成立：`runs.py:106`（`EXECUTION_POLICY_VERSION`） |
| `breach` 注册表为空、三个假动作保留 | 成立：`execution.py:43` 三个假动作、`:44` 三个真实动作，无 breach 动作 |
| 现状输入接口拒绝 CIDR | 成立：`backend/src` 中 `cidr`/`ip_network`/`collapse_addresses` 匹配数 **0**；`frontend/src/workspace.ts:119` 的 `invalid_ip` 文案明确「不接受 URL、端口、域名、CIDR 或 zone ID」 |
| `TARGET_LIMIT` 为 100 且按去重目标计数 | 成立：`runs/inputs.py:13`、`:53` |

**结论：本切片的文档修订方向成立，不声明任何代码中不存在的能力。** 本 ADR 与所引规格均只修改设计/契约文字，能力状态不变，P1 顺序与真实执行门槛不变。

## 后果

- [0008](../specs/0008-coverage-mode.md) 增补 §2.1 的四层阻断表、§3.5.1/§3.5.2 的三分与队列目的、§5.1 的判据交叉引用、§6 的 CIDR 待确认小节（原有 §6–§8 顺延为 §7–§9）；C-F 验收明确不含 CIDR 展开。
- [0005 §6](../specs/0005-capability-claim-criteria.md) 与 [0006 §3.2](../specs/0006-state-model-and-delivery.md) 增补与 0008 §5.1 的双向引用，避免两处各自成立、合起来矛盾。
- [0009 §12](../specs/0009-visual-design-system.md) 记录 V-C/V-D 的切片归属。
- [PROJECT.md](../../PROJECT.md) 升版到 0.9.16：第 3.2 节增补边界澄清段、第 4.2/12 节写明 CIDR 不在输入接口内且不绕过 100 上限、第 9.1 节写明「证据要求已满足 / 确认级就绪 / `confirmed`」三分。
- [P2 切片分批执行计划 §5](../agents/p2-execution-batches.md) 登记本轮口径更正，跨分支不再按旧验收文字开工。
- 不新增部署服务、不新增数据库迁移或空字段、不改任何后端/前端代码。
- **待操作员决定**：CIDR 是否纳入范围（0008 §6 的 5 项）。未决定前保留 IP-only 与 100 上限。

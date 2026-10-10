# 开工契约对齐：可表达与可执行、CIDR 边界、确认级队列与对照规则

日期：2026-10-11（Asia/Shanghai）。对应 [#37](https://github.com/kksty/HuntWeave/issues/37)。本切片**不做代码改动**，交付物是契约修订（`PROJECT.md`、`docs/specs/`、`docs/adr/`、`docs/agents/p2-execution-batches.md`），因此验证方式是**文档交叉引用、术语与 Issue 验收口径的一致性检查，加上对文档所断言代码事实的机械核对**，不是功能验收。

## 环境与入口

- 工作目录：`D:\code\huntweave-wt\37-contract-alignment`（worktree，从 `origin/main` 的 `7c56643` 切出）；分支 `codex/37-contract-alignment`；主仓库 `D:\code\HuntWeave` 未改动。
- Windows 11 x86_64、PowerShell、`gh` CLI。**未启动 Docker 栈**：本切片无代码变更，没有可执行的运行时行为需要验证，按 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md) 的「文档和展示类改动只做相应检查，不增加无意义测试」执行。
- 未接触任何目标；`lab/` 靶场与容器均未运行。
- 盘点基线：Issue #37 声明 2026-10-11、`main` `4f0d063`；本 worktree 实际起点为 `origin/main` 的 `7c56643`（含 #77 的两条后续提交）。

## 实施评审：核对文档的「不存在的字段 / 现有实现」断言

命令（在 worktree 根目录）：

```powershell
$pats = 'engagement_mode','PolicyGate','StopPolicy','required_confirmation_level','TARGET_LIMIT'
foreach ($p in $pats) {
  $n = (Select-String -Path (Get-ChildItem -Recurse -File -Include *.py backend/src | ForEach-Object FullName) `
        -Pattern $p -SimpleMatch -ErrorAction SilentlyContinue | Measure-Object).Count
  "{0,-30} {1}" -f $p, $n
}
Select-String -Path backend/src/huntweave/contracts/runs.py,backend/src/huntweave/contracts/capabilities.py -Pattern 'mode: Literal'
Select-String -Path backend/src/huntweave/contracts/capabilities.py -Pattern 'profile_revalidation|contract_expressiveness|console_consumption|deployment_revert'
Select-String -Path backend/src/huntweave/contracts/runs.py -Pattern 'policy_version'
Select-String -Path backend/src/huntweave/contracts/execution.py -Pattern 'FAKE_ACTIONS|REAL_ACTIONS|^ACTIONS'
(Select-String -Path (Get-ChildItem -Recurse -File -Include *.py backend/src | ForEach-Object FullName) `
  -Pattern 'cidr|ip_network|collapse_addresses' -ErrorAction SilentlyContinue | Measure-Object).Count
Select-String -Path frontend/src/workspace.ts -Pattern 'invalid_ip'
Select-String -Path backend/src/huntweave/runs/inputs.py -Pattern 'TARGET_LIMIT'
```

实际输出（原文）：

```
=== A) engagement_mode / PolicyGate / StopPolicy / required_confirmation_level 在源码中出现次数 ===
engagement_mode                0
PolicyGate                     0
StopPolicy                     0
required_confirmation_level    0
TARGET_LIMIT                   7

=== B) mode = demonstration/real 现存字段 ===
runs.py:87: mode: Literal["demonstration", "real"] = "demonstration"
runs.py:101: mode: Literal["demonstration", "real"] = "demonstration"
capabilities.py:42: mode: Literal["demonstration", "real"] = "demonstration"

=== C) ADR-0010 四项门槛名 ===
capabilities.py:15: "profile_revalidation",
capabilities.py:16: "contract_expressiveness",
capabilities.py:17: "console_consumption",
capabilities.py:18: "deployment_revert",

=== D) 授权快照策略版本 ===
runs.py:23: EXECUTION_POLICY_VERSION = 1
runs.py:106: policy_version: int = Field(default=EXECUTION_POLICY_VERSION, ge=1, strict=True)

=== E) ACTIONS / 假动作 / 真实动作 ===
execution.py:43: FAKE_ACTIONS: tuple[ActionId, ...] = ("fake.collect", "fake.verify", "fake.review")
execution.py:44: REAL_ACTIONS: tuple[ActionId, ...] = ("shell.exec", "discover_tcp_services", "probe_http")
execution.py:49: "fake-p0-v1": FAKE_ACTIONS,
execution.py:50: "real-lab-v1": REAL_ACTIONS,
execution.py:122: ACTIONS: dict[ActionId, ActionSpec] = {

=== F) IP 输入拒绝 CIDR（无任何 cidr 分支） ===
backend/src 中 cidr/ip_network/collapse_addresses 匹配数: 0
workspace.ts:119: protected_address: '环回、链路本地、未指定或组播地址不支持。', invalid_ip: '仅接受纯 IP，不接受 URL、端口、域名、CIDR 或 zone ID。'
```

判定：文档中「仓库不存在 `PolicyGate` / `StopPolicy` / `engagement_mode` / `required_confirmation_level`」「存在 `ReadinessGate` 四项门槛」「`mode = demonstration / real` 与新模式字段并存」「授权快照写入 `policy_version`」「`breach` 注册表为空且三个假动作保留」「现状输入接口拒绝 CIDR」这些断言**全部为真**。修订后的文档没有声明任何代码中不存在的能力（见下文逐条结论与「已知限制」）。

关键读取（本记录引用其行为，不引用其内容）：

- `backend/src/huntweave/contracts/runs.py:87`、`:101`（`ScopeCreate.mode` / `ScopeSnapshot.mode`）；`:106`（`policy_version`）。
- `backend/src/huntweave/contracts/capabilities.py:14-19`（四个门槛名）、`:42`（`Capabilities.mode`）。
- `backend/src/huntweave/contracts/execution.py:43-50`（三个假动作 / 三个真实动作 / 按 profile 分派）、`:122`（`ACTIONS`）。
- `backend/src/huntweave/runs/inputs.py:13`（`TARGET_LIMIT = 100`）、`:53-57`（超限逐行 `target_limit_exceeded`）。
- `frontend/src/workspace.ts:119`（`invalid_ip` 文案明确拒绝 CIDR）。

## 行为与结果

本切片没有运行时行为。下表是**改动的落点**与对应的验收标准：

| 落点 | 内容 |
| --- | --- |
| `docs/adr/0026-contract-alignment-readiness-cidr-and-queue.md`（新建，112 行） | 契约澄清决定：四层阻断、CIDR 未决、确认级三分与队列目的、对照归判据、视觉切片归属，以及本次实施评审结论 |
| `PROJECT.md:3` | 版本 0.9.15 → **0.9.16** |
| `PROJECT.md:41` | 0.9.16 修订说明（四组矛盾与处置、视觉归属、不改门槛） |
| `PROJECT.md:138` | §3.2 增补「边界澄清」段：CIDR 现状、无展开路径、100 上限不被绕过、未决归属 |
| `PROJECT.md:158` | §4.2 新增条：CIDR 及其它批量来源不在输入接口内，不隐含网段展开 |
| `PROJECT.md:574` | §9.1 新增「确认等级、证据满足与判定三分」段与三分表、队列目的不阻塞 |
| `PROJECT.md:832` | §12 表「单 Run 导入上限」行：注明按去重目标计数、不含 CIDR、无绕过路径 |
| `PROJECT.md:1029` | §14.1 必测场景「输入混入 URL/CIDR」行：逐行拒绝、不隐含扩目标 |
| `docs/specs/0008-coverage-mode.md:22`（§2.1） | 可表达不等于可执行：L1–L4 四层与各层要求 |
| `docs/specs/0008-coverage-mode.md:70`（§3.5.1） | 证据满足 / 确认级就绪 / `confirmed` 三分表，自动采证不等待人工 |
| `docs/specs/0008-coverage-mode.md:80`（§3.5.2） | 五种队列目的表（入口、允许动作、退出路径、不得变成） |
| `docs/specs/0008-coverage-mode.md:94` | §3.6 停止规则：停止的是该验证分支，不是整个 Run |
| `docs/specs/0008-coverage-mode.md:158`（§6） | CIDR 边界变更提案 + 修订点 + 5 项待确认项；**现状不变** |
| `docs/specs/0008-coverage-mode.md:181` | 若提案被接受须修订的位置（PROJECT §3.2/§4.2/§12、0008 §7 与 §8 的 C-F、#30 正文） |
| `docs/specs/0008-coverage-mode.md:198`（§7） | 验收 8 与 15 按三分与对照通则重写 |
| `docs/specs/0008-coverage-mode.md:216`（§8） | C-F 行明确 CIDR 展开以待确认项解锁为前置；第一交付面是上限口径与行级反馈 |
| `docs/specs/0005-capability-claim-criteria.md:160`（§6） | 与覆盖面复测语义的关系：适用与否归判据、不追加目标动作、不得单方豁免 |
| `docs/specs/0006-state-model-and-delivery.md:63`（§3.2） | 五种队列目的标识、三分概念、不得死锁（以 0008 §3.5.2 为唯一来源） |
| `docs/specs/0009-visual-design-system.md:114-118`（§12） | 切片归属列 + V-C/V-D 归属澄清段 |
| `docs/adr/README.md:32,58` | ADR-0026 索引行与说明段 |
| `docs/agents/p2-execution-batches.md:99`（§5.1） | #37 口径更正登记表（5 条）+ 未登记原生依赖边的说明 |
| `docs/research/2026-10-10-two-mode-architecture.md:135` | 给「导入上限」行加补注：其前提未获范围决定（原文保留不改） |

注：新增 §6 使 0008 原有 §6–§8 顺延为 §7–§9。已核对**本分支改动的文档与规格**对 0008 小节号的引用：`docs/research/2026-10-10-two-mode-architecture.md:153` 引用的 §3.7 与 §5.1 **未受影响**；`docs/STATUS.md:63` 引用的「0008 §7 的 C-A…C-F 切片」**已过期**（现为 §8），按本票白名单不得改 `docs/STATUS.md`，转由协调人在合入后统一更新（见「已知限制」）。全仓库范围的小节号引用核查不在本票证据内（`docs/STATUS.md` 不在白名单）。

## 机械一致性检查

命令：

```powershell
# V1 CIDR 现状描述一致
Select-String -Path PROJECT.md,docs/specs/0008-coverage-mode.md -Pattern 'IP-only|不接受 .*CIDR|CIDR.*拒绝|不含 CIDR|不出现在输入接口'
# V2 TARGET_LIMIT = 100 代码与文档一致
Select-String -Path backend/src/huntweave/runs/inputs.py,PROJECT.md,docs/specs/0008-coverage-mode.md -Pattern 'TARGET_LIMIT = 100|单 Run 导入上限'
# V3 队列目的词表在 0008 与 0006 同时出现
foreach ($f in 'docs/specs/0008-coverage-mode.md','docs/specs/0006-state-model-and-delivery.md','PROJECT.md') {
  (Select-String -Path $f -Pattern 'confirm.*确认复核|evidence_gap|classification|lapsed_retest|retest.*打回复测' | Measure-Object).Count }
# V4 三分概念在三份文件均出现
foreach ($f in 'PROJECT.md','docs/specs/0008-coverage-mode.md','docs/specs/0006-state-model-and-delivery.md') {
  $a=(Select-String -Path $f -Pattern '证据要求已满足'|Measure-Object).Count
  $b=(Select-String -Path $f -Pattern '确认级'|Measure-Object).Count
  $c=(Select-String -Path $f -Pattern 'confirmed'|Measure-Object).Count
  "{0,-45} 证据要求已满足={1} 确认级={2} confirmed={3}" -f $f,$a,$b,$c }
# V5 残留的「CIDR 展开」表述
Select-String -Path (Get-ChildItem -Recurse -File -Include *.md | ForEach-Object FullName) -Pattern 'CIDR 展开'
# V6 研究深度档位未被恢复
Select-String -Path (Get-ChildItem -Recurse -File -Include *.md | ForEach-Object FullName) -Pattern '深度档位|研究深度'
```

结果：

| 检查 | 结果 |
| --- | --- |
| V1 CIDR 现状 | 一致：`PROJECT.md:127/138/158/832/1029` 与 `0008:158-196/225-227` 都表述为「输入接口拒绝、无展开路径、上限不被绕过」；`0008:160` 明确「这条验收标准当前不成立」 |
| V2 上限 | 一致：`inputs.py:13` 的 `TARGET_LIMIT = 100` 与 `PROJECT.md:832`、`0008:165/191/227` 三处口径相同（去重后的规范化目标） |
| V3 队列目的 | 修订后 0008 命中 5 行、0006 命中 1 行（五种目的标识齐全）；修订前 0006 为 0 行——这正是本轮补上的矛盾点 |
| V4 三分概念 | 三份文件均出现：PROJECT 2/5/5、0008 4/9/7、0006 1/1/16（分别为「证据要求已满足 / 确认级 / confirmed」匹配行数） |
| V5 「CIDR 展开」残留 | 仅剩 8 处，全部属于：ADR-0026 的**矛盾摘录**（2 处）、0008 §6 的**提案与待确认表述**（4 处）、`p2-execution-batches.md` 的**原口径摘录**（1 处）、`research` 的**历史行**（1 处，已加补注）。没有一处把 CIDR 展开写成当前生效的验收要求 |
| V6 深度档位 | 命中全部落在 ADR-0025/ADR-0016 的历史条款、索引说明、0009 §12 的「不恢复」约束与验证记录中；**没有任何新增档位控件或取值** |

## 逐条验收结论

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | 明确 #25 的四种字段组合**可表达**不等于真实 Run **可执行**；四项就绪门槛、旧授权快照与空 breach 注册表的阻断保持有效 | **通过** | `0008:22-33` 的 L1–L4 四层表（L2 ADR-0010 四项门槛、L3 授权快照 `policy_version`、L4 空 `breach` 注册表）逐层写「不放宽」；`ADR-0026:17-32` 的四个组合与四层判断；代码核验 `capabilities.py:14-19`、`runs.py:106`、`execution.py:43-44`（A/B/C/D/E 段输出）。未新增任何「四种组合都能执行」的表述 |
| 2 | 核对 #30 / 0008 C-F 的 CIDR 展开意图与 PROJECT §3.2 的排除，形成具体边界变更提案、ADR/总纲修订及待确认项；未获范围决定前保留 IP-only / 100 上限，不自行放宽 | **通过（提案已交付，规则未改）** | `0008:158-196` 的 §6：现状 4 条 + 提案 7 行表 + 修订点 4 处 + **5 项待确认**；`PROJECT.md:138`（§3.2 边界澄清）、`:158`（§4.2）、`:832`（§12）、`:1029`（§14.1）；`ADR-0026:34-41`；`0008:225-227` 的 C-F 行把 CIDR 展开标为「以待确认项解锁为前置，未确认前按 IP-only 实现」。V1/V2 检查证明 IP-only 与 100 上限在三处口径一致 |
| 3 | 统一 #28 的「确认级才进人工队列」与 server 默认 `human` 等级：自动采证满足与最终 `confirmed` 分开，分类澄清/缺证/失效重审可有不同队列目的，不能死锁 | **通过** | `0008:64-92`：§3.5 首段把「确认级才进人工队列」限定为**确认复核**一个目的；§3.5.1 三分表；§3.5.2 五种目的表（含 `classification`/`evidence_gap`/`lapsed_retest`）；`0008:94-98` 停止规则；`0006:63` 五种目的标识与不得死锁；`PROJECT.md:574` 三分表；`0008:207` 验收 8 重写。V3/V4 检查 |
| 4 | 对齐 0008 §5.1 的差分对照与 0005 §6 的适用/不适用规则，不为形式追加目标动作，也不由模型单方豁免 | **通过** | `0008:146-156` §5.1 增补条（按主张类型适用、既有观察可满足、不追加目标动作、不适用须复审确认、程序校验类别/引用/版本）；`0005:160` §6 增补条（与复测语义的关系，明确「0008 §5.1 是复测可比的充分条件说明，适用与否归本节」）；`0008:214` 验收 15 重写 |
| 5 | 记录 V-C 已被 #34 的面板/接缝/身份元素范围覆盖；#35 为薄接入、#29 为完整矩阵；不恢复研究深度档位 | **通过** | `0009:105-118`：切片表新增「归属 Issue」列并逐行标注；V-C 行写「已被 #34 的验收范围覆盖，不另开切片」；`0009:114-118` 归属澄清段明确 V-D 两层分工与投影协议唯一性；不恢复档位的约束写明并引 ADR-0025。`ADR-0026:77-83`；`p2-execution-batches.md:99-109` 第 5 条；`ADR README:32,58`。V6 检查 |

**总结论：5 条全部通过。** 本切片不含功能验收，因此「通过」只表示**契约文字已可据以开发且与既有决定不再矛盾**，不表示 #25/#28/#29/#30/#34/#35 的任何能力已实现或已验收。

## 未达成与限制

- **未做功能验证**：没有代码变更，未启动 Docker 栈、未跑 API / PostgreSQL / Runner / 靶场、未做浏览器验收。本记录的任何「通过」都不构成功能证据。
- **CIDR 范围决定未完成**：`0008` §6 的 5 项待确认项全部开放。在操作员决定前，C-F 的交付范围只含上限口径与行级反馈，**不含 CIDR 展开**；这不是「已决定拒绝 CIDR」。
- **Issue 正文与原生依赖未改**：按本票约束，GitHub 上的 Issue 正文、标签与 `blocked_by` 边均未改动。矛盾原文仍留在 Issue 上，直到协调人执行更正（建议见下节）。跨分支在更正执行前读 Issue 会继续读到旧口径，**以仓库文档为准**。
- **`docs/STATUS.md` 未改**：按白名单该文件由协调人在合入后统一更新。因此 `STATUS.md:63` 的「0008 §7 的 C-A…C-F 切片」在本分支内是**过期引用**（现为 §8），`STATUS.md:75` 的「批量导入（单 Run 上限、CIDR 展开预览、跨 Run 资产台账）」也仍按旧口径列举。
- **未改历史验证记录**：`docs/validation/0004-identity-runs.md:23` 与 `0008-p1-target-limit.md` 等既有记录保留原样（约定：不改他人记录）。其中 `0004` 对 CIDR 拒绝的描述与本轮结论一致，无需更正。
- **未改动 `GLOSSARY.md`**：三分与队列目的术语已在三份规格中定义并互相引用，不需要新增词条；本轮判断新增词条只会扩大术语面。
- **`docs/research/2026-10-10-two-mode-architecture.md` 的原文保留**：只加了 2026-10-11 补注行，未改写原判断（研究记录按轮次留痕的既有约定）。
- **未复核 CI**：本票无代码改动，未推送、未触发 `checks`。本地未跑 `ruff`/`mypy`/`pytest`——它们的输入没有变化。

### 发现但未修的不一致（不扩大范围）

1. `docs/STATUS.md:63`「按 `0008 §7` 的 C-A…C-F 切片推进」——本轮 §6 插入后应为 §8。该文件不在本票白名单内。
2. `docs/STATUS.md:75` 把「CIDR 展开预览」列为未实现能力之一，与 `PROJECT.md §3.2` 的排除口径并列时容易读成「已确定要做、只是没做」。同属白名单外。
3. `docs/specs/0007-research-workbench-ui.md` 的创建流程仍只提到执行模式；测试模式选择已在 `PROJECT.md §4.1` 与 `#35` 范围 2 固定，但 0007 未同步。本票第 5 条只要求记录切片归属，未要求改 0007，故未动。
4. `docs/validation/0008-p1-target-limit.md` 记载「未做可配置化，预览接口同样按上限拒绝」——与现状一致，但未记载「CIDR 行按 `invalid_ip` 拒绝」这一相邻事实；本轮在 `0008`（规格）§6 记录，未回改历史验证记录。
5. `docs/research/2026-10-10-two-mode-architecture.md` 的第 134 行原文与 `PROJECT.md §3.2` 的口径冲突，本轮只加补注不改原文；若后续决定接受 CIDR，该行的「成立」判断需重新评估。

## 待人工确认

- **CIDR 范围决定**（5 项，见 `0008` §6）：需操作员/产品裁定。裁定后必须同步修订 `PROJECT.md` 第 3.2、4.2、12、14.1 节、`0008` §6–§8 的 C-F 验收，以及 #30 正文的范围 2 与验收 2、3。
- **GitHub 侧更正**（由协调人执行，本会话不操作）：
  1. [#30](https://github.com/kksty/HuntWeave/issues/30) 范围 2 原文「**CIDR 展开预览**：接受 CIDR，但**只做展开、由操作员确认展开结果**，平台不自动扩大目标集合（总纲 3.2）。展开数量超过上限时的行级反馈与拒绝语义要明确。」→ 建议改为：「**CIDR 展开预览（待范围决定，[ADR-0026](../adr/0026-contract-alignment-readiness-cidr-and-queue.md)、[0008 §6](../specs/0008-coverage-mode.md)）**：现状为 IP-only，输入接口拒绝含 `/` 的前缀（`invalid_ip`）。CIDR 是否纳入首版属未决产品决定；提案与 5 项待确认项见 0008 §6。**获得范围决定前不得按本条实现展开路径**；获得后须先修订 PROJECT §3.2 与 0008 的 C-F 验收，再开工。」
  2. [#30](https://github.com/kksty/HuntWeave/issues/30) 验收 2 原文「CIDR 展开结果在提交前可见、可修正，未确认时不创建 Run。」→ 建议改为：「（**待范围决定**）CIDR 若纳入范围，展开结果须在提交前可见、可修正，未确认时不创建 Run；未纳入前本项不适用，改为核验含 `/` 前缀按 `invalid_ip` 逐行拒绝。」
  3. [#30](https://github.com/kksty/HuntWeave/issues/30) 验收 3 原文「展开与去重的结果与手写清单一致；IPv6 有独立原因码。」→ 建议改为：「（**待范围决定**）展开与去重的结果与手写清单一致；未纳入前本项不适用。IPv6 有独立原因码 `ipv6_environment_unsupported`，与 CIDR 的 `invalid_ip` 不混。」
  4. [#25](https://github.com/kksty/HuntWeave/issues/25) 验收 2 原文「`engagement_mode` 与 `mode` 互不影响：四种组合都能创建 Run 且语义正确。」→ 建议改为：「`engagement_mode` 与 `mode` 互不影响：四种组合在**语法层**都能创建 Run，拒绝原因可区分（模式不允许/未授权/预算不足）；**可表达不等于可执行**——[ADR-0010](../adr/0010-real-execution-boundary-and-gate.md) 四项就绪门槛、旧授权快照绑定与空 breach 注册表的阻断保持有效，见 [0008 §2.1](../specs/0008-coverage-mode.md)。」
  5. [#28](https://github.com/kksty/HuntWeave/issues/28) 验收 3 原文「只有确认级条目进入人工队列；可疑与未决不占用人工队列。」→ 建议改为：「**只有确认级条目以「确认复核」目的进入人工队列**；可疑与未决不以该目的入队。缺证、分类争议与失效重评各自按注明的队列目的入队且互不阻塞，「证据要求已满足」「确认级就绪」与 `confirmed` 三分且分别可查询，见 [0008 §3.5.1–§3.5.2](../specs/0008-coverage-mode.md)。」
  6. [#28](https://github.com/kksty/HuntWeave/issues/28) 验收 6 原文「没有差分对照的命中不能进入确认级。」→ 建议补一句：「对照的适用与否按主张类型判据（[0005 §6](../specs/0005-capability-claim-criteria.md)）：可由同一次采集中的既有观察满足，不得为满足形式追加目标动作；声明「不适用」须复审确认并留依据，模型不得单方豁免。」
  7. [#34](https://github.com/kksty/HuntWeave/issues/34)（可选，仅加一条说明评论）建议补：「本票的面板/接缝/身份元素范围**已覆盖 [0009 §12](../specs/0009-visual-design-system.md) 的 V-C 切片**（见 [ADR-0026 §6](../adr/0026-contract-alignment-readiness-cidr-and-queue.md)），V-C 不另开切片。」
  8. **原生依赖边**：本轮**不新增、不删除**任何 `blocked_by` 边。[#35](https://github.com/kksty/HuntWeave/issues/35) 已有的 `#25`/`#33`/`#36` 三条依赖与 #29 的 `#28` 依赖仍然正确，无需更正。
- **`docs/STATUS.md` 的两处过期引用**由协调人在合入后一并更新（第 63 行的「0008 §7」→ §8；第 75 行「CIDR 展开预览」的列举）。

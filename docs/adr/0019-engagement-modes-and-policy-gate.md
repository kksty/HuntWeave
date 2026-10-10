# ADR-0019：双模式策略与准入层

日期：2026-10-10。状态：已采纳，待实施（设计已确定，能力未实现）。决策依据见 [2026-10-10 双模式架构评估](../research/2026-10-10-two-mode-architecture.md)。

同一个执行内核服务两种模式：`coverage`（覆盖面：一批固定资产，尽量多测、挖到即停）与 `breach`（对一组固定资产取得访问权并留证）。Run 固定 `engagement_mode`，随 `EXECUTION_POLICY_VERSION` 一起进入授权快照，旧 Run 不被当前构建改写。

模式差异只允许出现在三处：**动作注册表的声明**、**规划器**、**停止规则**。注册表只声明工具，规划器只提出研究建议，停止规则只管研究停止，**实际准入由 PolicyGate 决定**（动作类别、数据访问、资源限额、网络与会话能力）。执行内核**不读取模式**，但**需要为能力扩展**（UDP、入向监听、长期会话都不在现有契约内）。

模式在 Run 边界固定，agent 不得自升级：coverage 产出候选后由操作员开 breach Run，沿用既有「不能自行续期授权或通过新建 Run 继续原任务」。证据标准**不随模式降低**；报告可共享结构再增加模式对应内容。

## 技术取舍

- **不接受"动作注册表按模式过滤即能力边界"**。通用 Shell 直接执行命令文本（[`shell.exec`](../../backend/src/huntweave/execution/real.py)），隐藏"提权""第二跳"动作不会让这些操作从表达里消失，[PROJECT §9.1](../../PROJECT.md) 已写明这一限度。能力边界靠 PolicyGate 的准入决策与执行端的可核验事实，不靠注册表或提示词声明。
- **PolicyGate 是新增层**，不是复用：仓库中 `runs/` 只有 `policy_version` 的使用，`contracts/capabilities.py` 的 `ReadinessGate` 是就绪门槛而非准入。因此本决定的实施切片**不是零行为变更**。
- 不新增部署服务：PolicyGate、BudgetManager、ContextBuilder、ToolRegistry、Provisioner 仍是各 Module 的内部实现。
- `engagement_mode` **新增字段**，不复用也不替换既有的 `mode = demonstration | real`（[`ScopeCreate`](../../backend/src/huntweave/contracts/runs.py)）；两者语义不同，必须并存。

## 后果

- 预算粒度按模式区分：coverage 按资产 × 服务分配，breach 按主机分配，统一预留与结算。代码中 `Budget` 当前挂在 Scope 上（Run 级），因此这是政策与归属调整，不是既有机制的开关。
- coverage 的"确认即停"必须由停止规则保证，不交给模型判断，否则长尾资产会被越测越深。
- 本决定不改变 P1 顺序，也不放宽 [ADR-0010](./0010-real-execution-boundary-and-gate.md) 的真实执行就绪门槛。

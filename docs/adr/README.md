# 架构决策索引

当前架构基线为 [PROJECT.md](../../PROJECT.md)。ADR 解释取舍与历史，不要求将不同版本的结论同时实现。当前阶段、已交付能力与下一实施项见 [当前状态](../STATUS.md)；实施规格见 [P0](../specs/0001-foundation.md)、[P1](../specs/0002-real-execution.md)、[P2](../specs/0003-agent-research.md)，评分与准入、能力类判据见 [0004](../specs/0004-finding-admission.md)、[0005](../specs/0005-capability-claim-criteria.md)；实际验证证据见 [验证记录索引](../validation/README.md)。

| ADR | 状态 | 仍然有效的部分 | 已被替代的部分 |
| --- | --- | --- | --- |
| [0001 项目名称](./0001-project-name.md) | 有效 | HuntWeave / huntweave；名称初查有时间和范围限制 | 无 |
| [0002 自主执行与访问](./0002-autonomous-runtime-and-access.md) | 部分历史 | 自主联网获取工具、通用工具优先、全局密钥入口 | 默认 root；按 ADR-0004/0005 和当前总纲执行 |
| [0003 单机技术栈与工具](./0003-single-host-stack-and-tool-design.md) | 部分历史 | 单机优先；协议适配器可选；研究方法由 Agent 选择 | SQLite、两个常驻容器、默认 root |
| [0004 工具生命周期与运行底座](./0004-tool-lifecycle-runtime-and-observability.md) | 部分历史 | PostgreSQL、LangGraph、普通用户/有限提权、透明执行与恢复 | 严格单容器、统一 Debian、动态环境普遍长期保存 |
| [0005 Compose、Kali 与选择性保留](./0005-compose-kali-tool-retention-and-development.md) | 有效 | 三常驻服务、Kali 工具环境、临时/缓存/选择性工具库、下游自行构建 | 无 |
| [0006 Windows Docker 开发入口](./0006-windows-docker-development.md) | 有效 | Windows 工作区、Docker Desktop WSL2 后端；Kali WSL 非依赖；隔离 profile 独立验收 | 总纲早期的原生 Linux 优先路线 |
| [0007 会话网络命名空间](./0007-session-network-namespace.md) | 本机技术验证通过 | 每会话网关、普通工具共享网络栈、独立 PID/文件系统、精确 IPv4/TCP 许可；P1 接入后复验 | 无 |
| [0008 跨平台部署入口](./0008-portable-deployment-helpers.md) | 有效 | Win/Desktop 与 Linux/Engine 共用 Compose 和 Python 入口；Linux 理论可部署、原生验收待完成 | 0006 中仅 Windows 纳入部署目标的阶段范围 |
| [0009 证据驱动的任务规划与单次 Run 持续执行](./0009-adaptive-research-and-continuous-execution.md) | 已采纳，待 P2 实现 | 保留 LangGraph/PostgreSQL/Runner；事实与任务依赖分离、Run 内增量规划、任务上下文、一次发布后持续推进至结束 | 无；补充 0005 与总纲的研究组织，不扩大 P0 |
| [0010 P1 真实执行的宿主、边界与门槛](./0010-real-execution-boundary-and-gate.md) | 已采纳，待 P1 实现 | Windows 单一验收宿主；`lab/` 靶场为唯一验收对象；IPv4/TCP-only 能力边界；`real_execution_ready` 四项就绪门槛 | 无；细化 0006/0007 在 P1 的验收口径 |
| [0011 统一计划提交、持久依赖与证据版本](./0011-planning-authority-and-evidence-revisions.md) | 已采纳，待实施 | runs 统一计划提交、事件规划与持久依赖、历史只读引用、显式证据绑定、版本化复审/报告；核对/回退语义修正 | 细化 0009；0010 的回退须先收尾、就绪持续核验，不静默降级真实 Run |
| [0012 默认最小实证与目标数据保护](./0012-minimal-proof-and-target-data.md) | 已采纳，待实施 | 默认 RCE 最小实证，留证即退出；禁止破坏/删改目标数据，必要新增最小化并披露 | 替代总纲 0.8.4 的 RCE 类别额外许可要求（0.9.1 已按本决定替换），明确不自动删除目标测试记录 |
| [0013 标准评分与独立漏洞库准入](./0013-severity-and-finding-admission.md) | 已采纳，待 P2 实现 | CVSS v4.0 + 独立证据/复审/人工准入；中危以上才有资格，研究与遗留不随过滤消失 | 细化 0011 的结论版本；不引入 SRC 评分 |
| [0014 执行生命周期与环境身份](./0014-execution-lifecycle-and-environment-identity.md) | 已采纳，待分阶段实施 | 会话/实例/调用身份分离、固定环境清单与动态健康分离、慢工作不阻塞控制、独立环境复现 | 细化 0005/0010/0011；不替换运行底座或扩大执行权限 |
| [0015 研究图语义与投影边界](./0015-graph-semantics-and-projection-boundary.md) | 已采纳，待实施 | 三个视角与研究锚点、主张契约三层必需维度、前提门三值状态、尝试与调用分层、归属与统计四记录分离、时间与冻结交付、查询与容量边界 | 细化 0009/0011/0014；不引入图数据库或完整事件溯源，不把画布作为执行依据 |
| [0016 调度、槽位与资源政策](./0016-scheduling-and-resource-policy.md) | 已采纳，待实施 | 槽位分池、同 IP 主动执行并发维持 1、成本归一化与可校准份额、首触优先、复审额度保留、队列可解释性与控制路径负载验收 | 细化 0009/0014；不放宽同 IP 并发，不引入多机调度、读副本或第二套队列 |
| [0017 主张确认、当前有效性与冻结交付](./0017-claim-confirmation-and-frozen-delivery.md) | 已采纳，待实施 | 六维状态、确认等级、lapsed、完整输入准入、有效/保留分离、冻结与兼容迁移；全局与单 IP 物理额度并存 | 细化 0011/0013/0015/0016；替代未知类型一律封顶、确认一律人工、冻结豁免有效期与物理额度仅按 IP 等旧口径 |
| [0018 后端先行、集中前端接入与分层验证](./0018-backend-first-and-layered-validation.md) | 已采纳 | 后端契约与行为先完成并直接验证，前端集中接入后做浏览器验收；单个 Key 入口即时校验 | 细化开发与验证顺序，阶段退出与真实执行开放仍遵循原门槛 |
| [0019 双模式策略与准入层](./0019-engagement-modes-and-policy-gate.md) | 已采纳，待实施 | `engagement_mode` 随策略版本固定；动作注册表/规划器/停止规则三个接缝；PolicyGate 决定实际准入；执行内核不读取模式；证据标准不随模式降低 | 修正"注册表即能力边界"的说法；显式取代初稿"执行内核不改"的表述 |
| [0020 持久会话与最小实证边界](./0020-foothold-session-and-minimal-proof.md) | 已采纳，待实施 | breach 允许平台持有的受管会话、目标侧零驻留；平台连接/平台资源停止/目标侧停止证据/目标新增对象四类事实分别记录；最小实证边界按模式区分 | 修订 [0012](./0012-minimal-proof-and-target-data.md) 的驻留表述与总纲 9.1.1、[0005 C1a](../specs/0005-capability-claim-criteria.md) 的最低证明要求 |
| [0021 范围语义与凭据复用](./0021-scope-semantics-and-credential-reuse.md) | 已采纳，待实施 | 范围语义升级为"主机集合 × 操作类别"；凭据复用仍需逐目标/端口/动作校验，通过后免人工批准；第二跳最终目标由 Runner 校验 | 修订总纲 9.1 的审批边界与凭据使用语义；不改变"不自动扩大目标集合"与"不引入资产测绘" |
| [0022 带外接收端与新传输 profile](./0022-out-of-band-receiver-and-transport-profiles.md) | 已采纳，待实施 | 带外接收端为一等公民、操作员声明；带外确认做成有界调用、反向 shell 走受管会话；UDP/DNS 等新增 profile，不就地放宽 v1；第二跳同受约束 | 补充 [0010](./0010-real-execution-boundary-and-gate.md) 的能力边界；明确非 Web TCP 端口不是新传输能力 |
| [0023 双模式下的研究过程可视化投影](./0023-two-mode-visualization-projections.md) | 已采纳，待实施 | 既有七视图与只读投影不变；新增覆盖矩阵、攻陷状态、缺口视图三个投影，共用一套水位与快照协议 | 扩展 [0015](./0015-graph-semantics-and-projection-boundary.md)；不引入图数据库，不把画布作为执行依据 |

0.7 是当前选型的实施契约整理：六个代码 Module、状态归属、事件顺序、执行收尾与 P0 验收写入总纲，不新增一套部署架构。后续若变更核心选型，新增 ADR 并同步此索引和总纲；普通实现细节与验证结果放对应功能规格。

状态、冻结与迁移的行为来源为 [0006](../specs/0006-state-model-and-delivery.md)，UI 行为来源为 [0007](../specs/0007-research-workbench-ui.md)，能力判据和准入分别见 0005/0004。历史 ADR 保留，不实施相互冲突的旧定义，也不对历史文本全局改名。

0.8/0.8.1 根据 HuntWeave 的覆盖、恢复与自主执行需求补充研究规划，并明确 24×7 属于单次 Run 的持续推进，不更换技术栈；问题与方案、采纳决定、已实现能力分别记录在[架构评估](../research/2026-10-09-architecture-assessment.md)、ADR/总纲及验证记录中。

0.8.2 在 ADR-0009 的同一决定下补齐自动阶段与人工复审的边界、持久等待/唤醒及并发完成判定，不新增模块或部署服务；P2 契约与验收以总纲第 8、12、14 节为准。

0010 在 P1 开工前固定真实执行的验收宿主、能力边界与就绪门槛，不改变 ADR-0006/0007 已验证的 profile 结论，也不提前开放真实执行。

0011 对照当前假执行代码与 P1 Issues 细化规划写入权和证据生命周期；P1 先修执行语义，P2 分片实现自主研究与版本化结论，不增加部署服务。决策依据见 [2026-10-09 架构评估](../research/2026-10-09-architecture-assessment.md)。

0012/0013 根据用户后续明确需求细化验证目的、非破坏边界、测试遗留与结果筛选；执行策略以当前 PROJECT 为准，评分/准入按 [0004 正式规格](../specs/0004-finding-admission.md)，文档采纳不表示功能交付。

0014 细化逻辑会话/容器实例/调用的身份分离、固定环境清单与动态健康的分离，以及准备、执行、停止和证据的分别判断；不引入集群调度、定制容器运行时或快照恢复，不改变普通用户执行与固定 helper 的权限。决策依据见 [2026-10-09 架构评估](../research/2026-10-09-architecture-assessment.md)。

0015/0016 与 [0005 能力类判据规格](../specs/0005-capability-claim-criteria.md) 固定研究过程可视化的语义与调度政策：服务与入口是研究锚点、IP 页面是组合视图，研究图是版本化业务记录的只读投影；主张按契约成立与条目入库分别表达；调度按资源分池与可校准份额实现，同 IP 主动执行并发维持 1。配套验收组见 [P2 规格](../specs/0003-agent-research.md) 第 9 节。0015 对 0011 的补充是明确 as-of 承诺边界（历史能力以版本化业务记录的 as-of 查询为界，不以事件溯源为前提）；对 0014 的补充是把实例重建表达为可见徽章而非新研究节点。三者均为设计文档，不表示能力已实现，也不调整 P1 顺序。

0019–0023 按用户明确要求引入并列的**覆盖面**与**突破**两种测试模式，并把本轮设计评审的修正一并记录：模式差异只限动作声明、规划器与停止规则三个接缝，实际准入由新增的 PolicyGate 决定，执行内核不读取模式但需为能力扩展；持久会话属平台、目标侧零驻留，且平台连接状态与目标侧停止证据分别记录、互不填充；范围语义升级为「主机集合 × 操作类别」，凭据复用仍需逐目标/端口/动作校验；带外接收端为一等公民，UDP/DNS 等新传输面必须新增 profile 而不就地放宽 v1；可视化新增覆盖矩阵与攻陷状态投影，共用既有水位与快照协议。决策依据见 [2026-10-10 双模式架构评估](../research/2026-10-10-two-mode-architecture.md)，行为与验收见 [覆盖面规格](../specs/0008-coverage-mode.md)。均为设计文档，不表示能力已实现，也不调整 P1 顺序与真实执行门槛。

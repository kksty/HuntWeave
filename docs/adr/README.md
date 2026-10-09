# 架构决策索引

当前架构基线为 [PROJECT.md](../../PROJECT.md)。ADR 解释取舍与历史，不要求将不同版本的结论同时实现。当前阶段、已交付能力与下一实施项见 [当前状态](../STATUS.md)；实施规格见 [P0](../specs/0001-foundation.md)、[P1](../specs/0002-real-execution.md)、[P2](../specs/0003-agent-research.md)；实际验证证据见 [验证记录索引](../validation/README.md)。

| ADR | 状态 | 仍然有效的部分 | 已被替代的部分 |
| --- | --- | --- | --- |
| [0001 项目名称](./0001-project-name.md) | 有效 | HuntWeave / huntweave；名称初查有时间和范围限制 | 无 |
| [0002 自主执行与访问](./0002-autonomous-runtime-and-access.md) | 部分历史 | 自主联网获取工具、通用工具优先、全局密钥入口 | 默认 root；按 0004/0005 和当前总纲执行 |
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

0.7 是当前选型的实施契约整理：六个代码 Module、状态归属、事件顺序、执行收尾与 P0 验收写入总纲，不新增一套部署架构。后续若变更核心选型，新增 ADR 并同步此索引和总纲；普通实现细节与验证结果放对应功能规格。

0.8/0.8.1 根据 HuntWeave 的覆盖、恢复与自主执行需求补充研究规划，并明确 24×7 属于单次 Run 的持续推进，不更换技术栈；问题与方案、采纳决定、已实现能力分别记录在[架构改进研究](../research/2026-10-09-architecture-improvement.md)、ADR/总纲及验证记录中。

0.8.2 在 ADR-0009 的同一决定下补齐自动阶段与人工复审的边界、持久等待/唤醒及并发完成判定，不新增模块或部署服务；P2 契约与验收以总纲第 8、12、14 节为准。

0010 在 P1 开工前固定真实执行的验收宿主、能力边界与就绪门槛，不改变 ADR-0006/0007 已验证的 profile 结论，也不提前开放真实执行。

0011 对照当前假执行代码与 P1 Issues 细化规划写入权和证据生命周期；P1 先修执行语义，P2 分片实现自主研究与版本化结论，不增加部署服务。决策依据见 [2026-10-09 架构复核](../research/2026-10-09-architecture-review.md)。

0012/0013 根据用户后续明确需求细化验证目的、非破坏边界、测试遗留与结果筛选；执行策略以当前 PROJECT 为准，评分/准入按 [0004 正式规格](../specs/0004-finding-admission.md)，文档采纳不表示功能交付。

# 当前状态

本文件是**阶段、能力与下一实施项的唯一状态来源**。`README.md` 顶部与 `AGENTS.md` 只指向这里，不再各自复述状态。产品行为与安全边界以 `PROJECT.md` 为准，正式规格见 `docs/specs/`，验证证据见 `docs/validation/`（索引见[验证记录索引](./validation/README.md)）。

更新时机：一个阶段交付验收、能力开关变化、或下一实施项改变时。本文件与代码或验证记录冲突时，以代码和验证记录为准，并修正本文件。

## 阶段

| 阶段 | 范围 | 状态 |
| --- | --- | --- |
| P0 | 运行骨架、认证与授权快照、持久假 Run、隔离靶场验证 | 已有验收记录（`0001`–`0005`）；P0-A 至 P0-D 的实施 Issue 已关闭，记录更正 [#12](https://github.com/kksty/HuntWeave/issues/12) 已人工确认关闭（2026-10-09）；后续缺口见 `docs/validation/0005-p0-execution-and-recovery.md`，不视作全部目标行为已经达成 |
| P1 | Kali 真实执行、选择性保留、透明控制台 | 进行中：正式规格 [0002](./specs/0002-real-execution.md) 已交付，规格 Issue [#14](https://github.com/kksty/HuntWeave/issues/14) 已关闭（2026-10-09）；tracer [#15](https://github.com/kksty/HuntWeave/issues/15) 核对入口（验证记录 `0006`）与①档前置 [#9](https://github.com/kksty/HuntWeave/issues/9) 票据目标绑定（验证记录 `0007`）已交付并关闭；[#13](https://github.com/kksty/HuntWeave/issues/13) 单 Run 目标上限（验证记录 `0008`）与①档前置 [#10](https://github.com/kksty/HuntWeave/issues/10) 能力就绪状态、[#11](https://github.com/kksty/HuntWeave/issues/11) 版本冲突静默重发（验证记录 `0010`）已交付并关闭（2026-10-10）；tracer [#16](https://github.com/kksty/HuntWeave/issues/16) 受信管理组件与真实容器生命周期（验证记录 `0011`）、[#18](https://github.com/kksty/HuntWeave/issues/18) 出口控制与取消/回收（验证记录 `0012`）与 [#17](https://github.com/kksty/HuntWeave/issues/17) 真实动作最小闭环（验证记录 `0013`）已交付并关闭（2026-10-10）；按 2026-10-10 对 #19/#21 验收标准的补充，两条已交付行为事后收紧：调用记录区分「已确认停止」与「未确认停止」（未确认则不释放进程与连接主张，并写 `execution_stop_unconfirmed`），动作的工作目录由 profile 决定并写入调用事件（验证记录 `0014`）；tracer [#19](https://github.com/kksty/HuntWeave/issues/19) 透明控制台已交付并关闭（2026-10-10，验证记录 `0015`）：执行端对每次调用的自述（argv/cwd/执行身份/实例/环境清单/网络模式/网关/授权端点）与时长静默进入调用记录，Run 级容器与网关状态与回收预览成为可读的只读投影（读不到时报观测缺口而不是「没有资源在运行」），证据的截断/脱敏/存储满分支真实可达且归档失败阻断受影响执行，原因码前后端闭合，超限按行反馈且 IPv6 保留自己的原因码，界面分区陈述且不提供单一健康灯或百分比；tracer [#20](https://github.com/kksty/HuntWeave/issues/20) 选择性保留已交付并关闭（2026-10-10，验证记录 `0017`）：一次真实调用结束后的私有工作区进入保留策略而非当场销毁，环境键与候选按去重后的独立 Run 数统计，手动固定/取消固定/删除与回收预览可用（容量不足报阻断而不删受保护材料），删除只走受信管理器的所有权与在用校验且不删证据与清单引用，决定在业务库与受影响 Run 的时间线留痕，P1 不发布共享工具版本；#21 仍开放；真实执行**未开放**：四项门槛未满足，产品拒绝创建真实 Run |
| P2 | 完整 Agent MVP：真实模型、联网研究、Run 内规划、24×7 持续推进；覆盖面模式与覆盖能力 | 实现未开始；[0003](./specs/0003-agent-research.md) 保留整体实施草案；[0004 准入](./specs/0004-finding-admission.md)、[0005 判据](./specs/0005-capability-claim-criteria.md)、[0006 状态与交付](./specs/0006-state-model-and-delivery.md)、[0007 UI](./specs/0007-research-workbench-ui.md)、[0008 覆盖面](./specs/0008-coverage-mode.md) 的设计已确认、未实施/验收；取舍见 ADR-0009/0011/0013/0015/0016/0017 与 ADR-0019–0023（双模式、持久会话与最小实证边界、范围语义与凭据复用、带外接收端与新传输 profile、双模式可视化投影，见[双模式架构评估](./research/2026-10-10-two-mode-architecture.md)）；规格/设计 Issue [#22](https://github.com/kksty/HuntWeave/issues/22) 已关闭（设计交付，未实施），切片 Issue 与 P2 里程碑待开工时建立 |

## 当前能力

Web 入口按单个全局 Key 即时校验，不因错误次数限速、锁定或延迟登录；服务端会话与业务鉴权继续提供基础访问控制。本轮改动与控制 API 响应修复见 [#23](https://github.com/kksty/HuntWeave/issues/23) 和 [验证记录 0016](./validation/0016-development-validation-feedback.md)。开发与验证顺序按 [ADR-0018](./adr/0018-backend-first-and-layered-validation.md) 执行。

已交付：服务端会话与授权快照、持久 Run 与排队状态机、LangGraph 角色闭环（确定性模型 Adapter）、持久假 Runner 账本与租约、原始证据归档与哈希校验、有序事件与 SSE 时间线、暂停/取消/恢复预览/重启对账、`unknown` 调用的操作员核对入口（三种裁定、受限结束、带证据重派）、执行票据按该动作的实际目标绑定并取用授权快照的策略版本、由执行端按 ADR-0010 四项门槛计算并在界面标注的能力状态（含 `execution_ready` 随执行端观测变化）、控制台在 `version_conflict` 后先展示新状态再由操作员显式确认、Runner 内的受信沙箱管理组件（收窄的固定 Docker 操作集、由提交进仓库的固定 profile 决定容器选项、实例身份与创建中断记录、按标签回收无残留、管理能力的部署启用状态作为独立事实上报）、受网关强制的出口控制（默认拒绝、规则先于工具进程、只放行授权 IPv4/TCP、平台网络与桥接宿主地址及元数据被拒、IPv6 关闭、规则回读核验）、实例级取消/超时/撤销与控制租约到期回收（先断出口再停容器并确认无残留进程）、按顺序执行的回退序列（停新增 → 撤销并回收 → 对账归档 → 允许撤除管理能力，未核清则阻断）、版本化动作与执行 profile 契约（每动作自带参数 Schema/超时/summary 字段，跨 profile 票据被拒）、真假执行端共用的调用账本与按 profile 分派的同一 Runner 表面，以及三个真实动作（`shell.exec`、`discover_tcp_services`、`probe_http`）经票据在靶场执行并按真实返回归档证据、驱动后续动作、选择性保留（调用结束后的私有工作区进入 TTL/容量/固定策略，环境键与候选按去重后的独立 Run 数统计，手动固定/取消固定/删除与回收预览可用，删除按所有权与在用校验执行且不删证据与清单引用，操作员的决定在业务库与受影响 Run 的时间线留痕，`shared_tool_version` 恒为 false）。

已知限制：确定性 Adapter 的**演示**分支仍按预置场景而不是工具返回分支（真实分支已按执行端上报的 summary 决策，见 `0013`），当前每角色固定一项任务，尚无 Worker × N 的研究闭环；完整 Finding/Reviewer 报告和持久依赖尚未实现。核对入口的停止确认与观测事实由假执行账本提供；真实执行端亦已按受信管理器的实例事实上报「已确认/未确认停止」（未确认时进程与连接报为未知，见验证记录 `0014`），但控制面与界面尚未消费该区分（容量只在受信停止事实上释放归 [#21](https://github.com/kksty/HuntWeave/issues/21)；界面不得显示已回收的部分已随 [#19](https://github.com/kksty/HuntWeave/issues/19) 交付，见验证记录 `0015`）。`确认未执行` 的重派只有受信记录证明动作未开始且旧租约失效时可用，否则明确拒绝。未命名目标按调用序号在授权端点集内轮转属确定性 Adapter 期的暂定行为，端到端复核仍待真实 Run 开放后进行（[#17](https://github.com/kksty/HuntWeave/issues/17) 已交付的部分见验证记录 `0013`）；逐调用目标展示与 `scope_denied` 中文文案已随 [#19](https://github.com/kksty/HuntWeave/issues/19) 交付（验证记录 `0015`）。每 IP 串行、调度公平与并发上限归 #21。目标上限只在预览路径校验：`#13` 提交前按旧上限（5000）建立的授权快照不被回溯设限，仍可创建超过 100 个目标的 Run，封堵属新行为（需另开切片）；超限输入的行级反馈与 IPv6 计入额度导致的先报 `target_limit_exceeded` 已随 [#19](https://github.com/kksty/HuntWeave/issues/19) 交付。能力状态的两项构建门槛（契约可表达未就绪、控制台消费能力接口）由执行端读取契约模型与所服务的控制台构建产物判定，`profile_revalidation` 与 `deployment_revert` 仍按代码内事实报未满足；能力观测有 5 秒展示缓存，派发路径不使用它——每次调用仍直接提交给执行端并由其账本结算（见验证记录 `0010`）。**真实执行的应用层路径未被端到端验证**：创建真实 Run 需要四项门槛逐项满足，而门槛 1、4 仍为假，产品因此拒绝创建（这本身是验收标准 5/6 的行为）；真实动作的执行路径由靶场探针 `0013` 覆盖，控制面（profile 选择、就绪挂起、按 profile 构造票据）由单元检查覆盖，两者不构成一次完整的应用层真实 Run。真实动作的 argv 由产品代码固定（`sh -c` 与固定 `python -c` 脚本，工具镜像须提供 `python`），命令超时由适配器用容器内 `timeout` 包裹——这两点只由适配器实现与靶场实测支持。出口保护集合是固定范围 + 平台自身网络 + 所用桥的宿主地址，部署额外的宿主网段需在 profile 的 `network.protected` 声明；连接回收以对端观察到的关闭为准，未读 conntrack。生命周期与出口 profile 用 lab 探针镜像承担两个角色、工具清单为空，Kali 固定 digest 工具镜像尚未接入；镜像引用当前是 tag，解析出的 digest 记入实例清单。管理能力默认关闭，只有显式 `deploy/compose.sandbox.yaml` 启用后 Runner 才获得 socket（并需 `group_add: ["0"]`，本机 socket 为 `root:root 0660` 而 Runner 以 uid 10001 运行）；`execution/dockerruntime.py` 没有容器外的检查覆盖（见验证记录 `0011`、`0012`、`0013`）。**选择性保留的边界**：制品体积取自容器引擎的卷用量报告（`/system/df`，Runner 只保留本项目账本已命名的卷），引擎不报告时该制品显示为未测量并单独计数，不按 0 计入容量（容量结论因此只对已测量的字节作答）；策略的四个数值由 `HUNTWEAVE_RETENTION_*` 部署环境决定，app 与 Runner 需配置同一组值，降级视图的 `limits` 也是本进程读到的策略；无人值守回收默认每 300 秒一次（`HUNTWEAVE_RETENTION_SWEEP_SECONDS`，`0` 关闭），该间隔只在单元检查里以 1 秒验证行为，长时节奏未验证；TTL 到期分支由注入时间的单元检查覆盖，靶场探针覆盖容量阻断、固定保护与显式删除（真实时间尺度内无法等待 7 天）；回退不删除保留制品——它们仍在保留账本与实例账本的命名中并写进回退对账归档，撤除管理能力后需重新启用管理才能回收；「活跃任务/复审租约」在 P1 的实现口径是「未确认停止的实例仍持有该卷」（P1 没有复审租约与会话级共享环境，P2 引入后须改查任务/租约登记）；保留视图是部署级事实，控制台区块挂在 Run 页，环境/工具设置页尚未建立。以上取舍见验证记录 `0017`。

以上状态于 2026-10-10 对照源码、验证记录与 GitHub Issues 核实。

**真实执行未开放。** `fake_execution_ready=true` 只表示固定假动作链路就绪；`real_execution_ready=false`，原因仍取 ADR-0010 的 P1 结论 `environment_unsupported`，四项门槛的逐项状态随能力响应返回：契约可表达与控制台消费已满足，profile 复验与回退入口未满足（原因码分别为 `profile_unvalidated`、`revert_path_missing`）。管理能力另行表达：默认部署为 `sandbox_management=disabled`，显式启用后为 `ready`（或不可用时带原因码），两者都不改变 `real_execution_ready`。界面与响应标注“开发演示 / 假执行”，执行端不可达时标注不可用而不是就绪；管理能力的界面展示已随 [#19](https://github.com/kksty/HuntWeave/issues/19) 交付（验证记录 `0015`）。

每会话网关隔离 profile（IPv4/TCP）已在本地靶场验证，尚未接入产品 Runner；默认 Compose 的 Runner 不挂 Docker socket。

## 下一实施项（P1）

规格：`docs/specs/0002-real-execution.md`。边界与门槛见 [ADR-0010](./adr/0010-real-execution-boundary-and-gate.md)。实施顺序与逐条完成证据见规格第 5 节；规格 Issue [#14](https://github.com/kksty/HuntWeave/issues/14) 已交付关闭（其正文的「退出条件」已移交下列里程碑与本文件），实施不再挂在该 Issue 上。

GitHub 侧的切片进度见[里程碑 P1](https://github.com/kksty/HuntWeave/milestone/1)（成员为 #10、#11、#13、#16 与 #17–#21，其中已关闭成员保留；满足规格第 5 节末段的退出条件时关闭）。里程碑只表达切片进度，阶段状态仍以本文件为准。

前置修复（①档，阻塞首批 tracer）：

1. ~~[#9](https://github.com/kksty/HuntWeave/issues/9) 执行票据写死目标绑定~~ 已交付并关闭（2026-10-09：票据按计划的实际目标绑定、越界计划在占用预算前以 `scope_denied` 拒绝、`policy_version` 取自授权快照；提交 `656ab22`，证据见 `docs/validation/0007-p1-ticket-binding.md`）。
2. ~~[#13](https://github.com/kksty/HuntWeave/issues/13) 单 Run 目标上限文档 100 与实现 5000 不一致~~ 已交付并关闭（2026-10-09：上限收紧为 `TARGET_LIMIT = 100`，`target_limit_exceeded` 原因码与状态码不变，README、`PROJECT.md` §12 与实现三处对齐；提交 `c1a4ec2`（检查命名与断言调整 `7321082`），证据见 `docs/validation/0008-p1-target-limit.md`）。验证记录的 6 项待确认已按结案条款处置：2 项顺延 [#19](https://github.com/kksty/HuntWeave/issues/19)、1 项记入本文件已知限制，其余接受现状，去向见该 Issue 的关闭评论。这是 [#16](https://github.com/kksty/HuntWeave/issues/16) 原生阻塞边的已决部分，关闭后 #16 进入可执行前沿。
3. ~~[#10](https://github.com/kksty/HuntWeave/issues/10) 能力就绪状态只在 API 层、[#11](https://github.com/kksty/HuntWeave/issues/11) 控制台在 `version_conflict` 后静默重发~~ 已交付并关闭（2026-10-10：能力契约改为可表达未就绪且原因码可空，`real_execution_ready` 由 ADR-0010 四项门槛计算（两项构建门槛读取契约模型与所服务的控制台构建产物），控制台消费 `/api/v1/system/capabilities` 并在页面标注执行模式与未逐项满足的门槛，`RunView.execution_ready` 随执行端观测变化、不可达时为 false；控制台在 `version_conflict` 后展示新状态并等待操作员显式确认，不再静默重发；证据见 `docs/validation/0010-p1-capability-and-control-conflict.md`）。三项①档前置至此全部关闭，#16 的阻塞边不受影响。

tracer 顺序（实现切片）：

1. ~~[#15](https://github.com/kksty/HuntWeave/issues/15) `unknown` 调用的操作员核对入口~~ 已交付并关闭（2026-10-09，实现与证据见 `docs/validation/0006-p1-reconciliation.md`）：用现有假执行账本验收，不引入真实容器。
2. ~~[#16](https://github.com/kksty/HuntWeave/issues/16) 受信管理组件与真实容器生命周期~~ 已交付并关闭（2026-10-10，证据见 `docs/validation/0011-p1-sandbox-lifecycle.md`）：Runner 内受信管理组件把 Docker 可达面收窄为固定操作集，容器选项全部来自提交进仓库的固定 profile，实例身份/创建中断/按标签回收与默认部署无管理能力均经探针与检查验证；无目标网络、未接真实动作派发，`PROFILE_REVALIDATED` 仍为假。
3. ~~[#18](https://github.com/kksty/HuntWeave/issues/18) 出口控制与取消/回收~~ 已交付并关闭（2026-10-10，证据见 `docs/validation/0012-p1-egress-and-cancel.md`）：出口由网关强制且规则先于工具进程（就绪检查后才放行）、只放行授权 IPv4/TCP 并回读核验、平台网络/桥接宿主地址/元数据被拒、IPv6 关闭、撤销在 profile 时限内生效并记录时序、取消与租约到期后确认进程与连接回收、回退按顺序执行且未核清时阻断；真实调用派发与操作员入口分别归 #17/#19。
4. ~~[#17](https://github.com/kksty/HuntWeave/issues/17) 真实动作最小闭环~~ 已交付并关闭（2026-10-10，证据见 `docs/validation/0013-p1-real-actions.md`）：动作与执行 profile 版本化、真假执行端共用调用账本、三个真实动作（`shell.exec`/`discover_tcp_services`/`probe_http`）经票据在靶场跑通并归档证据、Run 的执行模式与就绪门槛、按真实返回驱动后续动作；产品仍拒绝创建真实 Run，因为四项门槛未逐项满足。
5. ~~[#19](https://github.com/kksty/HuntWeave/issues/19) 透明控制台~~ 已交付并关闭（2026-10-10，证据见 `docs/validation/0015-p1-transparent-console.md`）：只读六项 + 待核对调用区块 + 会话容器与网关状态及回收确认全部落地——执行端在调用上自述 argv/cwd/执行身份/实例/环境清单/网络模式/网关/授权端点与时长静默，Run 级容器与网关由受信管理器投影成只读视图（读不到时报观测缺口），证据的截断/脱敏/存储满分支真实可达且归档失败阻断受影响执行，原因码前后端闭合（机械检查防漂移），超限按行反馈且 IPv6 保留自己的原因码，界面分区陈述且无单一健康灯或百分比。实施中顺带修复三处既有缺陷：`BUSINESS_REVISION` 未随 #17 的迁移同步（就绪门槛自 #17 起一直失败）、证据接口因未声明字段返回 500、调用结算后执行端迟到的停止确认不落库。
6. ~~[#20](https://github.com/kksty/HuntWeave/issues/20) 选择性保留：统计候选 + 手动固定/删除 + 回收预览，不自动发布~~ 已交付并关闭（2026-10-10，证据见 `docs/validation/0017-p1-selective-retention.md`）：候选按去重后的独立 Run 数统计（同一 Run 的重试只增加调用数），固定项与实例仍在持有的制品不被自动回收，容量压力下按最久未保留优先且只在可回收项内选择、不足时报阻断与差额，删除走受信管理器的按名读回与所有权校验（非本项目所有报 `ownership_mismatch`、实例仍持有报 `retention_artifact_in_use`）、删除一次性环境不删证据与清单引用，操作员的每个决定在业务库与受影响 Run 的时间线留痕，`shared_tool_version` 恒为 false 且没有发布入口。实施中把「调用结束即销毁私有工作区」改为「进入保留策略」：`reclaim_instance` 增加保留卷的路径，实例账本新增 `retained`，回退对账归档同时列出保留制品。
7. [#21](https://github.com/kksty/HuntWeave/issues/21) 多活跃 Run 并发压力验收：作为最后一个 tracer 的验收项（现处可执行前沿）。

2026-10-09 架构修订已写入 P1 规格：核对不等于成功/回收、真实 Run 不切换为假执行、回退先收尾、运行中持续校验能力、手动保留不共享目标可写层；基础公平/取消检查随 #17/#18 先验。这些修订已于 2026-10-09 同步到 GitHub Issue 正文与原生依赖（见规格第 5 节末段）。其中**核对与执行端停止确认分离、受限结束**已由 #15 交付（`0006`）、**实例身份与创建中断残留核对**已由 #16 交付（`0011`）；**真实 Run 不切换为假执行、回退先收尾、运行中持续校验能力**已随 #17/#18 交付（`0012`、`0013`），**手动保留不共享目标可写层**已随 #20 交付（`0017`）；**基础公平/取消检查仍随 #21 验收**；实施顺序以规格第 5 节表格与原生阻塞边为准，不据旧 Issue 正文的依赖清单跳步。

## P2 的开工约束

本轮已完成文档整理：Phase 0 来源盘点、映射与兼容读要求见 [0006 §10](./specs/0006-state-model-and-delivery.md#10-phase-0映射与兼容策略)，工作台交互见 [0007](./specs/0007-research-workbench-ui.md)。这只锁定设计，不表示迁移、六轴模型、研究图、复审队列或冻结交付已实现；P1 的当前进度与顺序不因文档整理改变。

按 [P2 规格](./specs/0003-agent-research.md) 的 A–F 切片推进：事务外模型与独立任务身份 → 持久事件规划/依赖 → 真模型与洁净准备 → 证据绑定/独立复审/版本化报告 → 持续运行与恢复验收；研究图基础可视化（F）为独立切片，可与上述切片并行，不等待完整闭环。不新增常驻 Planner、图数据库或跨 Run 可变黑板；P1 不因这些设计推迟。P2 规格/设计 Issue [#22](https://github.com/kksty/HuntWeave/issues/22) 已关闭（交付物为设计本身）；实施切片 Issue（Phase 0 与 A–F）与 P2 里程碑待开工时按 `0003 §8` 建立，每个切片开工前须完成实施评审。本轮仅完成架构文档。主张与能力类判据按 [0005 规格](./specs/0005-capability-claim-criteria.md)，验收组见 [P2 规格](./specs/0003-agent-research.md) 第 9 节。

2026-10-10 引入并列的覆盖面与突破两种测试模式（[ADR-0019](./adr/0019-engagement-modes-and-policy-gate.md)–[ADR-0023](./adr/0023-two-mode-visualization-projections.md)、[覆盖面规格 0008](./specs/0008-coverage-mode.md)、评估见[双模式架构评估](./research/2026-10-10-two-mode-architecture.md)），设计交付为 [#24](https://github.com/kksty/HuntWeave/issues/24)。覆盖面按 `0008 §7` 的 C-A…C-F 切片推进，已建立切片 Issue：[#25](https://github.com/kksty/HuntWeave/issues/25) 模式骨架与准入层 → [#26](https://github.com/kksty/HuntWeave/issues/26) 有界非 Web 服务交互与能力缺口记账 → [#27](https://github.com/kksty/HuntWeave/issues/27) 带外接收端与新传输 profile → [#28](https://github.com/kksty/HuntWeave/issues/28) 分流聚合与覆盖分母 → [#29](https://github.com/kksty/HuntWeave/issues/29) 覆盖矩阵与缺口视图；[#30](https://github.com/kksty/HuntWeave/issues/30) 批量导入独立于模式。**突破模式只有设计决定与待切片的[#31](https://github.com/kksty/HuntWeave/issues/31)**（持久会话、范围语义与第二跳），[#32](https://github.com/kksty/HuntWeave/issues/32) 为规模化瓶颈实测。切片 Issue 尚未挂里程碑：P1（[里程碑 1](https://github.com/kksty/HuntWeave/milestone/1)）仍在进行，P2 里程碑按本文件约定在开工时建立。设计未实现，P1 顺序与真实执行门槛不变。

**P2-C（真模型、联网研究与洁净动态准备）的开工前置**除实施评审外，还包括确定**方法记录的字段形态与提示词分段**。按 [PROJECT §10.1](../PROJECT.md) 0.9.11，这两项在有真实模型循环的观察之前**不预设 schema，也不单独立规格**；当前只固定与实现无关的约束（不内置检查表与路线剧本、方法知识按需检索且检索不到不阻断研究、方法记录是产出而非输入、不得自动加载为插件）。判据与理由见[开发顺序与验证边界评估](./research/2026-10-10-development-and-validation-boundaries.md)。此处不另建 Issue：它不是当前可执行前沿的阻塞项，随 P2-C 的实施评审办理。

后续用户要求已纳入设计：默认 RCE 最小只读实证、成功退出、禁止破坏/删改目标数据、必要新增及大量脏数据披露（ADR-0012）；CVSS v4.0 评分与正式漏洞库准入（[0004 规格](./specs/0004-finding-admission.md)、ADR-0013）。低危/无害和仅版本命中不入库，未决项与测试遗留继续展示。以上均未实现，须随 P2-C/D 关联实施任务；不扩大当前执行能力。

## 未开放能力

2026-10-09 文档补充：[ADR-0014](./adr/0014-execution-lifecycle-and-environment-identity.md) 细化实例身份、环境清单、动态健康与控制路径，已映射到 P1/P2 对应切片；推导依据见[架构评估](./research/2026-10-09-architecture-assessment.md)。该细化的实例身份与创建中断部分已随 #16 交付（`0011`）；其余随 #17–#21 推进，各切片的实际交付情况见验证记录。

搜索与联网研究、真实模型、动态安装、选择性工具库的自动发布（P1 只做候选统计与手动处置，见 `0017`）、完整 Finding/Reviewer 报告、Run 内规划、24×7 持续推进。不要按已实现对待。真实执行**未开放**：四项门槛未逐项满足，产品因此拒绝创建真实 Run；三个真实动作只能在靶场经 Runner 表面执行（`0013` 的探针），操作员界面（执行模式标注、停止中/待核对、一键回退）已随 #19 交付（验证记录 `0015`）。

双模式相关能力同样**未实现**：`engagement_mode` 字段、准入层（PolicyGate）、按模式过滤的动作注册表与停止规则、按资产×服务与按主机的预算归属、有界非 Web 服务交互（`probe_service`）、带外接收端与有界确认调用、能力缺口记账与覆盖分母口径、覆盖矩阵与攻陷状态投影、批量导入（单 Run 上限、CIDR 展开预览、跨 Run 资产台账）。突破模式的持久会话、凭据复用校验与第二跳可达性只有设计决定，没有实施切片。**注意 `engagement_mode` 与既有的 `mode = demonstration / real` 是两个独立字段，不得复用一个。**

视觉语言约束已确定（[ADR-0024](./adr/0024-nasa-punk-visual-language.md)、[视觉设计系统 0009](./specs/0009-visual-design-system.md)），前端换肤**未实施**：当前控制台仍是绿调浅色、圆角卡片与 Inter 字体，且**没有设计 token 层**，色值散落在 `frontend/src/style.css` 与 `RunConsole.vue`。切片 Issue：[#33](https://github.com/kksty/HuntWeave/issues/33) V-A token 层与字体自托管 → [#34](https://github.com/kksty/HuntWeave/issues/34) V-B 既有控制台换肤（依赖 #33）；[#35](https://github.com/kksty/HuntWeave/issues/35) V-D 模式标注与覆盖状态视图（依赖 #33 与 #25）。V-A/V-B 不依赖双模式后端。前端变更触发浏览器验收（[ADR-0018](./adr/0018-backend-first-and-layered-validation.md)）。

术语已于 2026-10-10 统一：原「玻璃鱼缸」改称**执行可观测性**，界面称**透明控制台**（[GLOSSARY](./../GLOSSARY.md)）。**文档已改名，界面文案未改名**——`frontend/src/RunConsole.vue` 的 aria-label 与标题、`frontend/tests/authorized-run.spec.ts` 的断言仍用旧称，随 [#34](https://github.com/kksty/HuntWeave/issues/34) 一并改并跑浏览器验收；历史 ADR 与验证记录按既有约定保留旧称。

研究过程可视化与调度政策的设计已确定（[ADR-0015](./adr/0015-graph-semantics-and-projection-boundary.md)、[ADR-0016](./adr/0016-scheduling-and-resource-policy.md) 与 [0005 能力类判据规格](./specs/0005-capability-claim-criteria.md)），但能力同样未实现：研究图页面与图投影查询、主张契约与前提门判据、槽位分池与公平调度、队列解释面板均未开工。这些设计不改变上述 P1 切片顺序，也不表示任何验收已经完成；其验收要求见 [P2 规格](./specs/0003-agent-research.md) 第 9 节，实施归属见 ADR-0015/0016 各节的实施归属。

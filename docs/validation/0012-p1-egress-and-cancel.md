# P1 #18 出口控制与取消/回收：靶场验证

日期：2026-10-10（Asia/Shanghai）。对应 Issue [#18](https://github.com/kksty/HuntWeave/issues/18)，依据 P1 规格 [第 3.1、3.4、3.5、3.7 节](./../specs/0002-real-execution.md)、第 5 节 tracer 顺序与 [ADR-0010](./../adr/0010-real-execution-boundary-and-gate.md)、[ADR-0014](./../adr/0014-execution-lifecycle-and-environment-identity.md)。本轮**仍不开放真实执行**：真实调用尚未接入派发（#17），四项门槛未变（`PROFILE_REVALIDATED`、`REVERT_ENTRY_AVAILABLE` 仍为假）。

## 问题与判定

- **出口由工具容器外的网关强制，且规则先于进程**。裁决：网关的启动命令自己安装默认拒绝规则，并以健康检查报告就绪；管理器等它就绪后才安装授权放行并创建工具容器。放行规则由提交进仓库的 profile 命令安装（只有规则体随请求走，程序与执行用户都来自 profile），安装后**回读内核规则**并要求与授权集合完全一致，不一致即结束执行（`sandbox_egress_unverified`）。
- **只放行授权目标，平台地址默认拒绝**。裁决：保护集合由三处构成——profile 声明的范围、管理器从自身容器发现的平台网络、会话桥与目标桥的宿主侧地址（会话子网整体也受保护）；授权里出现这些地址时在**创建任何容器之前**以 `scope_denied` 拒绝，无法表达为 IPv4/TCP 的地址以 `endpoint_not_expressible` 拒绝；IPv6 在会话命名空间内显式关闭。
- **取消/超时/撤销先断出口，再停容器并确认停止**。裁决：`halt_instance` 先撤销许可、再停容器、再确认；停止确认要求容器不再运行**且其中没有残留进程**，否则报 `sandbox_stop_unconfirmed` 而不得写成已停止；停止后实例不再持有许可与租约。
- **控制租约到期由管理器自己收尾**。裁决：实例可持有控制租约（上限 15 秒），看门狗每 0.5 秒检查一次，租约到期即撤销许可并停止，记 `control_lease_expired`；续租前重新核验执行端可达与内核规则仍是已验证的那一份，核验不过即拒绝续租并停止。
- **回退按顺序执行且未核清时阻断**。裁决：`begin_revert` 先拒绝新实例（`sandbox_reverting`）→ 逐个撤销许可 → 停止 → 回收 → 对账（`audit` 的未记账与丢失资源）→ **把这次对账归档到证据目录**；仅当没有遗留项时报告 `withdrew=true`，否则 `state=blocked` 且保留管理能力。重复请求是幂等的空操作。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `execution/sandboxprofile.py` | 新增 `network.target_network`（网关连接的目标网络，null 表示该 profile 无目标出口）、`network.protected`（部署声明的额外保护范围）、`egress.policy_command`/`read_command`/`policy_user`（固定程序与执行用户，必须非空且与 gateway 角色同用户）、角色 `healthcheck`（gateway 必须有，且必须是 `CMD`/`CMD-SHELL`）、`limits.readiness_seconds` 与 `revocation_seconds` |
| `execution/sandbox.py` | 启动顺序改为：建卷 → 建会话网络 → 建网关 → 连目标网络 → 启动网关 → **等就绪** → 构建含桥接宿主地址的保护集 → 安装放行 → 回读核验 → 建并启动工具 → 核验实例事实。新增 `authorize_egress`（范围变更）/`revoke_egress`/`halt_instance`/`renew_instance_lease`/`begin_revert`、`EgressState` 与 `EgressChange` 时序、`egress_observation`（核验失败时留存实际规则）、看门狗（租约到期 + 每 2 秒复核内核规则，漂移即 `egress_unverified` 停止）；停止会清空许可与租约，回收按标签并二次核对名字前缀 |
| `execution/dockerruntime.py` | 新增收窄操作：`connect_network`、`network_facts`、`own_networks`（从 Runner 容器自身发现平台网络）、`apply_gateway_policy`/`read_gateway_policy`（只运行 profile 里的程序，且容器必须带本项目标签，未配置程序则什么都不运行）；`ContainerFacts` 增加 `health`，`NetworkFacts` 增加 `gateway`；把 profile 的 readiness 命令翻译成容器健康检查 |
| `execution/durable.py` | `atomic_write` 改为每个写入者使用唯一的临时文件名：管理器有调用线程与看门狗两个写入者，本轮实测到它们争用同一临时文件（见下） |
| `profiles/sandbox-egress-v1.json`（新） | 出口验收用的固定 profile：声明目标网络 `huntweave-lab-egress-targets`、固定策略命令、就绪检查与时限；`sandbox-lifecycle-v1.json` 同步补齐新字段（`target_network: null`，只有默认拒绝） |
| `lab/isolation/egress.py`、`deploy/verify_egress.py`（新） | 出口探针与入口：探针容器自己坐落在控制桥上（管理器从中发现平台网络），创建授权/未授权/控制三个靶场固定回显容器，驱动产品管理器完成默认拒绝、放行、撤销、收缩、取消、租约到期与回退；管理器之外只用 Docker SDK 读事实 |
| `backend/tests/test_sandbox_lifecycle.py` | 66 项：新增规则先于进程（按调用顺序与进程启动时间两种口径）、就绪未达成即结束、策略内容与保护集、基础设施地址与不可表达地址的拒绝方式、平台网络不可识别即拒绝、收缩/撤销/停止顺序、停止与租约清空、租约上限与到期自停、回退四种情形与归档、策略未被真正安装即结束、内核规则漂移即停止、适配器拒绝在非本项目容器里运行程序、整轮生命周期只使用固定操作集 |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.7.2）、宿主 Python 3.14.5、`backend/.venv` Python 3.12。三份靶场报告都在提交 `e1efeca` 的工作树上取得（`source_tree_dirty=true`，即本切片提交前），记录末尾附提交后复跑结论。

| 行为 | 结果 |
| --- | --- |
| 出口与取消验收 | `python deploy/verify_egress.py` → **30 项检查全部通过**，`leftovers: 0`（探针自己的 5 个 fixture/桥在结束时移除）；报告 `runtime/sandbox/66ccfc328d344cdc9d2ba1565d6ece71/report.json`，sha256 `3ecd99b545e536932827067f9390cf472afb245f2730cfbad404bf0c03d188a5` |
| 规则先于进程 | 守护进程时间戳对照管理器记录：网关启动 `02:27:44.666` ≤ 策略应用 `02:27:45.587` ≤ 工具容器创建/启动 `02:27:45.807`（`the_policy_was_applied_before_the_tool_container_existed` 与 `..._before_the_tool_process_started` 均通过；后者用 `State.StartedAt`，即进程启动时刻） |
| 就绪与默认拒绝 | `the_gateway_reported_ready_before_the_tool_started`（健康检查 `healthy`）、`the_gateway_holds_default_deny_plus_exactly_the_authorized_endpoints`（`INPUT/OUTPUT/FORWARD DROP`，恰好 4 条放行＝2 个授权端点×2 个方向）、`the_protected_ranges_are_dropped_explicitly` |
| 范围与平台保护 | `the_authorized_endpoint_is_reachable`、`an_unauthorized_target_is_not_reachable`、`a_port_outside_the_authorization_is_not_reachable`、`the_control_network_is_not_reachable`、`the_metadata_address_is_not_reachable`、`the_host_side_of_each_bridge_is_not_reachable`（目标桥 `172.26.0.1` 与会话桥 `172.27.0.1` 均不可达）、`ipv6_is_disabled_in_the_session_namespace`、`the_tool_cannot_widen_its_own_boundary` |
| 撤销时序 | `an_established_connection_is_revoked_within_the_bound`：已建立的流在 **1.385 秒**内结束（profile 上限 5 秒），记录 `revoked_at` 与原因 `scope_revoked`；`no_new_connection_is_possible_after_the_revocation`；`a_narrower_scope_leaves_only_the_remaining_endpoint`（收缩后仅保留 7000，7001 立即失效） |
| 取消与回收 | `descendants_exist_before_the_cancel`（4 个进程，含脱离进程组的孤儿）、`the_cancel_confirms_the_stop`、`cancelling_reclaims_the_pid_namespace_and_the_connections`（工具、网关与一个**共享其 PID 命名空间的独立观察容器**全部停止） |
| 租约到期 | `the_rebuild_reaches_its_target`、`a_lapsed_control_lease_is_closed_by_the_manager`（无人续租，管理器自行停止，`halt_reason=control_lease_expired`）、`the_expired_execution_is_stopped_and_its_egress_revoked` |
| 回退 | `the_revert_sequence_completes_and_allows_withdrawal`（`state=complete`、`withdrew=true`）、`the_revert_archives_what_it_reconciled`（`reverts/operator_revert-98420b20.json`）、`a_revert_leaves_nothing_behind`（管理器视图、标签查询与对账三处皆空）、`repeating_the_revert_is_a_no_op` |
| 生命周期探针未回归 | `python deploy/verify_lifecycle.py` → **29 项全部通过**、`leftovers: 0`；报告 `runtime/sandbox/3b06bf9dbdab40b19eecfc6cc5655dd3/report.json`，sha256 `ecc6fda8ef1e3e7924925106ff8b47e4d1d4acce9308349c4b7677f8bced1159`（探针入口改为带一张控制桥：管理器现在从自身容器发现平台网络并拒绝在无法识别时启动） |
| 隔离探针未回归 | `python deploy/verify_isolation.py` → **40 项全部通过**、`cleanup_errors: 0`；报告 `runtime/isolation/a0bd498f424946acb896670b258db8fb/report.json`，sha256 `897314edaad8612f1620da369b5ecdd5200aff47d2a6242179f9ac6a56328c34`（lab 镜像新增 `egress` 目标后复跑） |
| 本地纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 41 个源文件上无问题；`python -m pytest -m "not integration" -q` → **148 项通过、4 项跳过、38 项按标记排除**（`0011` 为 123 项通过） |
| 两轴代码审查 | 对本轮改动跑 Standards 与 Spec 两轴（并行子代理，对照 AGENTS/PROJECT/GLOSSARY、规格第 3/5/6 节、ADR-0010/0014 与 Issue #18 正文）。**采纳并修复**：新增了 exec 类操作后补上「容器必须带本项目标签」与「未配置程序则什么都不运行」两道闸，并把策略命令与执行用户收进 profile 的 `egress`（此前 `user="0:0"` 写死在适配器）；把「源码 grep 断言固定操作集」改为断言整轮生命周期里向运行时发出的调用都属于固定集合；`revocation_seconds` 改为真正生效（回读核验的时限就是它）；`renew_instance_lease` 续租前重新核验执行端可达与内核规则；停止与回退都会清空许可；回退补上对账归档；桥接宿主地址与会话子网纳入保护；`halt_reason` 改用 `HaltReason`；`_network_subnet`/`_network_internal` 合并为一个 `_network_facts`；镜像里无程序可运行与容器不属本项目各有独立检查。**未采纳**：把请求改成携带票据（归 #17）、把探针的判读逻辑改成不读 daemon 时间戳（daemon 时间戳正是独立证据）。 |
| 一项发现（并发缺陷） | 复跑时出现一次 `/results/state/sandboxes.pending -> sandboxes.json` 的 `FileNotFoundError`：总线（调用线程）与看门狗线程同时落盘争用同一临时文件。修法是看门狗取同一把锁，并让 `atomic_write` 每个写入者用唯一临时名。此前那次「策略核验失败」的偶发停止也是同一竞态的表现 |

## 未达成与限制

- **真实调用仍未接入**：本轮把取消/超时/撤销/租约到期都做成**实例级**事实（`halt_instance` 与看门狗），驱动它们的是靶场探针与管理器自己；控制面把某次真实 `ToolCall` 的取消映射到它的实例仍归 [#17](https://github.com/kksty/HuntWeave/issues/17)。因此「界面在此之前显示停止中/待核对」在本轮只有**事实基础**（停止确认与执行端观测分开记录，假执行路径见 `0006`），真实调用的界面呈现归 #17/#19。
- **`ToolCall` 状态机仍缺 `cancelling` 与 `denied`**（规格第 4 节②档）：取消已派发调用时，调用状态仍直接从 `dispatched` 进入 `cancelled`，未执行被拒的动作也未记为 `denied`。本轮未改控制面状态机，该项顺延到真实派发切片 #17（届时才有真实的取消与拒绝路径可验收），已在 Issue 关闭说明中注明。
- **回退没有操作员入口**：`begin_revert` 已按顺序实现并覆盖「正在执行、停止确认丢失、重复请求」三种情形（后两种由单元检查覆盖），但面向操作员的「一键回退」入口与界面归 [#19](https://github.com/kksty/HuntWeave/issues/19)；因此 ADR-0010 的门槛 4（`REVERT_ENTRY_AVAILABLE`）仍为假，门槛 1（profile 复验含票据）仍为假，真实执行未开放。
- **连接回收以对端观察为准**：探针证明的是「已建立的流在撤销后 1.385 秒内结束、之后无法新建」，未读 conntrack 表、也未证明网关侧连接表被清空。报告与本文不作「连接已清理」的表述。
- **宿主 LAN 不在默认保护内**：默认保护集合是固定范围（`0.0.0.0/8`、`127/8`、`169.254/16`、组播/保留段）、平台自身网络与所用桥的宿主地址；除此之外的宿主网段需要部署在 profile 的 `network.protected` 里声明。本轮靶场未构造该情形（`protected` 为空数组）。
- **回退的「停止确认丢失」只由单元检查覆盖**：靶场里诚实构造「停止无法确认」需要注入故障，本轮用 `FakeRuntime` 的拒绝停止路径覆盖；真实注入（例如暂停容器后停止）未做。
- **看门狗失败只被吞掉在内部**：`_watch` 的单次失败不会终止线程，但也不会写入事件时间线——管理器的账本记录的是状态与事实，成体系的执行事件流归 #19 的透明控制台。这是「所有动作进入真实事件时间线」在本轮未完全覆盖的部分。
- **未验证**：原生 Linux 宿主（ADR-0010 另立切片）；多活跃 Run 并发下的出口与回收（#21）；真实工具镜像（Kali）下的同组行为（#17）；`own_networks()` 在非容器化 Runner 下的行为（当前设计为无法识别即拒绝启动）。
- **探针自身的两处缺陷已修正**：fixture 传名而非容器对象、以及把会话变量覆盖成资源对象（都在探针里，不影响产品代码）。

## 待人工确认

- [ ] 实例的出口只允许「收窄」还是允许替换为更宽的一份授权？当前 `authorize_egress` 是**替换**（控制面拥有该 Run 的授权），靶场用「撤销后再放行一个端口」验证了替换语义；若要求只减不增，需要新增拒绝码并同步规格 §3.1。
- [ ] 内核规则复核的频率（当前 2 秒一次、每次一个 exec）与「漂移即停止」的策略是否可接受；是否会因网关短暂不可达而误停（当前不可达时跳过本轮，不停止）。
- [ ] 回退归档落在证据目录的 `reverts/` 下是否合适，或应进入业务库（#17 之后实例与调用关联会在业务侧，届时归档位置可能要重新统一）。
- [ ] `ToolCall` 的 `cancelling`/`denied` 状态与「取消 → 停止确认 → cancelled」的判定是否按上述归属顺延到 #17（规格第 4 节②档）。
- [ ] 保护集合是否应把部署声明的宿主网段做成必填（当前默认空数组，依赖部署者自觉）。

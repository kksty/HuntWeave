# P1 #17 真实动作最小闭环：靶场验证

日期：2026-10-10（Asia/Shanghai）。对应 Issue [#17](https://github.com/kksty/HuntWeave/issues/17)，依据 P1 规格 [第 3.1、3.2、3.4、3.6 节](./../specs/0002-real-execution.md)、第 5 节 tracer 顺序与 [ADR-0010](./../adr/0010-real-execution-boundary-and-gate.md)。提交 `08882f0`（契约）、`4e6b3e5`（真实执行端）、`454563f`（靶场探针）与本次（控制面与按真实结果分支）。

**本轮仍未开放真实执行**：`PROFILE_REVALIDATED`、`REVERT_ENTRY_AVAILABLE` 仍为假，四项门槛未逐项满足，因此**产品自身不允许创建真实 Run**（见下「控制面」）。真实动作在靶场经产品自己的 Runner 表面跑通，不经过应用层的 Run 创建。

## 问题与判定

- **动作与 profile 从字面量改为版本化注册表**。裁决：每个动作声明自己的参数 Schema、默认/硬超时、可分支的 summary 字段与可否重试；动作只属于一个 profile，跨 profile 的票据在契约层被拒；真假动作共用同一提交、查询、取消、续约与对账契约。
- **真假执行端共用一套账本规则**。裁决：抽出 `execution/ledger.py`，把受理、围栏（scope/policy/租约代次）、控制租约、取消、独立停止确认与证据哈希复核放在一处；`FakeRunner` 与 `RealRunner` 只实现「运行一次调用意味着什么」，各自使用独立的账本文件与所有权锁。
- **一次调用一个实例**。裁决：真实执行器按票据授权开执行会话、建实例（放行只含票据的目标端点）、在工具容器内以 profile 的普通用户运行命令、先文件后 hash 归档 stdout/stderr/转录、只保留动作声明的 summary 字段，并在所有路径上撤销→停止→回收。
- **控制租约绑定到实例**。裁决：实例租约取自票据租约，控制面不再续约时由管理器看门狗撤销并停止（#18 的机制），调用侧在结算前复核租约是否已失效。
- **真实 Run 不降级**。裁决：Run 的执行 profile 在创建时固定，且必须与其授权快照的 profile 一致；`real_execution_ready` 未满足时**拒绝创建真实 Run**（409 `real_execution_not_ready`）；派发前与续约时重新读取就绪，失效即**挂起**真实调用（保留预算与位置，记录 `real_execution_not_ready`）并停止续约，绝不改派到假执行端。
- **后续动作由真实返回决定**。裁决：模型 Adapter 增加真实分支，读 `last_summary`（执行端从原始输出解析并只保留声明字段）与 `last_target`；有端口则提出 `probe_http` 并指名该端点，没有端口则结束研究。演示分支行为保持不变。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `contracts/execution.py` | `ActionId`/`ExecutionProfile` 版本化枚举、动作注册表 `ACTIONS`、每动作参数模型（`FakeParameters`/`ShellExecParameters`/`DiscoverTcpParameters`/`ProbeHttpParameters`）、`ExecutionRequest` 校验参数与 profile 一致、`ExecutionResult.summary` |
| `execution/ledger.py`（新） | 共用账本：受理与指纹、围栏、控制租约、取消（把停止放在账本锁之外，避免与运行中调用反向取锁）、独立停止确认、证据归档与哈希复核、执行器私有笔记（`instance_id`、`halt_requested`） |
| `execution/real.py`（新） | 真实执行器：就绪前置（不可用即 503，不结算为拒绝）、按票据建实例并绑租约、动作 argv（`shell.exec` 跑票据命令；发现与 HTTP 探测跑固定程序，参数只来自票据绑定）、归档 stdout/stderr/转录、summary 只留声明字段、撤销→停止→回收、取消在「准备中」与「运行中」都兑现 |
| `execution/fake.py` | 只保留固定夹具行为（430 行 → 76 行），其余继承共用账本；事件仍带 `fake: true` 标记 |
| `execution/durable.py` | 临时文件名按写入者唯一（修 #18 复跑时发现的争用） |
| `execution/server.py` | 按 `execution_profile` 分派；真实 profile 在未启用管理时 409 `real_execution_disabled`；`RunnerUnavailable` → 503；查询/续约/取消在多个执行端之间路由 |
| `contracts/runs.py`、`storage/models.py`、`migrations/versions/0005_run_execution_profile.py` | Run 与授权快照新增 `execution_profile`/`mode`；`RunView.demonstration` 改为由 profile 派生的计算字段（不再恒为真） |
| `runs/service.py` | 创建 Run 时先读就绪（真实 profile 未就绪即 409），并校验 Run 的 profile 与其授权快照一致 |
| `runs/orchestration.py` | 按 Run 的 profile 构造票据（假：固定夹具参数；真：决策给出的参数，由契约按其 Schema 校验，不合格即 `action_not_in_profile`）；会话上下文带 `execution_profile`、授权端口、`last_summary`、`last_target`；事件与证据元数据不再一律写 `demonstration: true` |
| `runs/dispatch.py` | 真实调用在就绪失效时挂起而非改派；续约同样先读就绪，失效即停止续约让执行端自行停止 |
| `harness/model.py` | 真实分支：`id -u` → 按端口发现 → 有端口则对该端点 `probe_http`、无端口则结束；演示分支不变 |
| `lab/isolation/action.py`、`deploy/verify_action.py`（新） | 靶场探针：构建产品 Runner，向它的 HTTP 表面提交真实票据，用 Docker SDK 与证据目录读回事实 |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.7.2）、`backend/.venv` Python 3.12。

| 行为 | 结果 |
| --- | --- |
| 真实动作靶场验收 | `python deploy/verify_action.py` → **17 项检查全部通过**、`leftovers: 0`；报告 `runtime/sandbox/<run>/report.json`，本次运行 sha256 `867a85e00f9eec1925a47c3a361162abca02e703d8210e814684e9585376bf74`（`source_tree_dirty=true`，即本切片提交前的工作树） |
| 三个真实动作经票据跑通 | `shell.exec`（`id -u`）→ 退出码 0、stdout `10001`（工具容器普通用户）；`discover_tcp_services` → `open_ports:[7000]`、`closed_ports:[7001]`；`probe_http` → `status_code:200`、`server: SimpleHTTP/0.6 Python/3.12.15` |
| 真实反例驱动不同结局 | 对非 HTTP 端口 `probe_http` → `failed`/`action_failed`；`exit 3` 的命令 → `failed` 且 stderr 归档（内容为 `problem`）；发现无端口时下一步为 `finish_research` |
| 真实返回决定下一个动作并真的执行 | 对 HTTP 靶场做发现得 `open_ports:[8080]` → 计划提出 `probe_http` 并指名该端点（目标绑定由票据承载、不在动作参数里）→ **该动作真的运行并返回 200** |
| 证据可沿调用回溯 | 每次完成的调用有转录、`stdout.txt`、`stderr.txt` 三份证据；探针逐条复核文件存在且 sha256 与记录一致 |
| 出口与回收 | 实例的许可只含票据的目标端点，调用结束后许可为空、实例已回收；`manager.resources()` 与标签查询皆空 |
| 真假共用一个表面 | 同一次运行里演示 profile 的调用仍由同一表面服务且记录标记为 `fake-p0-v1`；未启用管理的部署提交真实票据得 409 `real_execution_disabled`，且该调用从未被受理 |
| 控制面：未就绪不得创建真实 Run | `RunService.create_run` 在无就绪读取者或读数为假时抛 `real_execution_not_ready`（400/409 语义由 `ServiceError` 决定），真实 Run 还必须与其授权快照的 profile 一致 |
| 控制面：就绪失效时挂起而非改派 | `ExecutionDispatcher` 对真实票据先读就绪；未就绪时调用业务侧 `hold_for_readiness`（记录中断与 `tool_held` 事件、保留预算与位置），且**不提交**给执行端；运行中的真实调用在就绪失效时停止续约 |
| 按真实结果分支（单元） | `harness/model.py` 的真实分支：`last_summary.exit_code` → 发现、`open_ports` 非空 → 指名端点的 `probe_http`、空 → 结束；无 `last_target` 时不指名端点；演示分支断言不变 |
| 本地纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 43 个源文件上无问题；`python -m pytest -m "not integration" -q` → **184 项通过、4 项跳过、38 项按标记排除**（`0012` 为 148 项通过） |
| 前序探针未回归 | `verify_lifecycle`（29 项）与 `verify_egress`（30 项）改用共用探针入口后复跑通过 |

## 未达成与限制

- **应用层没有端到端跑过真实 Run**：创建真实 Run 的前置是 ADR-0010 四项门槛逐项满足，而 P1 期间门槛 1、4 仍为假，产品因此**拒绝创建**真实 Run（这正是验收标准 5、6 的行为）。所以控制面的真实路径（profile 选择、就绪挂起、票据构造）由单元检查覆盖，执行路径由靶场探针覆盖；把两者接起来的应用层集成检查无法在门槛关闭时运行，也不允许为跑测试把门槛改成真。
- **预算与 outbox 的端到端真实性未验**：两者都属于控制面，与本切片选择的执行端无关；本轮的检查证明的是「同一个表面按 profile 分派、真实票据不会被假执行端服务」，而预算预留与 outbox 受理仍只由既有假执行路径的集成检查覆盖。
- **界面部分未交付**：验收标准 3 的「界面可跳转引用」与执行模式、IPv4/TCP 能力边界标注归 [#19](https://github.com/kksty/HuntWeave/issues/19)；本轮保证证据条目带 `relative_path` 与 sha256、且应用已有 `/evidence/{id}` 读取入口。
- **`ToolCall` 状态机的 `cancelling` 与 `denied` 仍未补齐**（规格第 4 节②档，`0012` 已顺延到此）：本轮新增的拒绝路径（`real_execution_disabled`、`action_not_real`、`action_not_in_profile`、`real_execution_not_ready`）在控制面分别表现为 409 或挂起，但执行端拒绝后仍未把调用记为 `denied`、取消已派发调用也未先经 `cancelling`。这两项直接决定控制台「停止中/待核对」与「被拒」的呈现，归 [#19](https://github.com/kksty/HuntWeave/issues/19) 一并落地。
- **真实动作的 argv 由产品代码固定**：`shell.exec` 用 `sh -c`，发现与 HTTP 探测用固定的 `python -c` 脚本，因此工具镜像必须提供 `python`（lab 探针镜像与 Kali 衍生镜像均满足）。P1 未把这些命令放进 profile；若要按镜像定制，应随 #19 的透明控制台一起把动作命令纳入 profile 并记录。
- **超时监督由适配器包裹**：命令在容器内由 coreutils `timeout` 监督（`-k 5`），退出码 124 视为超时。这一点只由适配器实现，单元检查用假运行时覆盖不了；靶场探针未构造超时用例（改造超时验证需注入慢命令，归 #21 的压力与容量验收）。
- **未验证**：原生 Linux 宿主（ADR-0010 另立切片）；多活跃 Run 下的并发与公平（#21）；Kali 工具镜像下的同组行为（本切片用 lab 探针镜像）；动态安装与按需特权（后续阶段）。

## 过程中发现并修复的缺陷

- 取消路径在准备阶段不知道实例身份，导致「准备中取消」不停止实例：改为先记录 `halt_requested`，由运行线程在准备完成后检查并释放。
- 取消先取账本锁再取管理器锁，而运行中的调用反向取锁（死锁）：把停止动作移到账本锁之外。
- profile 分派在持有缓存锁时调用管理器构造，而后者再次获取同一把锁（死锁）。
- `atomic_write` 多个写入者争用同一临时文件（#18 复跑时发现，本切片延续修复）。
- 探针用回声夹具的应答等待普通 HTTP 端口、每次调用换授权身份（被产品正确拒绝）、以及把目标端口放进动作参数（应由票据绑定承载）。

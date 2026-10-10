# P1 #16 受信管理组件与真实容器生命周期：靶场验证

日期：2026-10-10（Asia/Shanghai）。对应 Issue [#16](https://github.com/kksty/HuntWeave/issues/16)，依据 P1 规格 [第 3.1、3.6 节](./../specs/0002-real-execution.md)、第 5 节 tracer 顺序与 [ADR-0014](./../adr/0014-execution-lifecycle-and-environment-identity.md)。本轮**不开放真实执行**：不接目标出口（#18）、不接真实动作派发（#17）、不改 ADR-0010 门槛（`PROFILE_REVALIDATED` 仍为 `False`）。

## 问题与判定

- **受信管理组件只在 Runner 内**，且 Docker 可达面收窄为一组固定操作。裁决：新增 `execution/sandbox.py`（生命周期）与 `execution/dockerruntime.py`（唯一使用 Docker SDK 的适配器），后者只实现 `ContainerRuntime` 里点名的操作，没有通用请求方法、没有命令执行、没有 `**options` 透传；`execution/sandboxprofile.py` 保存固定 profile 与由它生成的容器 spec。
- **容器选项不由请求决定**。裁决：镜像、用户、挂载、网络模式、capabilities、限额与命令全部来自提交进仓库的 `profiles/sandbox-lifecycle-v1.json`；请求契约 `SandboxSessionRequest`/`SandboxInstanceRequest` 只带标识与授权身份且 `extra="forbid"`，profile 中任何放宽沙箱的取值（tool 非普通用户、带 capability、网络非 internal、镜像用 `latest`、可写根文件系统）都被加载器以具名原因码拒绝。
- **实例身份与创建中断**（ADR-0014）。裁决：`SandboxSession`、执行实例、ToolCall 三层身份分开；实例身份在创建开始前落账本并立即挂到会话下，创建中断的实例重启后标记为 `interrupted`，未核清的旧实例阻塞新实例，重建必然产生新实例身份且旧实例连同其环境清单与资源历史保留。
- **就绪必须来自观测**。裁决：实例只有在创建后**读回运行事实并与 profile 逐项比对**（用户、capabilities、no-new-privileges、只读根、挂载、网络命名空间）后才进入 `ready`，否则 `sandbox_profile_not_applied` 并留在中断态；管理能力只由部署显式启用，且启用不等于开放真实执行。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `execution/sandboxprofile.py`（新） | 固定 profile 的纯标准库加载器：精确键校验、镜像必须带 tag 或 digest 且非 `latest`、gateway 仅允许 `NET_ADMIN` 且必须 root、tool 必须普通用户且无 capability、`capabilities_drop=["ALL"]`、`read_only_rootfs`/`no_new_privileges` 必须为真、网络必须 `internal` 且 IPv4-only 且 DNS 仅回环。同时产出 `gateway_spec()`/`tool_spec()`，容器选项因此只有一个决定点 |
| `execution/sandbox.py`（新） | `SandboxManager` 生命周期：`open_session` / `launch_instance` / `stop_instance` / `reclaim_instance` / `reclaim_session` / `resources` / `audit` / `processes` / `logs` / `archive_evidence` / `observe`。标签只写本项目一套（`com.huntweave.{project,run_id,session_id,instance_id,role,profile}`），资源名由同一组身份推导；回收按标签选择并二次核对名字前缀，不属本实例的资源记 `ownership_mismatch` 且不删除 |
| `execution/dockerruntime.py`（新） | 唯一 Docker SDK 使用者，实现 `ContainerRuntime`：探测、解析镜像、创建/删除网络与卷、创建/启动/停止/删除容器、读回容器事实、查询带标签资源与进程、有界日志。所有 Docker 异常翻译为 `RuntimeUnavailable`/`ResourceNotFound`，构造失败也在启动时翻译 |
| `execution/durable.py`（新） | `atomic_write` 由 `fake.py` 抽到共用模块：真实执行端与假执行端用同一份落盘语义 |
| 账本 | `runner_state/sandboxes.json` 一次写入会话与实例两张表；重启后 `creating` 一律降为 `interrupted`；实例记录保留 `EnvironmentManifest`（profile 版本、解析后的镜像 digest、profile 声明的工具清单、引擎与架构）与全部受管资源 |
| `execution/server.py` | 新增 `sandbox_settings`/`sandbox_factory` 注入点与懒加载：未启用时不构造任何 Docker 客户端、不导入 Docker SDK；启用后报告 `sandbox_management`（`disabled`/`ready`/`unavailable`）与原因码，构造或探测失败都报原因码而不是 500 |
| `contracts/capabilities.py`、`execution/capabilities.py` | 能力契约新增 `sandbox_management` 与 `sandbox_reason_code`，与四项门槛分开表达：部署启用状态不是门槛，也不是就绪 |
| `config.py` | `SandboxSettings.from_env()`：`HUNTWEAVE_SANDBOX_MANAGEMENT` 必须是 `enabled` 才启用（`true`/`1`/大小写变体一律不启用），profile 目录默认镜像内 `/opt/huntweave/profiles` |
| `deploy/compose.sandbox.yaml`（新） | 显式启用入口：只覆盖 runner 服务，加挂 `/var/run/docker.sock`、`group_add: ["0"]`（socket 为 root:root 0660 而 Runner 以 uid 10001 运行）与两个环境变量；基础 `deploy/compose.yaml` 保持无 socket、无开关、三服务不变 |
| `deploy/Dockerfile` | `docker>=7.1,<8` 进入主依赖（启用管理时 Runner 需要它），`COPY profiles /opt/huntweave/profiles` 让固定 profile 随镜像发布；checks 阶段改为复制整个 `deploy/`（纯检查要读 Compose 文件与该 Dockerfile） |
| `lab/isolation/Dockerfile`、`lab/isolation/lifecycle.py`（新） | 拆出 `lab` 基础阶段 + `manager`/`sandbox` 两个目标；新探针在 `--network none --read-only --cap-drop ALL` 容器里驱动**产品自己的** `SandboxManager`，再用 Docker SDK 独立读回事实 |
| `deploy/verify_lifecycle.py`、`deploy/lab_docker.py`（新） | 生命周期探针入口与两个探针共用的 socket/构建/来源事实助手（原先在 `verify_isolation.py` 内重复） |
| 检查 | `backend/tests/test_sandbox_lifecycle.py` 41 项、`backend/tests/test_sandbox_deployment.py` 4 项：固定操作集与无 `**kwargs`、请求不能夹带配置、profile 放宽被拒、选项全部来自 profile、授权身份绑定与错配拒绝、实例身份与重建、创建中断与重启降级、未记账资源阻塞新实例、停止未确认拒绝、回收只及本项目、日志双边界与截断标注、进程查询、证据归档的路径与上限、默认部署无管理能力、启用不等于开放真实执行 |

## 验证

环境：Windows 11 x86_64（`Windows-11-10.0.26200-SP0`）、Docker Desktop（Engine 29.7.2）、宿主 Python 3.14.5（探针入口）、`backend/.venv` Python 3.12（纯检查）。生命周期探针自建 `huntweave-isolation-probe:p0` 与 `huntweave-sandbox-lifecycle:p1` 两个 lab 镜像，报告写入被忽略的 `runtime/sandbox/<run>/report.json`。

| 行为 | 结果 |
| --- | --- |
| 无目标网络的固定生命周期检查 | `python deploy/verify_lifecycle.py` → **29 项检查全部通过**，`leftovers: 0`、`cleanup_failures: []`；报告 `runtime/sandbox/6abc3eb30ddf4f80ac5f21ee5aae4897/report.json`，sha256 `51b98612a82349dceb5a83ea2e03b7bbcde84f80660a9746cbe292721ad12ede`（运行时 revision `b644ecd`、`source_tree_dirty=true`，即本切片提交前的工作树） |
| 固定 profile 生效 | `profile_keeps_the_tool_unprivileged`：profile 载入且在会话网络中 (`internal`)、tool 用户 `10001:10001`、无 capability、只读根与 `capabilities_drop=["ALL"]` |
| 实例与标签 | `instance_is_ready`、`environment_manifest_records_what_ran`（引擎 29.7.2、x86_64、两个角色解析出镜像 digest）、`round_owns_a_volume_a_network_and_two_containers`、`every_resource_carries_the_project_labels`、`resource_names_are_derived_from_the_identities` 全部通过 |
| 普通用户与受控挂载 | `tool_runs_as_a_normal_user_in_a_read_only_filesystem`（SDK 读回 `Config.User=10001:10001`、`CapDrop=["ALL"]`、两容器均有 `no-new-privileges`、`ReadonlyRootfs=true`）、`tool_mounts_only_its_private_workspace`（唯一挂载是本轮私有卷）、`no_container_sees_a_management_socket`、`gateway_is_the_only_privileged_part`（gateway 仅 `NET_ADMIN`） |
| 网络与出口 | `session_network_is_internal_and_isolated`（`Internal=true` 且 `gateway_mode_ipv4=isolated`）、`only_one_network_exists_for_this_round`、`the_tool_has_no_address_outside_the_session_network`（`lo` + `eth0=172.28.0.1`，唯一非回环地址落在会话子网 `172.28.0.0/16` 内）、`gateway_installs_default_deny_rules`（`INPUT/OUTPUT/FORWARD DROP`，无任何 ACCEPT 规则）；**本轮不创建目标网络、不发起任何目标流量** |
| 进程与日志 | `tool_processes_are_visible_through_the_manager`（uid 10001 的 `/lab/fixture.py hold` 两个进程）、`log_read_is_bounded`（实测 0 字节 / 上限 65536；这两个固定镜像不打印内容，真正被截断的边界由单元检查覆盖） |
| 证据归档 | `evidence_is_archived_for_this_instance`：写入 `<run>/<instance>/lifecycle.json`，102 字节，sha256 `48c9ff90…`，路径由管理器按身份拼装 |
| 停止与回收 | `stop_is_confirmed_by_the_runtime`（`stop_confirmed_at` 晚于实际停止）、`no_container_is_running_after_the_stop`、`reclaim_removes_every_owned_resource`（容器×2/网络/卷四级全部移除、`failed: []`）、`the_round_leaves_nothing_behind`（管理器视图、SDK 按标签查询与 `audit()` 三处均为空） |
| 身份与重建 | `ledger_survives_a_new_manager`（新进程读回同一会话与已回收实例及其环境清单）、`a_rebuild_is_a_new_instance_identity`（旧 `1e753fd3…` → 新 `e4697c43…`）、`the_old_instance_is_still_recorded`、`the_second_round_reclaims_completely` |
| 隔离探针未因镜像重构回归 | `python deploy/verify_isolation.py` → **40 项检查全部通过**、`cleanup_errors: 0`；报告 `runtime/isolation/f5893680946f4e57a3dfe1354ce618a0/report.json`，sha256 `33c90ab6f1508386a5e7bc62b0b019efb6d34ed81e70cdf7619e1a634c2d3f24`（lab 镜像改为 `lab` 基础阶段 + `manager`/`sandbox` 目标后复跑） |
| 本地纯检查 | `ruff check src tests` 全部通过；`mypy --config-file pyproject.toml src` 在 **41 个源文件**上无问题（`0008`/`0010` 为 37 个）；`python -m pytest -m "not integration" -q` → **123 项通过、4 项跳过、38 项按标记排除**（`0010` 为 78 项通过，本轮 +45 = 生命周期 41 + 部署边界 4） |
| 默认部署仍无管理能力 | `docker compose config --quiet` 对基础文件与覆盖文件均通过；两个文件的 `docker.sock` 挂载点分别为 `[]` 与 `["runner"]`，服务仍只有 `app`/`postgres`/`runner`。dev 栈实测 `/v1/capabilities`：`sandbox_management=disabled`、`sandbox_reason_code=null`、`mode=demonstration`、`real_execution_ready=false`、顶层原因 `environment_unsupported`，四项门槛仍为 `profile_revalidation=false`、`contract_expressiveness=true`、`console_consumption=true`、`deployment_revert=false` |
| 显式启用后的实测 | 用 `-f deploy/compose.yaml -f deploy/compose.sandbox.yaml` 重建 runner：`sandbox_management=ready`、`sandbox_reason_code=null`，而 `real_execution_ready=false`、`mode=demonstration` 不变（启用管理不等于开放真实执行）。随后用基础文件恢复，runner 回到 `sandbox_management=disabled`、三服务 healthy |
| 镜像内容核对 | 控制镜像内 `import docker` 得 7.2.0，`/opt/huntweave/profiles` 含 `sandbox-lifecycle-v1.json`（不只看构建退出码） |
| 两轴代码审查 | 对本轮改动跑 Standards 与 Spec 两轴（并行子代理，对照 `AGENTS.md`、`PROJECT.md`、`GLOSSARY.md`、ADR-0010/0014、规格第 3/5/6 节与 Issue #16 正文）。**采纳并修复**：`docker.from_env()` 构造失败不再让 `/v1/capabilities` 返回 500 而是报 `sandbox_runtime_unreachable`；探针 `cleanup()` 由「项目标签」收窄为「本项目 + 本轮 Run 身份 + 名字前缀」，不再可能删除其他 Run 的资源；`stop_confirmed_at` 改为在停止之后取时间；日志截断同时上报 daemon 行数边界与被截断标记；`gateway_installs_default_deny_before_any_process` 更名为 `gateway_installs_default_deny_rules`（#16 不装放行规则，「规则先于进程」是 #18 的验收项，探针里注明）；checks 阶段补复制 `deploy/`（此前纯检查的容器入口会因缺 Compose 文件失败）；`EnvironmentManifest` 补 §3.6 的工具清单；会话/实例补授权身份（`scope_id`/`scope_version`/`policy_version`）且错配以 `sandbox_authorization_mismatch` 拒绝；`sandbox.py` 拆分出 `sandboxprofile.py`（固定描述 vs 生命周期）、`_names` 由字典改为具名结构、容器 spec 生成移回 profile；抽出 `deploy/lab_docker.py` 消除两个探针入口的重复。**未采纳**：把请求改成携带票据——票据到实例的绑定了 #17 才有执行端可校验（见「未达成与限制」）。 |
| 一项发现（外部环境） | `deploy/compose.sandbox.yaml` 首次实测时 Runner 报 500（后修为原因码），根因是 socket 为 `root:root 0660` 而 Runner 以 uid 10001 运行；覆盖文件补 `group_add: ["0"]` 后 `sandbox_management=ready`。该结论只在本机 Docker Desktop 的 WSL2 后端实测 |

## 未达成与限制

- **未接票据与派发**：管理器只接受会话/实例标识与授权身份，`ToolCall` 票据到实例的绑定、动作参数校验、预算占用发生路径仍归 [#17](https://github.com/kksty/HuntWeave/issues/17)。因此本轮的「按票据」只到「按同一授权身份建立实例绑定」这一层，没有票据校验可验；规格第 3.1 节「准备调用」一行的其余事实（票据、当前授权有效）仍未实现。
- **无目标出口**：本轮不安装任何放行规则、不创建目标桥接网络，「规则先于进程」「撤销/取消/回退时序」归 [#18](https://github.com/kksty/HuntWeave/issues/18)。探针的默认拒绝检查在容器启动后读取，不构成「规则先于进程」的证据，已在检查名与报告备注中标明。
- **管理组件未被产品调用**：真实动作尚未接入，`SandboxManager` 目前只由靶场探针与单元检查驱动；`execution/dockerruntime.py` 因此没有容器外的检查覆盖，只有本记录里的靶场实测。
- **profile 用的是 lab 探针镜像**：`sandbox-lifecycle-v1` 的 gateway 与 tool 都指 `huntweave-isolation-probe:p0`（`status: lifecycle_check_only`），工具清单为空。规格第 3.6 节的 Kali 固定 digest 工具镜像、包版本清单与 HuntWeave 自有版本标签随真实动作切片（#17）落地；本轮的 `EnvironmentManifest` 只记它真能观测到的内容，不伪造尚不存在的构建记录。
- **镜像引用是 tag 不是 digest**：profile 里写的是 `repo:tag`，实例清单记录运行时解析出的 digest。生产 profile 是否必须直接写 digest 见「待人工确认」。
- **日志截断分支只有单元覆盖**：两个固定镜像都不打印内容，探针实测 0 字节/上限 65536；「被 daemon 截断」的路径由 `FakeRuntime` 的单元检查覆盖（行数边界与字节边界各一条）。
- **创建中断只有单元覆盖**：探针覆盖了身份、重建与回收，但「创建到一半进程死亡」由单元检查注入失败模拟（账本留 `interrupted`、资源留名、下次启动被阻塞），未做真实容器中途杀进程的注入。
- **能力字段尚未展示**：`sandbox_management`/`sandbox_reason_code` 只在能力响应里，控制台展示归 [#19](https://github.com/kksty/HuntWeave/issues/19)；本轮未改前端。
- **未验证**：原生 Linux 宿主（ADR-0010 另立切片）；多活跃 Run 下的并发（#21）；真实 Run 的就绪失效与回退收尾（#17/#18）；`group_add` 在其他宿主上的必要性与最小化。
- **一次性资源**：探针自建容器、网络、卷与本轮 lab 镜像；`runtime/sandbox/` 与 `runtime/isolation/` 均在被忽略的 `runtime/` 下，不入 Git。

## 待人工确认

- [ ] profile 中的镜像引用是否必须直接写 digest（当前为 `repo:tag`，解析后的 digest 记进实例清单；lab 构建产物没有稳定的 registry digest，生产 profile 是否要求写死 digest 需定调）。
- [ ] `sandbox_management`/`sandbox_reason_code` 放在能力契约内是否合适，或应独立成一个管理状态接口（`0010` 的「门槛 2、3 是否改为构建期断言」待确认项与之相关）。
- [ ] 实例账本继续放在 Runner 状态卷的单个 JSON 里是否可接受，还是应在 #17 把调用绑定后迁到 PostgreSQL（由 app 侧拥有实例与调用关联）。
- [ ] 是否在 #18 单独构建一个 HuntWeave 自有 gateway 镜像，而不是继续用 lab 探针镜像 + 固定命令（本轮的 gateway 命令在 profile 里写死为 `/lab/network.py gateway`）。
- [ ] `deploy/compose.sandbox.yaml` 的 `group_add: ["0"]` 是否可接受，或应由部署者传入 socket 的 GID（当前默认 0 是为了让本机 Docker Desktop 的 `root:root 0660` socket 可用）。

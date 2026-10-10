# P1 停止确认事实与动作工作目录：靶场验证

日期：2026-10-10（Asia/Shanghai）。对应 [#18](https://github.com/kksty/HuntWeave/issues/18) 与 [#17](https://github.com/kksty/HuntWeave/issues/17) 已交付行为的**事后收紧**：触发条件是 2026-10-10 对 [#19](https://github.com/kksty/HuntWeave/issues/19)、[#21](https://github.com/kksty/HuntWeave/issues/21) 验收标准的补充（同步 0.9.5 设计，来源 `docs/specs/0006` §7、`docs/specs/0007` §3、`PROJECT.md` §12.4）。这两条补充要求**陈述本身有事实支撑**，而下列两处代码当时给不出该事实，因此在 #19 开工前先修：

1. **停止未确认必须可表达**：#19 的「不得显示已回收或额度已释放」与 #21 的「容量只由受信停止事实释放」都要求记录能区分「已确认停止」与「未确认停止」。收紧前执行端只在结果状态上体现停止，没有把「停止是否被确认」作为事实记录。
2. **动作的 cwd 必须可见**：#19 要求「命令、规范化参数 hash、cwd、执行身份、工具与环境版本可见」。收紧前动作的工作目录由工具镜像自己的默认值决定，产品既不指定也不记录。

本记录不含 #19、#21 的新增功能工作，也不改变任何就绪门槛与阶段状态。

## 问题与判定

- **停止确认与调用结局分离**。裁决：`CallLedger` 的每次状态迁移都取用一条停止主张——状态从 `running` 走到终止态时，只有执行端拿得出受信停止事实才允许记录「进程与连接已结束」；拿不出时写 `process_active=None`、`connection_open=None`，并追加 `execution_stop_unconfirmed` 事件（携带该调用最终结局与原因码），而不是把「已取消」当成「已回收」。`ExecutionObservation` 既有语义不变：`None` 表示无法确认，对账与容量释放都不得把它读成已停止。
- **主张不设乐观默认值**。裁决：`StopOutcome.stopped` 无默认值（构造时必须写出），`_change(..., stopped=None)` 表示「按本执行端自己的停止事实回答」而不是「假定已确认」；`CallLedger` 基类的 `_stop_fact` 默认返回 `False`（无法证明就不主张），由自有执行结果的执行端覆写为 `True`（演示侧固定夹具在本进程内运行，结束调用即结束它启动的一切；真实侧读管理器事实）。因此漏写参数得到的是保守答案，而不是「已停止」；即使解释器用 `-O` 去掉断言，方向仍然是保守的一侧。
- **受信停止事实由管理器给出，不由执行器自述**。裁决：`RealRunner._stop_confirmed(instance_id)` 只读受信管理器的实例记录——状态为 `stopped`/`reclaimed` **且** `stop_confirmed_at` 已写入才算确认；调用从未准备过实例（笔记为空）算已确认；实例仍在运行、停止过程失败、或管理器已不再持有该实例（「它不知道」不等于「它已停止」）都算未确认。容器是否仍在运行属于管理器的判据（`sandbox_stop_unconfirmed`），执行端只读取记录下来的结论，不重新判定、也不假定。
- **调用始终记名它用过的实例**。裁决：释放路径不再在结束时清空 `instance_id` 笔记——停止失败时清空会让下一次停止把「该调用仍有一个实例在运行」回答成「该调用从没有过实例」。笔记留在记录里，任何后续停止都能找到它。
- **重启不构成停止**。裁决：账本重启后把未结算的记录读为 `unknown` 时显式声明未确认（`stopped=False`），因此进程与连接保持未知并写出缺口事件。重启只能证明账本丢过观察，不能证明它启动的进程已经结束；某次停止若真被确认过，那是管理器记录里的事实，由 `_stop`/后续路径按事实读出，而不是由重启推断。
- **结束时没有显式停止尝试的路径同样要给出事实**。裁决：租约到期、归档失败等路径经 `_stop_fact(record)` 读取事实，而不是按状态推断；`_stop`（对 `unknown` 调用的取消）也先问事实，能确认才写 `execution_stopped` 与「进程已结束」，否则只写缺口事件。
- **工作目录由 profile 决定并在调用上显式说明**。裁决：`SandboxManager.run_command` 把 profile 的 `workspace.mount` 作为 `workdir` 显式传给运行时（`ContainerRuntime.exec_in_tool` 新增该关键字参数，Docker 适配器把它交给 exec 本身），不再依赖工具镜像的默认工作目录；执行器在 `execution_instance` 事件里写出同一个值，因此「动作在哪里跑」是可复核的部署决定，而不是镜像的偶然默认值。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `execution/ledger.py` | 新增 `StopOutcome(stopped, result)`（`stopped` 必需）；`_change(..., stopped=None)` 未给出时按 `_stop_fact` 回答，终止态（`completed`/`failed`/`cancelled`/`unknown`）且该调用确实启动过时追加 `execution_stop_unconfirmed` 事件；`_observation_after` 把「结局」与「停止」分开：启动过但未确认即报 `None`，确认或从未启动才报 `False`；`_stop_fact` 基类默认 `False`；`_stop_call` 基类默认取本执行端事实；`_stop` 在未确认时只写缺口事件；账本重启读为 `unknown` 时显式 `stopped=False` |
| `execution/fake.py` | 覆写 `_stop_fact` 为真并说明依据：固定夹具在本进程内运行，进程结束即其启动的一切结束 |
| `execution/real.py` | `_stop_confirmed(instance_id)` 只读管理器事实（实例不在管理器记录里报未确认）；`_stop_fact` 从调用笔记取实例身份；结算、归档失败、租约到期与取消四条路径都按该事实回答（不再逐处重复取值）；`_release` 不再清空实例笔记；`_announce` 在事件里写出 `cwd` |
| `execution/sandbox.py` | `ContainerRuntime.exec_in_tool` 新增 `workdir` 关键字参数；`run_command` 以 profile 的工作区挂载点显式传入，并说明这是「调用自身的属性」 |
| `execution/dockerruntime.py` | `exec_in_tool` 把 `workdir` 交给 `exec_run`（目录属于本次调用，不属于镜像配置） |
| `lab/isolation/action.py` | 新增第 2 项检查：以 `pwd` 实测工具容器的起始目录，并与 `execution_instance` 事件声明的 `cwd` 及 profile 的 `workspace.mount` 三者对照；后续分组序号顺延 |

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.7.2）、`backend/.venv` Python 3.12、宿主 Python 3.14.5（探针入口）。

| 行为 | 结果 |
| --- | --- |
| 真实动作靶场验收 | `python deploy/verify_action.py` → **18 项检查全部通过**、`leftovers: 0`；最终报告 `runtime/sandbox/8aca0824f55243e6b169299de4afe146/report.json`，sha256 `876f9bb6fd2f0d0d93a2da1ea9ef6f00a10bf2a2470f694c4b47374993e8346c`；报告内 `source_revision=6d27220`、`source_tree_dirty=true`（本轮改动此时尚未提交，被测代码即该工作树） |
| 动作真的在 profile 指定的目录里运行 | 新增检查 `an_action_runs_in_the_directory_the_profile_names`：`shell.exec pwd` 的归档输出为 `/workspace`，`execution_instance` 事件声明 `cwd: /workspace`，profile 的 `workspace.mount` 亦为 `/workspace`，三者一致 |
| 已确认的停止才允许声明进程结束 | 单元检查：正常完成的调用其观测 `process_active is False`、`connection_open is False`，时间线中不含任何 `unconfirmed` 事件 |
| 未确认的停止不释放任何东西 | 单元检查（注入 `refuse_stop`）：调用仍以 `completed` 结束（结局不被改写以掩盖缺口），但观测为 `process_active is None`/`connection_open is None`，事件 `execution_stop_unconfirmed` 的 `outcome` 为 `completed`，且管理器里该实例仍为 `ready` |
| 取消路径同样受此约束 | 单元检查（注入 `refuse_stop` 且命令仍在运行）：取消返回 `cancelled`/`operator_cancelled`，观测为 `None`/`None`、含 `execution_stop_unconfirmed`，实例仍为 `ready` 而不是被当成已回收 |
| 「管理器不知道」不等于「已停止」 | 单元检查：运行中取消前清空管理器的实例记录（模拟重启后实例账本不再覆盖该实例），取消结果仍为 `cancelled`，但观测为 `None`/`None` 并带缺口事件 |
| 停止失败不清空调用与实例的绑定 | 单元检查：注入 `refuse_stop` 后，调用仍记名它用过的实例（`instance_id` 笔记仍在、`_stop_fact` 为假），因此后续停止不会把它读成「从没有过实例」 |
| 重启不构成停止 | `test_fake_runner`：账本重启把运行中的记录读为 `unknown` 时，观测保持 `None`/`None` **并新增缺口事件**（该检查同时被加强以断言事件存在）；未启动过的记录仍报「从未启动、无物可停」 |
| 本地纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 43 个源文件上无问题；`python -m pytest -m "not integration" -q` → **191 项通过、4 项跳过、38 项按标记排除**（`0013` 为 184 项通过） |
| 前序探针未回归 | `verify_lifecycle`（29 项）与 `verify_egress`（30 项）在改动后复跑通过（`exec_in_tool` 的签名变化对生命周期与出口路径无影响）。出口探针的**首次尝试**在启动探针容器时以退出码 1 失败（`deploy/lab_docker.py` 未捕获该命令的 stderr，原因未取得，报告目录 `runtime/sandbox/7099fceab8724ef49a64bd2942fcbfab` 只有空报告）；同一源码版本立即重跑通过（报告 `runtime/sandbox/c8f29b58c5c2490ba1f11e38e6c4d25f/report.json`，sha256 `604614ca3f74ab3d5ef468904a41041b47cf8e0edaa3689b05c03990204d4a61`）。这是探针入口的可用性缺口，不是本轮改动的回归——本轮未改到出口路径，且失败发生在容器启动阶段 |

## 未达成与限制

- **本轮只是把事实补齐，控制台仍未消费**：#19 的「不显示已回收」「展示停止未确认及其占用的最后实例与调用」需要界面与事件流落地；本记录只保证后端**给出可区分的事实**，不声称界面已展示。
- **容量与背压未实现**：#21 的「容量只由受信停止事实释放」在控制面（控制槽与物理执行额度）尚无实现，而它的正确性依赖本轮的事实区分。本轮不新增额度模型、也不改任何限额默认值。
- **`cwd` 是部署决定而非运行时探测**：记录的是管理器命令执行时使用的工作目录（profile 的工作区挂载点），靶场已实测容器真的从该目录启动；但产品不会在每次调用前后再去容器内读取一次实际目录，因此若有进程自行 `cd`，记录反映的是**调用起点**而非其内部后续变化。
- **事件名与既有原因码同名**：`execution_stop_unconfirmed` 已作为核对路径的原因码存在（`runs/orchestration.py`、前端文案表），此处用作事件类型是刻意复用同一概念，不是笔误；两者一个是「调用时间线上的事实」，一个是「控制面给操作员的归类」。
- **工具镜像仍须提供 `python`**：动作 argv 由产品代码固定（`0013` 的限制未变）；把命令纳入 profile 归 #19 的透明控制台一并处置。
- **未验证**：真实执行仍是关闭的（四项门槛未逐项满足，产品拒绝创建真实 Run，`0013` 的结论不变）；未确认停止与「管理器不知道」只在假运行时上注入，未在真容器上构造「停止失败」用例（需注入容器引擎级故障，归 #21 的压力与容量验收）。

## 两轴复审与处置

改动经 Standards（`AGENTS.md`/`PROJECT.md`/`0002` 规格/ruff 与 mypy 配置 ＋ Fowler 坏味道基线）与 Spec（`0002` 规格 §2 第 11 条、§3.1 生命周期、§4 重启语义，`0006` §7，以及 #17/#18/#19/#21 的验收标准）两轴并行复审，处置如下（发现即修，未留待后续切片）：

| 复审发现 | 处置 |
| --- | --- |
| `real.py` 中 `instances.get()` 外挂了永不可达的 `except ValueError`（`instances` 是普通字典） | 删除该分支；实例不在管理器记录中改报**未确认**（见上「管理器不知道」检查） |
| `stopped` 主张有乐观默认值（`_change(..., stopped=True)`、`StopOutcome.stopped=True`），漏写参数即等于「已确认停止」 | `StopOutcome.stopped` 改为必需参数；`_change(..., stopped=None)` 改为「按本执行端事实回答」；基类 `_stop_fact` 默认返回 `False`，由自有执行端覆写为真 |
| 释放路径用 `finally` 清空 `instance_id` 笔记，可能把「仍有实例在跑」变成「从没有过实例」（规格 §3.1「停止确认必须对应原调用所用的实例」） | 不再清空；新增检查固定该绑定在停止失败后仍然存在 |
| 重启升级路径未作停止声明，且缺口没有事件（规格 §4「重启本身不构成停止」、§7 容量只由受信事实释放） | 升级路径显式声明未确认并写出缺口事件；`test_fake_runner` 的对应检查同步加强 |
| `_stop`（对 `unknown` 调用的取消）无论能否确认都直接写「进程已结束」 | 先问 `_stop_fact`：能确认才写 `execution_stopped`，否则只写缺口事件 |
| `unknown` 状态无法表达「停止已确认」（`0006` §7 要求该情形可归还额度） | `_observation_after` 把结局与停止分开：已启动且未确认才报 `None`，已确认即报 `False`，与状态解耦 |
| `_failure(..., stopped: bool | None = None)` 用一个参数承载两种策略 | 该参数只作透传，解析规则统一由 `_change` 定义，不再有隐藏分支 |
| 同一事实在四条路径重复取值（Shotgun Surgery） | 结算/归档失败/租约到期三条路径不再逐处取值，统一由账本向执行端的事实提问；取消路径仍用刚发生的停止尝试结果 |
| `0014` 曾写「执行端不重复判定」，而 `_stop_confirmed` 会自己读管理器状态 | 表述改为准确说法：执行端只读取管理器记录下来的结论，判据仍在管理器（`sandbox_stop_unconfirmed`） |
| 复审指出 `test_sandbox_lifecycle` 中改动的等待条件与本轮两项目的无关 | 保留并写明理由：该检查等待「已停止」时可能在 halt 的两次写入之间读到中间态（先写状态、再写原因），改为等待 halt 完成；这是顺带的检查修正，不涉及产品行为 |

## 补充验收的处置（哪些不是本轮调整）

2026-10-10 的 Issue 补充里，以下属于**新工作**，不在本记录范围，按原切片的阻塞顺序办理：

- #19：六轴主张状态编码、`lapsed`、`quarantine`/`degraded` 的呈现与不可折叠、控制槽与物理执行额度背压、禁止单一健康灯或安全百分比、超 100 目标的行级反馈、IPv6 先报 `ipv6_environment_unsupported`、`ToolCall` 的 `cancelling`/`denied`（`0012`、`0013` 已两次顺延）。
- #21：全局物理容量（默认 4）与每 IP 上限（默认 1，跨 Run 含受控复现）、阈值在 Run 前锁定并版本化、未知结果不重试的计数口径。
- #22 已关闭为设计交付：其验收案例与 P2-F 条目（六轴、确认等级、冻结交付）属 P2 实施期；P2 里程碑与切片尚未建立。

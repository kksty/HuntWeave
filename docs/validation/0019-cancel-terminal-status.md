# 取消终态确定性：动作失败不得覆盖已请求的取消

日期：2026-10-11（Asia/Shanghai）。对应缺陷见 [#77](https://github.com/kksty/HuntWeave/issues/77)。本轮由 CI 在 `main` 上的一次随机失败触发（`4f0d063` 已存在，与当次提交无关），属**既有缺陷的修复**，不是新切片。

## 问题与判定

- **现象**。`main` 的 `checks` 在 `tests/test_real_execution.py::test_cancelling_ends_the_running_command_and_keeps_what_was_collected` 上随机失败（本地实测约 1/40）：`assert cancelled.status == "cancelled"` 得到 `'failed'`。同一份代码两次 CI 一次通过（253 项）一次失败（252 通过 1 失败）。
- **根因**。两条路径竞争同一个调用的终态：

  1. `RealRunner._stop_call` 先写下 `halt_requested` 笔记，再 `halt_instance` 停掉实例，返回后由 `Ledger.cancel` 的第二次取锁把状态写成 `cancelled`；
  2. 动作侧线程还在 `run_command` 里。实例被停后 `SandboxManager.run_command` 因 `record.state != "ready"` 抛 `SandboxRejected("sandbox_instance_not_running")`，`_run_call` 的 `except` 分支调用 `_settle_failure`，把状态写成 `failed`。

  谁先拿到账本锁，终态就是谁写的。取消到达时动作拒绝运行，是**取消生效的结果**，不是独立失败；把它记成 `failed` 等于让线程调度决定调用结局。
- **判定**。**调用被请求取消后，终态由取消路径独占。** `_settle_failure` 在 `halt_requested` 已置位时不再迁移状态：取消路径会写 `cancelled` 与该操作员原因码，并保留已归档的证据，因此让出这次迁移不丢任何事实。反过来若让 `failed` 先落，`Ledger.cancel` 第二次取锁看到终态即原样返回（`ledger.py` 的 `if current.status not in {"accepted", "running"}: return current`），调用结局就永久取决于竞态。
- **不改变的部分**。停止确认与结局分离的既有语义不变：确认停止才报 `process_active=False`，未确认仍报 `None` 并写 `execution_stop_unconfirmed`。本修复只决定**终态归谁写**，不触碰停止事实的来源（仍是受信管理器，见验证记录 `0014`）。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `execution/real.py` `_settle_failure` | 取锁后若该调用已写下 `halt_requested`，直接返回，不写 `failed`；取消路径继续负责终态、原因码与已归档证据 |
| `tests/test_real_execution.py` | 把「命令是否启动是真实竞态」的注释改写为「竞态在命令是否启动，**不在**取消是否成为终态」，并说明 `process_active is False` 只在结合已确认停止时才成立 |

## 验证

环境：Windows 11 x86_64、`backend/.venv` Python 3.12.14、pytest 9.1.1。全部为本地单元检查，未启动容器、未接触任何目标。

| 行为 | 结果 |
| --- | --- |
| 缺陷可复现 | 修复前，该检查单独重复 40 次出现 **1 次失败**、重复 60 次出现 **1 次失败**（第 41 次）；失败输出为 `assert 'failed' == 'cancelled'`、`tests/test_real_execution.py:249`，用时 0.37 秒 |
| 修复后不确定性消失 | 修复后单独重复 **60 次全部通过，失败 0 次**；其中「快速路径」出现 **0 次**（修复前正是 0.3–0.4 秒的快速路径产出 `failed`） |
| 相关用例成组稳定 | `tests/test_real_execution.py` + `tests/test_operator_view.py` 连续 3 轮：**32 项全通过 ×3**（用时 11.03s / 1.04s / 11.03s，覆盖慢路径与快速路径两种时序） |
| 无回归 | `python -m pytest -m "not integration" -q` → **254 项通过、7 项跳过、41 项按标记排除**（修复前同一命令为 254 通过 / 7 跳过，修复未增删用例） |
| 纯检查 | `ruff check src tests` 通过；`mypy --config-file pyproject.toml src` 在 **50 个源文件**上无问题 |
| CI | 推送后 `checks` 在 `main` 上通过；本地重复无法替代 CI 结论，以该次运行为准 |

## 未达成与限制

- **只覆盖单元路径**。修复验证走的是假运行时（`FakeRuntime` + 门闩命令）与进程内 Runner；真 Docker 下由实例停止引发 `sandbox_instance_not_running` 的时序未重跑靶场探针，`lab/` 四份探针（动作 18、生命周期 29、出口 30、保留 10）本轮未复跑。
- **竞态窗口按构造收窄，未做形式化证明**。`halt_requested` 由取消路径在停实例**之前**写入，因此动作侧的拒绝必然晚于该笔记；这是顺序保证，不是互斥锁。若将来新增一条不经 `_stop_call` 就迁移终态的取消路径，需重新核对本判定。
- **重复次数不是概率上界**。1/40 与 1/60 是本地观测值，不构成该竞态发生率的估计；修复的依据是终态归属的判定，而不是「跑多少次没红」。
- **未确认停止的既有缺口不变**：控制面与界面尚未消费「已确认/未确认停止」的区分（归 #19/#21），本修复不改变该状态。

## 待人工确认

- 缺陷本身见 [#77](https://github.com/kksty/HuntWeave/issues/77)（已建，状态开放）。修复直接落在 `main`，**由维护者核对本记录后关闭该 Issue**，不由本记录自行宣布结案。

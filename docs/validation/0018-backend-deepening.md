# 后端架构深化：挂起契约与停止事实的接口归属

日期：2026-10-11（Asia/Shanghai）。无对应 GitHub Issue：本轮源自一次架构评估（[后端架构深化评估](../research/2026-10-11-backend-deepening-assessment.md)），评估结论的第 1、2 条在本切片实施，其余候选按该文件第 6 节另行评估。

依据：[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)（后端先行、分层验证）、[PROJECT.md §7.1](../../PROJECT.md)（六个代码 Module 及其 Interface）。**本轮不改变六个 Module 的集合**，因此不新增 ADR——依据见评估记录第 5 节（24 份现有 ADR 中 0 份关于代码组织；直接先例是 `sandbox.py` 拆出 `sandboxprofile.py`，记在[验证记录 0011](./0011-p1-sandbox-lifecycle.md)）。

实现提交：本切片分三次提交——`e805901`（`runs/suspension.py` 与调用点）、`6c13539`（`StopFact` 与两个消费方），随后一次提交带入评估记录、本记录与索引回接。**本记录不钉自己所在那次提交的 hash**：记录内容随该提交一起产生，引用它必然自相矛盾（本记录初稿曾引用 `00c77d0`，该提交在修订本文时被 `--amend` 取代）。需要复核时用 `git log --oneline -- docs/research/2026-10-11-backend-deepening-assessment.md` 取得它。**下列计数与集合比对在工作树干净时复核过一遍**（初稿在未提交时取得，数字相同）。

## 问题与判定

**一、悬挂这条契约没有归属。** 七条原因路径各自把同样四行手抄一遍（`run.status = "waiting"` / `run.version += 1` / `task.status = "blocked"` / `agent.status = "blocked"`），再各自跟一次中断记录。评估阶段查到 **11 处** `_interrupt` 调用，但**只有 6 处**是这一统一形态：

| 形态 | 调用点 | 处理 |
| --- | --- | --- |
| run + version + task + agent + 记录 | `:338` 授权窗口过期、`:453` 预算耗尽、`:478` 计划越界、`:781` 执行失败、`:792` 调用被取消、`:804` 证据不完整 | **抽入 `suspend`** |
| 仅设 run 状态（带守卫）+ 记录 | `:686`、`:838`（`unknown()`） | 保留原样 |
| 仅记录原因，不改状态 | `:166`（`hold_for_readiness`）、`:1094`、`:1102`（`_refresh_interruption`） | 保留原样 |

**评估记录曾把这 7 处（含 `unknown()`）当作同一形态，实施时发现不成立并修正。** `unknown()`（`orchestration.py:835`）的自增是带守卫的——`if run.status not in {"pausing", "cancelling", "waiting"}` 才会设状态并递增版本，否则只记中断。强行套用统一的 `suspend` 会改变行为：run 已在 `waiting` 时它本不该递增版本。因此统一形态是 **6 处**，不是 7 处。

**二、「停止确认」从受信管理组件泄漏出来。** GLOSSARY 把停止确认判给管理组件，`operator.py` 的文档也写明管理组件自己的答案是 `stop_confirmed_at`，但这条规则写了两遍，其中一遍直接读管理组件的私有实例字典：

```
real.py:350   instance = self.manager.instances.get(instance_id)      # 私有账本
operator.py:47  if record.state in {"stopped","reclaimed"}:           # 第二份规则
```

裁决：**规则只写一次，问一次，答一份事实而不是一个布尔。** 新增 `StopFact`（`state` / `confirmed_at` / `removed`）与 `StopFact.of(record)` 作为唯一规则点；`SandboxManager.stop_fact(instance_id)` 与 `SandboxRunState.stop_facts` 是它对外与对读侧的出口。答事实而非布尔，是因为第二个消费方（回收预览）需要「确认时间」与「移除是否完成」，布尔会逼它再读一次私有账本。

**`CallLedger._stop_fact` 的接缝保留为两个适配器。** 假执行端直接答 `True`（夹具跑在本进程），真执行端去问管理组件——两者答案不同是语义上的真分歧，不是重复。改动只在真执行端**从哪里取答案**，不在两个适配器是否合并。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `runs/suspension.py`（新增） | `Suspension`（`reason_code` + `recovery_condition`，两者必填）与 `suspend(session, run, task, agent, suspension)`：写状态与版本、阻塞 task 与 agent、在 run 尚未带该原因码时插入 `InterruptionRecord` 并追加 `interrupted` 事件。**调用顺序是接口的一部分**：`_converge()` 会在控制请求后立即运行，因此记录必须在调用方的事务里写，不能推迟到后续 pass——那时 run 已带该原因码，会一条都写不出来 |
| `runs/orchestration.py` | 6 处统一形态改调 `suspend`；`unknown()` 与另外 4 处形态不同的调用点保留原样；`_interrupt` 保留（仍被 5 处使用）。顺带把 `plan()` 中重复的 agent 查询提到 `try` 之前 |
| `execution/sandbox.py` | 新增 `StopFact`（含 `of(record)` 类方法与 `CallStopState` 类型）与 `__all__` 条目；新增 `SandboxManager.stop_fact(instance_id)`；`SandboxRunState` 增 `stop_facts` 字段 |
| `execution/real.py` | `_stop_confirmed` 改为问 `manager.stop_fact(...)`；`_release` 改用 `halt_instance()` 返回的记录归属保留制品，不再读 `manager.instances` |
| `execution/operator.py` | 删除 `_stop_state`（第二份规则）；`_instance_view` 与 `_reclaimable` 改读 `state.stop_facts` |

**无行为变化是本轮的验收前提。** 两处接口改动都只改变「答案从哪里来」，不改变答案本身。

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.8.2）、`backend/.venv` Python 3.12.14（pytest 9.1.1）。本地检查在 `backend/` 目录执行；集成检查在一次性项目 `huntweave-p0-checks`（独立卷与网络、Web 端口 18000、`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1`）内执行，结束后已 `down -v` 删除。

| 行为 | 结果 |
| --- | --- |
| 静态检查 | `ruff check src tests` → **All checks passed**（含 `E501`）；`mypy --config-file pyproject.toml src` → **Success: no issues found in 50 source files**（严格类型） |
| 本地非集成检查 | `python -m pytest -m "not integration" -q` → **248 passed, 7 skipped, 41 deselected** |
| 集成检查（一次性栈） | `compose ... run --rm --no-deps checks`（该服务自带 `-m integration`）→ **41 passed, 253 deselected, 0 failed** |
| 用例集合 | 对比改动前的 `551c7a4` 与提交态，在两侧各按同一条规则（排除 `integration` 标记）收集实际会被运行的节点 ID：**253 → 255，差异恰好只有新增的两条检查**，无任何用例被删除或替换。**这一步必须按运行集合做，不能用 `--collect-only` 的直接计数**：`--collect-only` 忽略 `-m`，其计数是未过滤的全量（改动前 253、提交态 296），无法回答「实际会跑的集合是否变了」 |
| 停止事实成为唯一规则 | 新增 `test_the_stop_fact_is_the_only_statement_of_what_a_stop_means`：`creating`/`interrupted` → `unconfirmed`（未被确认的创建，谁都不能据其行动）、`ready` → `running`、`reclaimed` 无确认 → `unconfirmed` 且 `removed=true`、`stopped` 带确认 → `confirmed`、`reclaimed` 带确认 → `confirmed` 且 `removed=true` |
| 真执行端改从接口取答案 | 新增 `test_the_executor_reads_a_stop_through_the_manager_and_never_its_ledger`：读 `execution/real.py` 源文本，断言不含 `.instances` 且含 `stop_fact(` |

**新检查的有效性已单独证明，不是只看它通过。** 把 `real.py` 的 `_stop_confirmed` 临时改回「读 `manager.instances`」的写法后，该检查**失败**（`AssertionError`）；改回后**通过**。所以它锁的是这次真正改掉的那件事。

## 未达成与限制

- **本轮只由单元与集成检查覆盖，未跑靶场探针。** 改动落在 `runs/` 与 `execution/` 的内部接口，`deploy/verify_*.py` 覆盖的是容器生命周期、出口、动作与保留行为，与本轮改动的接缝不重叠，因此没有重跑；`PROJECT.md` 与规格所要求的真实执行门槛状态未变（真实执行仍未开放）。
- **「真执行端不再读私有账本」这一条由源码扫描锁住，不是行为断言。** 该性质是「本模块不伸手进那个模块」，运行时断言无法像它这样直接陈述；仓库已有同类先例（`backend/tests/test_reason_codes.py` 也是扫描源文本）。
- **集成检查需要一次性栈。** 宿主 8000 端口被开发栈占用时，必须先以 `HUNTWEAVE_WEB_PORT=18000` 起一次性栈，否则 app 容器绑定失败、全部集成用例因连接失败而报错（本轮首次即如此，属环境配置而非实现问题）。该步骤在 README 已记录。
- **`_release` 有一处极窄的行为差异未被覆盖。** 旧写法在「管理组件的账本不再认识该实例」时会早退、不登记保留制品；新写法用 `halt_instance()` 返回的记录，不早退。要走到这里必须先通过 `halt_instance`，而管理组件一旦不认识该实例，那条路径本身就会失败——所以只有「halt 成功但紧接着实例记录消失」这个瞬时窗口才会不同。没有为此构造检查。
- **本轮不改六个 Module 的集合**：`timeline/` 与 `evidence/` 两个包仍不存在（`PROJECT.md §7.1` 有名字、`§13.2` 有计划目录树）。评估记录第 4 节给出了不在本轮提升为顶层包的理由（`timeline` 的真正缺口在读侧），该判断未在本轮验证。
- **本地检查的临时目录**：本轮期间 `pytest` 的 `tmp_path` 曾因会话文件策略写入受限而报 `WinError 5`，导致 144 项 ERROR。策略放开后基线复现为 246 通过、0 失败。此项是本机会话环境的插曲，与代码无关，记录于此以免被误读为回归。

## 待人工确认

1. **本切片已提交到本地 `main`**（`e805901`、`6c13539` 与随后带入本记录的那次提交），**未推送远端**——是否推送由操作员决定。
2. **候选 3、4、6 的评估时机。** 按评估记录第 6 节，它们与 1、2 不在同一条接缝上，应在 1+2 落地并验证完成后另行评估（各开新窗口）。本记录完成即到达该边界。
3. **证据读取缺少范围校验**（`orchestration.evidence()` 有路径包含校验与逐字节 sha256+size 复核，但不检查该证据属于调用者有权读的 Run）。评估记录已按「另开切片」处置，尚未建立 Issue。

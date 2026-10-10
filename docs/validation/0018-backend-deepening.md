# 后端架构深化：挂起契约与停止事实的接口归属

日期：2026-10-11（Asia/Shanghai）。无对应 GitHub Issue：本轮源自一次架构评估（[后端架构深化评估](../research/2026-10-11-backend-deepening-assessment.md)），评估结论的第 1、2 条在本切片实施，其余候选按该文件第 6 节另行评估。

依据：[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)（后端先行、分层验证）、[PROJECT.md §7.1](../../PROJECT.md)（六个代码 Module 及其 Interface）。**本轮不改变六个 Module 的集合**，因此不新增 ADR——依据见评估记录第 5 节（24 份现有 ADR 中 0 份关于代码组织；直接先例是 `sandbox.py` 拆出 `sandboxprofile.py`，记在[验证记录 0011](./0011-p1-sandbox-lifecycle.md)）。

实现提交：本切片分三次提交——`e805901`（`runs/suspension.py` 与调用点）、`6c13539`（`StopFact` 与两个消费方），随后一次提交带入评估记录、本记录与索引回接。**本记录不钉自己所在那次提交的 hash**：记录内容随该提交一起产生，引用它必然自相矛盾（本记录初稿曾引用 `00c77d0`，该提交在修订本文时被 `--amend` 取代）。需要复核时用 `git log --oneline -- docs/research/2026-10-11-backend-deepening-assessment.md` 取得它。**下列计数与集合比对在工作树干净时复核过一遍**（初稿在未提交时取得，数字相同）。

## 问题与判定

**一、「挂起 Run」这条契约没有归属。** 六条原因路径各自把同样四行手抄一遍（`run.status = "waiting"` / `run.version += 1` / `task.status = "blocked"` / `agent.status = "blocked"`），再各自跟一次中断记录。评估阶段查到 **11 处**中断调用，但**只有 6 处**是这一统一形态：

| 形态 | 调用点 | 处理 |
| --- | --- | --- |
| run + version + task + agent + 记录 | `:453` 预算耗尽、`:478` 计划越界、`:781` 执行失败、`:792` 调用被取消、`:804` 证据不完整 | **抽入 `suspend`** |
| run + version + 记录（**不阻塞 task/agent**） | `:336` 授权窗口过期 | **抽入 `suspend`，不传 task/agent** |
| 仅设 run 状态（带守卫）+ 记录 | `:686`、`:838`（`unknown()`） | 保留原样 |
| 仅记录原因，不改状态 | `:166`（`hold_for_readiness`）、`:1094`、`:1102`（`_refresh_interruption`） | 保留原样 |

**评估记录曾把这 7 处（含 `unknown()`）当作同一形态，实施时发现不成立并修正。** `unknown()`（`orchestration.py:835`）的自增是带守卫的——`if run.status not in {"pausing", "cancelling", "waiting"}` 才会设状态并递增版本，否则只记中断。强行套用统一的 `suspend` 会改变行为：run 已在 `waiting` 时它本不该递增版本。因此统一形态是 **6 处**，不是 7 处。

**授权窗口过期是第 7 处，但不是统一形态，本记录初稿在这里出过一次错。** 它只设 `run.status` 与 `run.version`，**不**阻塞 task 与 agent——那里 Run 等的是新授权，不是这个 task。初稿的重构把五处的形态套到了它上面，改变了 task/agent 状态；`claim()` 会选 `blocked` 的任务，所以这是可观测且承重的行为变化。两轴评审的 Spec 轴抓到了它，已改为 `suspend` 的可选参数：只有真正「这个 task 做不下去了」的路径才传 task 与 agent。详见文末「本记录的更正」。

**二、「停止确认」从受信管理组件泄漏出来。** GLOSSARY 把停止确认判给管理组件，`operator.py` 的文档也写明管理组件自己的答案是 `stop_confirmed_at`，但这条规则写了两遍，其中一遍直接读管理组件的私有实例字典：

```
real.py:350   instance = self.manager.instances.get(instance_id)      # 私有账本
operator.py:47  if record.state in {"stopped","reclaimed"}:           # 第二份规则
```

裁决：**规则只写一次，问一次，答一份事实而不是一个布尔。** 新增 `StopFact`（`state` / `confirmed_at` / `removed`）与 `StopFact.of(record)` 作为唯一规则点；`SandboxManager.stop_fact(instance_id)` 与 `SandboxRunState.stop_facts` 是它对外与对读侧的出口。取事实而非布尔形态的依据是这三件事由管理组件一次性知道，让未来的读侧不必再读它的私有账本；**但要如实说明：目前没有任何消费方读 `confirmed_at` 与 `removed`**（回收预览与真执行端都只用 `state`），本记录初稿把它们说成「第二个消费方需要」是推断而非事实。

**`CallLedger._stop_fact` 的接缝保留为两个适配器。** 假执行端直接答 `True`（夹具跑在本进程），真执行端去问管理组件——两者答案不同是语义上的真分歧，不是重复。改动只在真执行端**从哪里取答案**，不在两个适配器是否合并。

## 实现

| 改动点 | 行为 |
| --- | --- |
| `runs/suspension.py`（新增） | `Suspension`（`reason_code` + `recovery_condition`，两者必填）、`record_interruption(session, run, reason, condition)`（唯一的中断记录点：已在同原因码时跳过，写 `InterruptionRecord` 并追加 `interrupted` 事件，然后设 `run.reason_code`）与 `suspend(session, run, suspension, *, task=None, agent=None)`：写状态与版本，**仅在传入时**阻塞 task 与 agent，再调 `record_interruption`。`task`/`agent` 只允许成对传入，传一半直接拒绝。**调用顺序是接口的一部分**：`_converge()` 会在控制请求后立即运行，因此记录必须在调用方的事务里写，不能推迟到后续 pass——那时 run 已带该原因码，会一条都写不出来 |
| `runs/orchestration.py` | 6 处统一形态改调 `suspend`（其中授权窗口过期不传 task/agent，保持原行为）；`unknown()` 与另外 4 处形态不同的调用点保留原样；**删除 `_interrupt`**，它余下的 5 个调用点改为直接调 `record_interruption`，中断记录由此只有一处实现。**未采纳**把 `plan()` 的 agent 查询提前到 `try` 之前（那会让「授权过期且 agent 缺失」从干净返回变成断言失败），保持原位 |
| `execution/sandbox.py` | 新增 `StopFact`（含 `of(record)` 类方法与 `CallStopState` 类型）与 `__all__` 条目；新增 `SandboxManager.stop_fact(instance_id)`；`SandboxRunState` 增 `stop_facts` 字段 |
| `execution/real.py` | `_stop_confirmed` 改为问 `manager.stop_fact(...)`；`_release` 改用 `halt_instance()` 返回的记录归属保留制品，不再读 `manager.instances` |
| `execution/operator.py` | 删除 `_stop_state`（第二份规则）；`_instance_view` 与 `_reclaimable` 改读 `state.stop_facts`，并在缺失时回退到 `StopFact.of(record)`，使一个未带 `stop_facts` 的 `SandboxRunState` 仍是可读的答案而不是 `KeyError` |
| `tests/test_the_suspension_contract.py`（新增） | 6 条契约检查：状态与版本成对、可选阻塞、成对约束、同原因码去重、异原因码另行记录、`record_interruption` 不改 Run 状态 |
| `tests/test_reason_codes.py` | 原因码扫描器的锚点从 `_interrupt` 改为 `record_interruption`。**这不是纯改名**：初版函数名 `interrupt` 与 LangGraph 自己的原语重名，`harness/graph.py:67` 的调用被扫描器误当成原因记录，于是 `pending_call_ids` 这个字典键被报成「后端说了但控制台没有文案的原因码」 |

**本轮的前提是「结构变、行为不变」。** 两处接口改动只应改变「答案从哪里来」，不应改变答案本身。**这一前提在初稿中被破坏过一次**（授权窗口过期位点被套上了阻塞 task/agent 的形态），由两轴评审的 Spec 轴发现并已修正；修正后该位点的可观察效果与改动前一致，六条恢复条件文案经逐字节比对未变。

## 验证

环境：Windows 11 x86_64、Docker Desktop（Engine 29.8.2）、`backend/.venv` Python 3.12.14（pytest 9.1.1）。本地检查在 `backend/` 目录执行；集成检查在一次性项目 `huntweave-p0-checks`（独立卷与网络、Web 端口 18000、`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1`）内执行，结束后已 `down -v` 删除。

| 行为 | 结果 |
| --- | --- |
| 静态检查 | `ruff check src tests` → **All checks passed**（含 `E501`）；`mypy --config-file pyproject.toml src` → **Success: no issues found in 50 source files**（严格类型） |
| 本地非集成检查 | `python -m pytest -m "not integration" -q` → **254 passed, 7 skipped, 41 deselected**（更正后；初稿为 248，见文末「本记录的更正」） |
| 集成检查（一次性栈） | `compose ... --profile verify build checks`（**必须先重建**，否则跑的是缓存镜像里的旧代码与旧测试）→ `compose ... run --rm --no-deps checks`（该服务自带 `-m integration`）→ **41 passed, 261 deselected, 0 failed**。`261` 与本地非集成运行集合一致，是这次确实跑在当前代码上的判据 |
| 用例集合 | 对比改动前 `551c7a4` 与提交态，两侧用**同一条命令**收集实际会被运行的节点 ID（`-m "not integration"`，`--collect-only` 是认 `-m` 的）：**253 → 261，差异恰好只有新增的六条契约检查**，无任何用例被删除或替换。**本记录初稿在这里写错过一次**：当时用了不带 `-m` 的 `--collect-only` 计数作对比，那是未过滤的全量（302），与前一次测量的口径不同，据此得出的「集合相同」结论不成立——见文末「本记录的更正」 |
| 挂起契约 | 新增 `tests/test_the_suspension_contract.py` 6 条：状态与版本成对移动、传 task/agent 时两者都被阻塞、**不传时两者都不被阻塞**（授权窗口过期那条路径的行为，即初稿改错又改回的那一处）、只传一半被拒绝、同原因码不重复记录、`record_interruption` 记录原因但不改变 Run 状态 |
| 恢复条件文案未变 | 把新文件里相邻字符串字面量还原成逻辑字符串后，六条恢复条件与 `551c7a4` 逐字节一致（折行未改动任何空格） |
| 停止事实成为唯一规则 | 新增 `test_the_stop_fact_is_the_only_statement_of_what_a_stop_means`：`creating`/`interrupted` → `unconfirmed`（未被确认的创建，谁都不能据其行动）、`ready` → `running`、`reclaimed` 无确认 → `unconfirmed` 且 `removed=true`、`stopped` 带确认 → `confirmed`、`reclaimed` 带确认 → `confirmed` 且 `removed=true` |
| 真执行端改从接口取答案 | 新增 `test_the_executor_reads_a_stop_through_the_manager_and_never_its_ledger`：读 `execution/real.py` 源文本，断言不含 `.instances` 且含 `stop_fact(` |

**新检查的有效性已单独证明，不是只看它通过。** 把 `real.py` 的 `_stop_confirmed` 临时改回「读 `manager.instances`」的写法后，该检查**失败**（`AssertionError`）；改回后**通过**。所以它锁的是这次真正改掉的那件事。

**两轴评审（Standards / Spec）在提交后执行，按 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md) 的约定。** 两轴都以改动前的 `551c7a4` 为固定点、各由一个子代理独立完成，结论并排而不合并。它们各自发现了本记录初稿的错误，是本记录出现「本记录的更正」一节的直接原因：

| 轴 | 抓到的实质问题 |
| --- | --- |
| Spec | **一处真实的行为变化**：授权窗口过期位点被套上了阻塞 task/agent 的形态，而旧代码不阻塞；`claim()` 会选 `blocked` 的任务，因此可观测且承重。另有 §4.1「版本保持就地可见」未被实施采纳、§4.3 的理由说过头（`confirmed_at`/`removed` 没有消费方）、`plan()` 的 agent 查询被提前而改变失败模式 |
| Standards | **`run.version += 1` 计数错误**（写作 13，实为 18）并已沿用到提交信息与本记录；提交信息里「stays inline at every site」是假陈述；`suspension.py` 重写了 `_interrupt` 的守卫与记录形状（真重复） |

两轴也都给出了正面结论，一并记下以免只留下批评：`StopFact.of` 与它取代的**两份**旧规则逐字相符；六条恢复条件文案未变；`runs/suspension.py` **通过删除测试**（是深模块而非传声筒：删掉它，`append_event` 必须在调用方事务内、以及同原因码跳过这两件事会重新散到六个位点）；运行集合比对、README/STATUS 回接、以及「不开 ADR」的判断都被复核为成立。

## 未达成与限制

- **本轮只由单元与集成检查覆盖，未跑靶场探针。** 改动落在 `runs/` 与 `execution/` 的内部接口，`deploy/verify_*.py` 覆盖的是容器生命周期、出口、动作与保留行为，与本轮改动的接缝不重叠，因此没有重跑；`PROJECT.md` 与规格所要求的真实执行门槛状态未变（真实执行仍未开放）。
- **「真执行端不再读私有账本」这一条由源码扫描锁住，不是行为断言。** 该性质是「本模块不伸手进那个模块」，运行时断言无法像它这样直接陈述；仓库已有同类先例（`backend/tests/test_reason_codes.py` 也是扫描源文本）。
- **集成检查需要一次性栈。** 宿主 8000 端口被开发栈占用时，必须先以 `HUNTWEAVE_WEB_PORT=18000` 起一次性栈，否则 app 容器绑定失败、全部集成用例因连接失败而报错（本轮首次即如此，属环境配置而非实现问题）。该步骤在 README 已记录。
- **`_release` 有一处极窄的行为差异未被覆盖。** 旧写法在「管理组件的账本不再认识该实例」时会早退、不登记保留制品；新写法用 `halt_instance()` 返回的记录，不早退。要走到这里必须先通过 `halt_instance`，而管理组件一旦不认识该实例，那条路径本身就会失败——所以只有「halt 成功但紧接着实例记录消失」这个瞬时窗口才会不同。没有为此构造检查。
- **本轮不改六个 Module 的集合**：`timeline/` 与 `evidence/` 两个包仍不存在（`PROJECT.md §7.1` 有名字、`§13.2` 有计划目录树）。评估记录第 4 节给出了不在本轮提升为顶层包的理由（`timeline` 的真正缺口在读侧），该判断未在本轮验证。
- **本地检查的临时目录**：本轮期间 `pytest` 的 `tmp_path` 曾因会话文件策略写入受限而报 `WinError 5`，导致 144 项 ERROR。策略放开后**改动前基线**复现为 246 通过、0 失败。（这两个数字是**改动前**的基线，不是本轮结果；本轮结果见表内 254 通过。）

## 本记录的更正（2026-10-11，两轴评审后）

按 `docs/validation/README.md:30`，更正另立一节，不改写上文已经成立的结论。

1. **「七条原因路径」应为六条**，授权窗口过期是形态不同的第 7 处。已在上文更正。
2. **初稿的重构改变了一处行为**：授权窗口过期位点原本不阻塞 task/agent，初稿让它阻塞了。这是本轮「结构变、行为不变」前提的唯一破例，由 Spec 轴发现。修正方式是把 task/agent 改为 `suspend` 的可选参数；已新增检查直接锁住「不传时不阻塞」。
3. **`run.version += 1`：初稿写作 13 处，实为 18 处**（其中 6 处属挂起形态）。该错数字曾沿用到提交信息与评估记录，两处均已更正。
4. **提交信息里「`run.version += 1` stays inline at every site」是假陈述**——实施把状态与版本成对放进了 `suspend`。这是设计选择，但当时的措辞与实施不符；评估记录 §4.1 已按实施后的形态更正说明。
5. **§4.3「第二个消费方需要确认时间与移除标志」是推断而非事实**：`StopFact.confirmed_at` / `removed` 目前没有消费方。事实形态的依据改为「这三件事由管理组件一次性知道」，并如实标注当前无人读取。
6. **`record_interruption` 的名字是评审后改的**（初版 `interrupt` 与 LangGraph 原语重名，导致原因码扫描器误报 `pending_call_ids`）；扫描器锚点随之更正。
7. **用例集合比对的初稿口径不成立**。初稿用不带 `-m` 的 `--collect-only` 计数，与前一次测量的口径不同；改用两侧同一条命令后，实测 **253 → 261，净增六条契约检查**。
8. **新增了六条契约检查**，其中四条是评审后才补的——初稿只加了两条，且都没有触碰 `suspend()` 本身，而 `suspend()` 正是本轮新增行为的所在。
9. **集成检查的镜像必须重建，初稿忽略了这一点。** `huntweave-checks:p0` 是构建缓存里的产物；不先 `--profile verify build checks` 就 `run`，跑的是**旧镜像里的旧代码与旧测试**。本记录初稿写下的「41 passed」正是这样取得的——当时镜像内只有 25 个测试文件、不含新测试，且 `deselected` 显示为 253（新增检查之前的运行集合大小）而非 261。重建后复跑为 **41 passed, 261 deselected**，这才是落在本切片代码上的结果。判据是 `deselected` 的数字与本地收集数一致。

## 待人工确认

1. **本切片已提交并推送**到 `origin/main`（`e805901`、`6c13539` 与随后带入本记录的那次提交）。评审后的修正以新提交追加。
2. **候选 3、4、6 的评估时机。** 按评估记录第 6 节，它们与 1、2 不在同一条接缝上，应在 1+2 落地并验证完成后另行评估（各开新窗口）。本记录完成即到达该边界。

## 本记录的更正（续）：一处撤回

**初稿的第 3 条待办「证据读取缺少范围校验」已整条撤回，撤回后没有留下待办。** 它以为存在「调用者有权读的 Run」这种可检查对象，但本产品按 `PROJECT.md §3.2` 不建设账号体系、永久排除多租户，`WebSession`（`storage/models.py:25-35`）与 `Project`（`:38-43`）都没有 owner 字段——会话即全权。该路由已有的保护是会话鉴权（`api/app.py:326` 的 `public` 白名单不含它）、路径包含校验（`orchestration.py:1516`）、逐字节 sha256+size 复核（`:1530`）与按 `EvidenceView` 契约作答的集成检查（`tests/test_orchestration_integration.py:260`）。

**撤回过程中我又错了一次**：改说「真正缺的是那条集成检查」——而那条检查已经存在且通过，就在上面引用的位置。两次都是同一个毛病：把推断当事实写进文档。评估记录「本记录的更正」中标题为「整条撤回」的那一条记着同一件事。

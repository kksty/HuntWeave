# 延后候选的深化评估：保留路由、Runner 链接与测试运行时

日期：2026-10-11。本文件是设计评估，**不是实现**，也不表示任何能力已交付。评估对象是[上一轮后端深化评估](./2026-10-11-backend-deepening-assessment.md)第 6 节明确未涉及的三条候选——选择性保留路由的四处转抄、`RunnerClient` 包装层、测试运行时搬出测试文件。阶段状态仍只在 [STATUS](../STATUS.md) 维护，本文件不复述它。

## 1. 评估范围与方法

取材沿用上一轮的判据：按**变更频率**而非行数选择要评估的地方，并优先给最近被改动的部分加权。

方法为静态对照加一次只读事实核验：源码、`GLOSSARY.md`、`docs/adr/`、`PROJECT.md`、`backend/tests/`、`deploy/` 与 `.github/workflows/checks.yml`。三条候选各由独立核验逐句对照源码，本文只登记**已查证**的事实；凡核验中与候选陈述不符之处记入第 7 节。本轮不启动容器、不接触任何目标、不改动任何被测行为，也不改动仓库文件（核验为只读）。

核验基点：`HEAD = 4f0d063`，与候选所据的评审报告同日，因此报告中的行号在本次核验中**没有漂移**；下文引用的行号均为当前工作树。

除特别说明，代码路径相对 `backend/src/huntweave/`；测试路径相对 `backend/tests/`。

## 2. 变更频率基线

最近 25 个提交中各路径被触碰的次数（按提交计，一次提交内多次改动记一次）：

| 路径 | 次数 | 路径 | 次数 |
| --- | --- | --- | --- |
| `runs/orchestration.py` | 5 | `execution/client.py` | 2 |
| `execution/sandbox.py` | 4 | `execution/server.py` | 2 |
| `tests/test_sandbox_lifecycle.py` | 4 | `tests/_fakeruntime.py` | 2 |
| `tests/test_real_execution.py` | 4 | `execution/retention.py` | 1 |
| `api/app.py` | 3 | `contracts/retention.py` | 1 |
| `frontend/src/workspace.ts` | 3 | `runs/dispatch.py` | 0 |
| | | `api/readiness.py` | 0 |

同一窗口内改动最多的是文档（`docs/STATUS.md` 12、`PROJECT.md` 10、`README.md` 8）。三点结论：候选 6 所在的测试簇是代码侧第二热的位置（仅次于 `orchestration.py`）；候选 3 的两个所有者模块各只被改过一次；候选 4 的两个消费方文件在窗口内**一次都没被改动**。

## 3. 代码与现象依据

### 3.1 保留动作词汇有两个互相竞争的 canonical 字面量

选择性保留的传输形状在四个 Python/TS 面与一个探针面上重复，但真正的问题不是"路由写了几遍"，而是**同一组名字有两份定义**：

```
contracts/retention.py:31   RetentionAction = Literal["pin","unpin","delete","sweep"]
contracts/retention.py:32   RetentionTarget = Literal["version","artifact","deployment"]
client.py:17                RetentionTargetKind = Literal["versions","artifacts"]
client.py:18                RetentionAction = Literal["pin","unpin","delete"]
```

`execution/retention.py:39,44` 导入 contracts 的那一对，而 `api/app.py:59-61` 从 `execution.client` 导入 `RetentionAction` 与 `RetentionTargetKind`。也就是说 `RetentionAction` 这个名字在两个模块里指**不同的成员集合**（含不含 `sweep`），单复数也不同（`version` 对 `versions`）。两侧只在 HTTP 上相遇，所以 mypy 只能各查一段，无法发现这处漂移——这正是 [PROJECT.md:396](../../PROJECT.md) 要求「至少统一 `reason_code`、关联 ID 和版本冲突结果」时同类问题的一半。

路由的重复是真实的：六个动作路由在每一个 Python 面上近乎逐字重复六次（`api/app.py:583-667` 与 `execution/server.py:364-442` 各自六段），加上一个 GET、一个 `/sweep`、`client.py:84,95` 的 f-string 路径、`frontend/src/workspace.ts:303,305` 的 TS 路径，以及 `lab/isolation/retention.py` 自己硬写的 runner URL（约 12 处）。**报告漏掉了最后这个面。**

但这份重复**尚未被付过成本**：整个功能在一个提交里落地（`219fa58`，#20），此后 `execution/retention.py` 与 `contracts/retention.py` 各只被改动一次，且没有任何提交新增或改名过一个保留动作。`api/app.py:240-241` 的显式 422 拒绝与 `artifact_id` 的 UUID 强转是这种手写形状换来的东西，改成"每面一张薄表"会失去它们。

### 3.2 Runner 链接：传输重复是真的，"三种策略"是低估，而且不能合并

报告称同一远端有三种错误处理策略；实测是 **9 个入口**（`api/app.py:167,179,201,225`、`api/agentd.py:48` 的 5 处 `RunnerClient()`，加上 `client.py:126-135` 手写的 `get_capabilities` 被 `api/app.py:155`、`api/agentd.py:37`、`api/healthcheck.py:21`、`execution/healthcheck.py:8` 四处调用），调用方可见的转换 **≥6 种**：观测缺口视图（`api/app.py:187,189,208,210`）、原样透出的 503/409（`:249-252`）、`None`（`runs/dispatch.py:27`）、落库的 `execution_unreachable`（`dispatch.py:104`）、`hold_for_readiness("real_execution_not_ready")`（`dispatch.py:73-75`）、`execution_ready=false`（`api/readiness.py:33`），再加两个健康检查的进程退出码。`api/readiness.py:21` 的 5 秒 TTL **不是错误策略**：该文件 `:15-18` 自己写明它只服务展示，派发路径从不查它。

这些**语句**不能合并，且差异由文档要求：读必须如实报观测缺口而不是"缓存为空"（验证记录 `0015`、`0017`）；命令必须以执行端自己的 `reason_code` 可见失败（`api/app.py:248-252`）；对账不得把"没答"变成裁定（`dispatch.py:20-23`，AGENTS.md「未知副作用不得盲目重试」）；真实就绪门必须 fail closed（`api/agentd.py:29-39`，ADR-0010）。把 `read/command/probe/hold` 做成 1:1 转调加一个 policy 参数，正是 [PROJECT.md:394](../../PROJECT.md) 禁止的「仅转发参数的公共类」。

站得住的那一半是三处**传输性**重复：`get_capabilities` 自带的第二份 `httpx.get`；`"runner_unavailable"` 字面量 8 处（`api/app.py:128,185,189,206,210,252`、`api/readiness.py:33`、`execution/healthcheck.py:12`）；以及"不可达"的异常集合在各处**不一致**：

```
runs/dispatch.py:26,99      except (httpx.HTTPError, OSError, TimeoutError)
api/app.py:188,251          except (httpx.HTTPError, OSError, TimeoutError, ValueError)
api/readiness.py:32         except (httpx.HTTPError, ValidationError, ValueError)
```

这处不一致有具体后果，且后果不在本轮定性范围内：`client.py:107` 的 `ExecutionRecord.model_validate(...)` 会对畸形响应抛 `ValidationError`（⊂ `ValueError`），`dispatch.py:63` 的 `runner.query()` 在 `:99` 的 guard 之外，异常会穿过 `dispatcher.sweep()`（`api/agentd.py:100` 只捕获 `ServiceError`）落到 `api/agentd.py:114`，打印 `scheduler_unavailable` 并 `return 1`——调度循环退出，而另外两处同类读取把它当作观测缺口。**这是"疑似缺陷"，不是结构问题**：它需要一条已经能红的复现才能定性（是否属于有意的 fail-fast），因此单独走缺陷诊断，不混进结构收敛。

### 3.3 测试运行时：夹在检查文件里的假件只有一处，但耦合是真的

`backend/tests` 既没有 `conftest.py` 也没有 `__init__.py`，`pyproject.toml:29-30` 只设 `pythonpath = ["src"]`、`testpaths = ["tests"]`；测试文件之间能互相导入，靠的是 pytest 把 `backend/tests` 放进 `sys.path`。当前跨文件导入共 **4 条边、5 个文件**：

```
test_real_execution.py:19   from test_sandbox_lifecycle import PROFILES, REPOSITORY, FakeRuntime  # noqa: F401
test_retention.py:19-21     from test_real_execution import …  +  from test_sandbox_lifecycle import PROFILES, FakeRuntime
test_real_execution.py:535  from test_fake_runner import ticket as fake_ticket_builder
test_operator_view.py:13    from _fakeruntime import FakeRuntime        # 直连，不经生命周期文件
```

真正的味道是 `:19` 那句 `# noqa: F401`——其它套件依赖的是**再导出**，不是夹具；`test_operator_view.py:29` 还自己重复了一份 `PROFILES`。测试专用模块只有一个（`_fakeruntime.py`，344 行），不是报告说的两个。

`ContainerRuntime`（`execution/sandbox.py:200`）的公开操作数是 **23**，`FIXED_OPERATIONS`（`tests/test_sandbox_lifecycle.py:57`）同是 23 项，`FakeRuntime` 逐项实现；两个适配器是 `DockerRuntime`（生产）与 `FakeRuntime`（唯一测试适配器）。这印证了 [PROJECT.md:396](../../PROJECT.md) 关于"执行端提供真实与确定性测试 Adapter"的表述，但 `FakeRuntime` 是唯一测试适配器，而不是"两个之中的第二个"。

代价必须正视，报告都低估了：`tests/support/` 今天**不可导入**（无 `__init__.py`，`pythonpath` 不含 `tests`），只有 conftest 是免改路径的安全做法；夹具重名已经存在（`manager` 在 `test_retention.py:77` 与生命周期文件 `:97-125`，`settled`/`real_ticket` 在三处各自定义）；`real_ticket/settled/build` 这一组比报告说的"假运行时 + profile 助手"更宽，是一套共享的 runner 场景夹具。最后，它**不增加任何覆盖**，按 AGENTS.md「不增加无意义测试」与 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md)，收益只能记在可维护性上。

## 4. 取舍

| 候选 | 方案 | 收益 | 代价 |
| --- | --- | --- | --- |
| 保留词汇 | 把 `client.py:17-18` 折到 `contracts/retention.py:31-32`，路径模板与 `/sweep` 各导出一份 | 两个竞争字面量归一；一个动作词汇一个名字 | 需要先定单复数与 `sweep` 归属（唯一的语义决定） |
| 保留词汇 | 每面一张薄表（报告原方案） | 新动作一处编辑 | 约 100–150 行新模块与 28 条声明重写只省约 40 行；失去显式 422 与 UUID 强转；把变化挪进一个循环 |
| 保留词汇 | 完全不动 | 零改动 | `RetentionAction` 继续在两个模块里指不同集合 |
| Runner 链接 | 一条传输 + 各调用点保留自己的策略，并收敛异常集合与 `runner_unavailable` | 9 个入口收敛到 1 个客户端 + 1 个谓词；消除 guard 不一致 | 触及 `dispatch.py`/`api/app.py`/`readiness.py` 与两个健康检查；改名会牵动 `frontend/src/workspace.ts` 与 `test_reason_codes.py` 的机械检查 |
| Runner 链接 | 统一成一条操作员可见的"不可达"陈述 | 界面用一句话表达同一件事 | 删除失败与读不到容器列表显示相同——破坏读/命令的语义区分 |
| Runner 链接 | `RunnerLink(read/command/probe/hold)` 适配器 | 一个适配器名字 | 1:1 转调加 policy 参数，属 PROJECT.md:394 禁止的转发类；一个适配器＝假想接缝 |
| 测试运行时 | 假件与 profile 搬进可导入的 `tests/support/`（两步） | 去掉 `# noqa: F401` 再导出与 `PROFILES` 重复；检查文件只做检查 | 需引入 `tests/support/__init__.py` 或 conftest 的 sys.path 处理；夹具重名要分批合并 |
| 测试运行时 | 只并夹具（conftest 化） | 不改文件布局 | 不解决"检查文件被当作库"的耦合 |

## 5. 本轮结论（2026-10-11，用户确认按推荐执行）

1. **候选 3 冻结，只做字面量归一。** 不做路由框架、不建 `RetentionCommands`；把 `client.py:17-18` 折到 `contracts/retention.py:31-32`（先定单复数与 `sweep` 归属），路径模板与 `/sweep` 各导出一份。理由不是"重复不存在"，而是这份重复**从未被付过成本**，而按本仓库自己的判据——被约束的输入还不存在时写下的不是规格而是猜测——为一次未发生的变更预先重构协议面，收益是推断的，代价是可计数的。
2. **候选 4 收窄为传输收敛。** 一条传输、各调用点保留自己的策略；收敛 `get_capabilities` 的重复请求、`"runner_unavailable"` 字面量与"不可达"异常集合。**不引入 `RunnerLink`**：它会把必要差异藏进一个转发层，并撞上 PROJECT.md:394。
3. **候选 4 附带的那处 guard 不一致单独处理。** 它是疑似缺陷而非结构问题，按缺陷诊断流程走：先取得一条已经能红的复现，再决定它是缺陷还是有意 fail-fast。**不并入**第 2 条的结构改动——否则一旦行为变化，无法判断是收敛引入的还是缺陷暴露的。
4. **候选 6 分两步，作为维护切片。** 第一步只把 `_fakeruntime.py` 与 `PROFILES/REPOSITORY` 移进可导入的 `tests/support/`，让生命周期文件只做检查；第二步才处理 `manager`/`settled`/`real_ticket` 的重名与合并。验收要求既有四个套件计数不变，**不新增测试**；不涉及前端，因此不触发浏览器验收（ADR-0018）。
5. **顺序：排在当前 P1 可执行前沿之后。** 三条都不阻塞既有切片；候选 6 可随任一触及该测试簇的切片顺带完成。本轮不改变阶段顺序，也不表示任何一条已开工。
6. **本轮不开 ADR。** 沿用上一轮第 4.5 条的判据：三条都是模块内部的接缝与所有权整理，不改六个 Module 的集合，也不改核心选型；`docs/adr/README.md` 的触发条件是核心架构或产品边界变化。
7. **本轮不改 `PROJECT.md` 与 `STATUS.md`。** 结论未实施，PROJECT 的 Interface 说明与 STATUS 的下一实施项都还不需要改；实施切片落地时按 `docs/validation/README.md` 的约定新增验证记录并同步 STATUS。
8. **候选 6 不在 `GLOSSARY.md` 增词。** `_fakeruntime.py` 是测试适配器，不是操作员或研究者会说的领域词；词汇表不承载实现细节（上一轮第 4.6 条同一判据）。

## 6. 明确排除

- **`lab/isolation/retention.py` 硬写的 runner URL 不作为独立候选。** 该文件是探针，产物是宿主 profile 的验收证据而非产品代码；它与产品的耦合形式（自己写 URL 以独立见证）与 3.1 的产品内转抄不同类。若探针开始需要跟随动作词汇变化，按 3.1 的归一结果顺带处理，不单独立项。
- **保留动作的响应形状分支不合并。** `client.py:87-89` 的"删除返回报告、其余返回视图"分支与 `api/app.py:257-277` 的代理加留痕包装各有分工，`docs/specs/0002-real-execution.md:232`「控制面只代理与留痕」是它们必须保持的边界；共享模块只能带词汇与路径，不能带决定。
- **前端不做代码生成。** `frontend/src/workspace.ts:303,305` 的两行路径是唯一可行的 TS 侧表达，跨语言生成会引入一条新工具链而没有对应的变更频率。

## 7. 未决与限制

- **本记录更正了来源报告的若干说法**（行号本身全部成立）：(a) 保留路由是 7 条/面（6 动作 + 1 GET），且报告漏了 `lab/isolation/retention.py` 这个面；(b) "两个测试专用模块"实为一个，`backend/tests` 无 `conftest.py` 与 `__init__.py`；(c) "在 `test_sandbox_lifecycle.py:130` 逐方法断言"不成立——`:130` 是注释分隔线，真正的机制是 `:133-139` 的集合比对，协议操作数是 23 不是 24；(d) "三种错误处理策略"低估为 9 个入口、≥6 种转换，且 5 秒 TTL 是展示新鲜度而不是错误策略；(e) `api/readiness.py` 的失败→缺口在 `:32-34`，不在报告引用的 `:12-27` 内。
- **`dispatch.py` 的 guard 不一致未复现。** 3.2 描述的路径是静态读出的，没有构造过畸形响应，因此它是"疑似缺陷"而非已确认缺陷；定性归缺陷诊断切片。
- **本轮所有结论均未经实现验证。** 落地后按 `docs/validation/` 体例新增验证记录，实际计数与命令输出写入该记录，不在本文件复述。
- **上一轮的两处历史更正仍然有效**（`run.version` 计数、授权窗口过期位点、证据范围校验整条撤回），本文件不复述，见[上一轮记录](./2026-10-11-backend-deepening-assessment.md)的「本记录的更正」。

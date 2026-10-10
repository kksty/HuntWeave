# 后端架构深化评估

日期：2026-10-11。本轮修订是设计评估，**不是实现**，也不表示任何能力已交付。结论落点为 [PROJECT §7.1](../../PROJECT.md) 的 Interface 说明、一份新的验证记录，以及明确的「不开 ADR」判断。阶段状态仍只在 [STATUS](../STATUS.md) 维护。

## 1. 评估范围与方法

评估取材按**变更频率**而非行数决定：最近 25 个提交中反复出现的文件是 `runs/orchestration.py`、`execution/sandbox.py`、`execution/real.py`、`execution/ledger.py`、`execution/dockerruntime.py`、`api/app.py`，下面每条结论都落在其中之一。

方法为静态对照：源码、`GLOSSARY.md`、`docs/adr/`、`PROJECT.md`、`backend/tests/` 与 `.github/workflows/checks.yml`。本轮不启动容器、不接触任何目标，不改动任何被测行为。

规模基线。**本文件所有「非空行」由 `Get-Content | Measure-Object -Line` 计得（跳过空行），所有「物理行」由 `Measure-Object` 直接计得；两种数不混用。** 引用计数时一律标出是哪一种。

- `backend/src/**/*.py`：10,349 非空行（`backend/src` 全部文件含迁移与 `pyproject.toml` 为 10,874 物理行）。
- `backend/tests/**/*.py`：6,139 非空行。
- `lab/**`：2,951 非空行（含 `lab/isolation/Dockerfile` 47 行）。
- 单文件：`execution/sandbox.py` 1,583 非空行；`runs/orchestration.py` 1,550 物理 / 1,482 非空；`execution/retention.py` 755 非空；`execution/ledger.py` 705 非空。
- 包分布（物理行）：`execution` 6,488、`runs` 2,175、`contracts` 1,115、`api` 977、`storage` 330、`harness` 259、`access` 190。

## 2. 代码与现象依据

### 2.1 「挂起 Run」这条契约没有归属

同四行在 `runs/orchestration.py` 被手抄 7 次：

```
run.status = "waiting"     # :336 授权窗口过期   :777 执行失败
run.version += 1           # :449 预算耗尽       :788 调用被取消
task.status = "blocked"    # :474 计划越界       :800 证据不完整
agent.status = "blocked"   # :836 unknown()
```

其中 6 处还各自跟一次 `_interrupt(session, run, 原因, 恢复条件)`。`_converge()`（`:1138`）第三次重写了这套级联，服务的是暂停/取消而非 research 失败。

`run.version += 1` 全文件共 **13 处**，另外 6 处不是挂起：`:250` 启动、`:403`/`:407` 进入下一角色 / 自动阶段结束、`:1190` 关闭、`:1209` 请求暂停、`:1216` 请求取消。`run.version` 是 `control()`（`:1177`）比对并据此返回 `version_conflict` 的栅栏，也是 `PROJECT.md:394` 列为必须统一的「版本冲突结果」。

### 2.2 「停止确认」从受信管理组件泄漏出来

`GLOSSARY.md:170` 把停止确认交给受信管理组件，`execution/operator.py:1-15` 的文档也写明管理组件自己的答案是 `stop_confirmed_at`。但这条规则写了两遍，其中一遍读私有账本：

```
real.py:350     instance = self.manager.instances.get(instance_id)      # 私有实例字典
real.py:353     return instance.state in {"stopped","reclaimed"} and instance.stop_confirmed_at is not None
operator.py:47  if record.state in {"stopped","reclaimed"}:
operator.py:48      return "confirmed" if record.stop_confirmed_at is not None else "unconfirmed"
```

`SandboxManager` 的 `instances` / `sessions` 是**公开属性**而非接口方法（`sandbox.py:670`），因此调用方可以绕过接口重构这条判断。三处测试也顺着这个口子断言实现（`test_operator_view.py:113`、`:205`、`:235` 断言 `isinstance(manager.runtime, FakeRuntime)`）。

`CallLedger._stop_fact` 这个接缝本身是**真接缝**：`execution/fake.py:38` 的假执行端直接答 `True`（夹具跑在本进程），`execution/real.py:355` 的真执行端去问管理组件。两者答案不同是语义上的真分歧，不是重复。

### 2.3 原因码词汇没有归属模块

`reason_code` 是散落的字符串字面量：`contracts/errors.py:2`（`ServiceError.reason_code`）、`contracts/{runs,execution,orchestration,retention,capabilities}.py`、`runs/orchestration.py`（24 处）、`execution/ledger.py`、`execution/server.py`、`api/*`。唯一声明的词汇表是 `execution/retention.py:93` 的 `RetentionReason = Literal[...]`，只覆盖它自己那 13 条。

两侧一致性由**文本扫描**维持，而不是由接口：`backend/tests/test_reason_codes.py:1-14` 自述「deliberately literal: it collects string literals that *look* like reason codes」，配 `:40` 起的约 120 条 `NOT_REASON_CODES` 白名单，再与 `frontend/src/workspace.ts:116-232` 的 117 条手写映射对照。

### 2.4 六个 Module 里有两个只有名字

`PROJECT.md §7.1`（`:383-392`）列六个代码 Module：`access`、`runs`、`harness`、`execution`、`evidence`、`timeline`。实际只有前四个是包（另有 `api`、`contracts`、`storage` 三个表外包）。`grep huntweave.timeline` / `huntweave.evidence` 全后端**零命中**；`PROJECT.md §13.2`（`:957-962`）把两者画在计划目录树里。

职责的实际分布：

| 归属 | 时间线 | 证据 |
| --- | --- | --- |
| 写入 | `runs/events.py`：`append_event`，单一公开函数，行锁内分配游标 | `execution/archive.py`：脱敏、截断标注、配额、sha256/size、uuid5 id、根目录包含、原子写 |
| 读取 | `runs/orchestration.py:1461` `history()`；`api/app.py:518` SSE 端点 | `runs/orchestration.py:1493` `evidence()` |
| 其他 | `execution/ledger.py:332` 另有一套**调用级**事件日志 | `storage/models.py:215`、`runs/orchestration.py:721` 写入行、`execution/retention.py` 回收 |

### 2.5 没有任何东西守着模块边界

后端 23 个测试文件中没有一条断言分层或导入方向；`.github/workflows/checks.yml:28-30` 只跑 `ruff check`（`E,F,I,UP,B`，不含 `TID`）、strict mypy、非集成 pytest。今天 `runs` 导入 `execution`，或 `evidence`/`timeline` 永远不存在，都不会有任何东西失败。

### 2.6 真正的导入循环只有一对

**初稿在此处有一个实质错误，已修正。** 初稿称存在 `runs` ↔ `harness` 循环，并把它列为一条候选。查证后：

- `harness/graph.py:12` → `runs.orchestration` 是**单向**的；orchestration 只导入 `harness.model`（`:30`），从不导入 `harness.graph`；`harness/model.py` 全文没有任何项目内导入。`graph.py` 与 `model.py` 之间也没有导入。
- **唯一真正的循环是 `runs/service.py` ↔ `runs/orchestration.py`**：orchestration→service 在模块级（`:32`），service→orchestration 在**函数体内**（`service.py:182`，无注释）。
- 该延迟导入存在的原因很窄：`service.py:184` 调用 `OrchestrationService._event`——一个**跨模块调用的私有方法**——只为在 `start()` 已有事务里追加一条 `run_queued` 事件。而 `append_event` 是公开的，`runs/retention.py:64` 已在另一条路径上直接调用它。

对照 `execution/server.py:47` 的延迟导入：那里 `:42-46` 的 docstring 把理由写清楚了（默认部署不导入 Docker SDK，ADR-0010）。一处是设计，一处是权宜。

## 3. 取舍

| 候选 | 方案 | 收益 | 代价 |
| --- | --- | --- | --- |
| 挂起契约 | 抽出 `suspend` 与调用点复制 | 局部性：一套级联；测试只跨一个接缝 | 多一层调用；`_converge` 的立即调用需要显式顺序约束 |
| 挂起契约 | 连 `run.version += 1` 一并封装 | 理论上「不会忘记自增」 | 13 个调用点里 6 个用不上该接口；读 `control()` 需跳一层才知道版本是否变化 |
| 挂起契约 | 只加一条机械检查 | 保证由检查给出，不靠抽象 | 需要新增一条测试 |
| 停止确认 | 把判断搬到管理组件的接口后面 | 一条规则一个归属；私有账本不再是接口 | 需要新增返回记录与一个方法 |
| 停止确认 | 只答一个布尔 | 接口最小 | 第二个消费方（回收预览）要再读一次账本 |
| 时间线 | 现在就新开顶层 `timeline/` 包 | 与 `PROJECT §7.1` 字面一致 | 改变六个 Module 集合，按仓库规矩要开 ADR；且读侧未归口，会得到半截模块 |
| 原因码 | 建具名模块聚合词汇表 | 发出点取得类型检查；漂移可判定 | 150 条字面量改用具名常量，逐行 diff |
| 原因码 | 维持文本扫描 | 零改动 | 「后端能说什么」继续由散文而非接口回答 |

## 4. 本轮结论

1. **抽出的是「挂起」，不是「版本栅栏」。** `run.version` 属于每一次状态跃迁，不属于挂起独有；一个负责自增的 helper 接口过弱（13 个调用点里 6 个不是挂起），而它换来的保证应由检查给出。`run.version += 1` 保持就地可见。
2. **`suspend()` 的接口带一条调用顺序约束**：`_interrupt` 先比 `run.reason_code` 再插 `InterruptionRecord`，而 `_converge()` 会在 `:1141`、`:1186` 被立即调用，因此 `suspend` 不能在设置 `run.reason_code` 后再写记录，否则会多出一条重复的 `interruption`。顺序约束是接口的一部分。
3. **停止确认的接口答一份事实，不答一个布尔**：够用的最小事实是「是否已确认停止、何时确认、移除是否也已完成」。布尔会逼第二个消费方再读一次私有账本。
4. **不引入新的顶层包。** 候选 1 的新模块留在 `runs/` 内。理由不是回避 ADR，而是 `timeline` 的真正缺口在**读侧**（`history()` 与 SSE 端点不在 `events.py` 里）；只把写侧搬出去、留下读侧散着，等于为对齐文档制造一个半截模块。`PROJECT §7.1` 的六个 Module 集合保持不变。
5. **本轮不开 ADR。** 依据：24 份现有 ADR 中 0 份是关于代码组织的；(b) 类内容只以「哪个模块拥有哪个状态」出现，从不是布局。直接先例是 `sandbox.py` 拆出 `sandboxprofile.py`——记在 [验证记录 0011](../validation/0011-p1-sandbox-lifecycle.md) 里，没有 ADR。仓库的触发条件是「核心架构或产品边界变化」（`AGENTS.md:45`）与「变更核心选型」（`docs/adr/README.md:32`）；内部分层不属于二者。**唯一会跨进门槛的是改变六个 Module 的集合**，本轮刻意不改变它。
6. **`reason_code` 不进入 `GLOSSARY.md`。** 词汇表里每条都是操作员或研究者会说的词；原因码是 Interface 约定（`PROJECT.md:394`），`domain-modeling` 要求词汇表不承载实现细节。它取得的是**代码归属模块**，不是术语。
7. **证据读取缺少的范围校验另开切片，不并入本轮。** 本轮各条都是「结构变、行为不变」，评审方式简单；掺入一个授权判断会让同一次改动既含结构调整又含安全行为变化，两条轴的证据要求不同。

## 5. 明确排除

- **`execution/dockerruntime.py` 的网关策略格式归属**：`ContainerRuntime.apply_gateway_policy` / `read_gateway_policy` 以字符串传递规则，而程序与输出格式属于 profile。当前分工是**有意的**——管理组件要核验自己下发的策略（`sandbox.py:1268` `_verify_egress_drift`）。不列为候选，只在适配器变更频率超过 profile 时重开。
- **`lab/isolation/` 不合并。** **初稿在此处也有一个实质错误，已修正。** 初稿称「探针必须能独立于它所检查的代码失败」。该要求在全仓库 `*.md` 中**零命中**，不存在。事实相反：探针导入并驱动产品自己的模块（`lab/isolation/lifecycle.py:3-8` 驱动 `huntweave.execution.sandbox`；egress/action/retention 通过 `create_runner` 构建产品 Runner；`Dockerfile:14` 拷贝 `execution/network_policy.py`）。唯一 lab 自有的实现是 `lab/isolation/manager.py`，用于产品管理组件出现之前的 P0 宿主 profile 证明（`deploy/verify_isolation.py:54`）。仓库真正要求的独立性是**证据上的**：被测组件不能是唯一的证人，事实要用 Docker SDK 与证据目录读回核对（`action.py:7`、`egress.py:4`、`lifecycle.py:8`、`retention.py:7`）。不合并的理由因此换成：它是唯一持有 Docker socket 的探针镜像，产物是宿主 profile 的验收证据，不是产品代码。

## 6. 未决与限制

- **本轮未涉及候选 3、4、6**（选择性保留路由的四处转抄、`RunnerClient` 包装层、测试运行时搬出测试文件）。它们与 1、2 不在同一条接缝上，另行评估。
- **「没有任何东西守着模块边界」记入待办，不在本轮处理。** 若本轮之后要加一条导入方向检查，那属于开发环境改动而非产品代码，按 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md) 的验证顺序在切片落地后评估。
- **证据读取缺范围校验**（`orchestration.evidence()` 有路径包含校验 `:1515` 与逐字节 sha256+size 复核 `:1529`，但不检查该证据属于调用者有权读的 Run）只作记录，按第 4 节第 7 条另开切片。
- **本轮所有结论均未经实现验证。** 落地后按 `docs/validation/` 体例新增一份验证记录，实际计数与命令输出写入该记录，不在本文件复述。

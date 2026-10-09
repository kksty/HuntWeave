# 当前状态

本文件是**阶段、能力与下一实施项的唯一状态来源**。`README.md` 顶部与 `AGENTS.md` 只指向这里，不再各自复述状态。产品行为与安全边界以 `PROJECT.md` 为准，正式规格见 `docs/specs/`，验证证据见 `docs/validation/`。

更新时机：一个阶段交付验收、能力开关变化、或下一实施项改变时。本文件与代码或验证记录冲突时，以代码和验证记录为准，并修正本文件。

## 阶段

| 阶段 | 范围 | 状态 |
| --- | --- | --- |
| P0 | 运行骨架、认证与授权快照、持久假 Run、隔离靶场验证 | 已有验收记录；P0-A 至 P0-D 的实施 Issue 已关闭，审查更正记录 [#12](https://github.com/kksty/HuntWeave/issues/12) 仍 open 待人工确认；后续缺口见 `docs/validation/0005-p0-execution-and-recovery.md`，不视作全部目标行为已经达成 |
| P1 | Kali 真实执行、选择性保留、透明控制台 | 进行中：正式规格 [0002](./specs/0002-real-execution.md) 已有，#9–#21 待办仍开放；真实执行未开放 |
| P2 | 完整 Agent MVP：真实模型、联网研究、Run 内规划、24×7 持续推进 | 实现未开始；[0003](./specs/0003-agent-research.md) 与 [0004](./specs/0004-finding-admission.md) 是**未经评审的草案**（评分与准入尚无阶段表归属），取舍见 ADR-0009/0011/0013 |

## 当前能力

已交付：服务端会话与授权快照、持久 Run 与排队状态机、LangGraph 角色闭环（确定性模型 Adapter）、持久假 Runner 账本与租约、原始证据归档与哈希校验、有序事件与 SSE 时间线、暂停/取消/恢复预览/重启对账。

已知限制：确定性 Adapter 按预置场景而非工具返回分支，当前每角色固定一项任务，尚无 Worker × N 的研究闭环；完整 Finding/Review/报告和持久依赖尚未实现。#12 的记录更正已落文档，但 Issue 仍待人工确认。以上状态于 2026-10-09 对照源码与 GitHub Issues 核实。

**真实执行未开放。** `fake_execution_ready=true` 只表示固定假动作链路就绪；`real_execution_ready=false`，原因 `environment_unsupported`。界面与响应标注“开发演示 / 假执行”。

每会话网关隔离 profile（IPv4/TCP）已在本地靶场验证，尚未接入产品 Runner；默认 Compose 的 Runner 不挂 Docker socket。

## 下一实施项（P1）

规格：`docs/specs/0002-real-execution.md`。边界与门槛见 [ADR-0010](./adr/0010-real-execution-boundary-and-gate.md)。

前置修复（阻塞首批 tracer）：[#9](https://github.com/kksty/HuntWeave/issues/9) 执行票据写死目标绑定、[#10](https://github.com/kksty/HuntWeave/issues/10) 能力就绪状态只在 API 层、[#11](https://github.com/kksty/HuntWeave/issues/11) 控制台在 `version_conflict` 后静默重发、[#13](https://github.com/kksty/HuntWeave/issues/13) 单 Run 目标上限文档 100 与实现 5000 不一致。

tracer 顺序（实现切片）：

1. [#15](https://github.com/kksty/HuntWeave/issues/15) `unknown` 调用的操作员核对入口：当前此类 Run 停在 `waiting`，`resume`/`close` 被拒绝、`cancel` 无法收敛。用现有假执行账本验收，不引入真实容器。
2. [#16](https://github.com/kksty/HuntWeave/issues/16) 受信管理组件与真实容器生命周期。
3. [#18](https://github.com/kksty/HuntWeave/issues/18) 出口控制与取消/回收：必须先于 #17 的实际靶场目标动作验收。
4. [#17](https://github.com/kksty/HuntWeave/issues/17) 真实动作最小闭环：真实工具容器走既有票据、租约、取消与证据契约，以工具结果驱动确定性后续分支。
5. [#19](https://github.com/kksty/HuntWeave/issues/19) 透明控制台：只读六项 + 待核对调用区块 + 会话容器与网关状态及回收确认。
6. [#20](https://github.com/kksty/HuntWeave/issues/20) 选择性保留：统计候选 + 手动固定/删除 + 回收预览，不自动发布。
7. [#21](https://github.com/kksty/HuntWeave/issues/21) 多活跃 Run 并发压力验收：作为最后一个 tracer 的验收项。

2026-10-09 架构修订已写入 P1 规格：核对不等于成功/回收，真实 Run 不切换为假执行，回退先收尾，运行中持续校验能力，手动保留不共享目标可写层；基础公平/取消检查随 #17/#18 先验。这些修订已于 2026-10-09 同步到 GitHub Issue 正文与原生依赖（见规格第 5 节末段），**但均未实现**；实施顺序以规格第 5 节表格与原生阻塞边为准，不据旧 Issue 正文的依赖清单跳步。

## P2 的开工约束

按 [P2 规格](./specs/0003-agent-research.md) 的 A–E 切片推进：事务外模型与独立任务身份 → 持久事件规划/依赖 → 真模型与洁净准备 → 证据绑定/独立复审/版本化报告 → 持续运行与恢复验收。不新增常驻 Planner、图数据库或跨 Run 可变黑板；P1 不因这些设计推迟。P2 实施 Issues 尚未建立，本轮仅完成架构文档。

后续用户要求已纳入设计：默认 RCE 最小只读实证、成功退出、禁止破坏/删改目标数据、必要新增及大量脏数据披露（ADR-0012）；CVSS v4.0 评分与正式漏洞库准入（[0004 规格](./specs/0004-finding-admission.md)、ADR-0013）。低危/无害和仅版本命中不入库，未决项与测试遗留继续展示。以上均未实现，须随 P2-C/D 关联实施任务；不扩大当前执行能力。

## 未开放能力

搜索与联网研究、真实模型、动态安装、选择性工具库、完整 Finding/Reviewer 报告、Run 内规划、24×7 持续推进。不要按已实现对待。

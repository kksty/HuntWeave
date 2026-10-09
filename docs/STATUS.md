# 当前状态

本文件是**阶段、能力与下一实施项的唯一状态来源**。`README.md` 顶部与 `AGENTS.md` 只指向这里，不再各自复述状态。产品行为与安全边界以 `PROJECT.md` 为准，正式规格见 `docs/specs/`，验证证据见 `docs/validation/`。

更新时机：一个阶段交付验收、能力开关变化、或下一实施项改变时。本文件与代码或验证记录冲突时，以代码和验证记录为准，并修正本文件。

## 阶段

| 阶段 | 范围 | 状态 |
| --- | --- | --- |
| P0 | 运行骨架、认证与授权快照、持久假 Run、隔离靶场验证 | 已验收（issue #1–#6 已关闭），记录见 `docs/validation/0001`–`0005` |
| P1 | Kali 真实执行、选择性保留、透明控制台 | 进行中：规格待写，`docs/specs/` 目前只有 P0 的 `0001-foundation.md` |
| P2 | 完整 Agent MVP：真实模型、联网研究、Run 内规划、24×7 持续推进 | 未开始，设计见 `docs/adr/0009-adaptive-research-and-continuous-execution.md` 与 `docs/research/2026-10-09-architecture-improvement.md` |

## 当前能力

已交付：服务端会话与授权快照、持久 Run 与排队状态机、LangGraph 角色闭环（确定性模型 Adapter）、持久假 Runner 账本与租约、原始证据归档与哈希校验、有序事件与 SSE 时间线、暂停/取消/恢复预览/重启对账。

**真实执行未开放。** `fake_execution_ready=true` 只表示固定假动作链路就绪；`real_execution_ready=false`，原因 `environment_unsupported`。界面与响应标注“开发演示 / 假执行”。

每会话网关隔离 profile（IPv4/TCP）已在本地靶场验证，尚未接入产品 Runner；默认 Compose 的 Runner 不挂 Docker socket。

## 下一实施项（P1）

规格：`docs/specs/0002-real-execution.md`。边界与门槛见 [ADR-0010](./adr/0010-real-execution-boundary-and-gate.md)。

前置修复（阻塞首批 tracer）：[#9](https://github.com/kksty/HuntWeave/issues/9) 执行票据写死目标绑定、[#10](https://github.com/kksty/HuntWeave/issues/10) 能力就绪状态只在 API 层、[#11](https://github.com/kksty/HuntWeave/issues/11) 控制台在 `version_conflict` 后静默重发、[#13](https://github.com/kksty/HuntWeave/issues/13) 单 Run 目标上限文档 100 与实现 5000 不一致。

tracer 顺序（实现切片）：

1. [#15](https://github.com/kksty/HuntWeave/issues/15) `unknown` 调用的操作员核对入口：当前此类 Run 停在 `waiting`，`resume`/`close` 被拒绝、`cancel` 无法收敛。用现有假执行账本验收，不引入真实容器。
2. [#16](https://github.com/kksty/HuntWeave/issues/16) 受信管理组件与真实容器生命周期。
3. [#17](https://github.com/kksty/HuntWeave/issues/17) 真实动作最小闭环：真实工具容器走既有票据、租约、取消与证据契约。
4. [#18](https://github.com/kksty/HuntWeave/issues/18) 出口控制与取消/回收。
5. [#19](https://github.com/kksty/HuntWeave/issues/19) 透明控制台：只读六项 + 待核对调用区块 + 会话容器与网关状态及回收确认。
6. [#20](https://github.com/kksty/HuntWeave/issues/20) 选择性保留：统计候选 + 手动固定/删除 + 回收预览，不自动发布。
7. [#21](https://github.com/kksty/HuntWeave/issues/21) 多活跃 Run 并发压力验收：作为最后一个 tracer 的验收项。

## 未开放能力

搜索与联网研究、真实模型、动态安装、选择性工具库、完整 Finding/Reviewer 报告、Run 内规划、24×7 持续推进。不要按已实现对待。

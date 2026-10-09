# ADR-0010：P1 真实执行的验收宿主、能力边界与就绪门槛

日期：2026-10-09。状态：已采纳，待 P1 实现。

P1 第一次让平台的 `ToolCall` 真实出网，因此把三件事一起固定下来。**验收宿主**只取本机 Windows 11 + Docker Desktop/WSL2，沿用 ADR-0006/0007 已验证的每会话网关 profile；原生 Linux 仍只要求共享部署入口可理论部署，其原生验收另立切片，避免 P1 的失败定位在「执行链路错」和「宿主 profile 未适配」之间摇摆。**能力边界**沿用 profile v1 的现状：只有 IPv4/TCP，没有 DNS、UDP、IPv6 与 loopback TCP；首个真实样例取 HTTP 与 Redis，界面、报告与覆盖记录必须如实标注这一边界，「profile 不支持」不得记成「目标无此服务」。**就绪门槛**要求四项同时满足才允许 `real_execution_ready=true`：profile 复验覆盖票据、租约、取消、资源回收与证据归档；能力契约从当前的 `Literal[False]` 改为可表达的状态且 `reason_code` 可空；前端真正消费 `capabilities` 并在页面标注；默认部署保持假执行且可一键回退。在此之前该值保持 false。

P1 的全部验收对象是 `lab/` 自建靶场里的固定回显容器，不接触任何外部目标。宿主管理/查看入口仅绑定回环，工具连接的是内部网络明确授权的 IPv4/TCP 地址，不能据此声称 profile 支持 loopback TCP。真实 IP 的开放留到 P1 验收通过之后、由操作员显式配置。真实执行仍只由 Runner 容器内的受信管理组件接触 Docker，且该组件的 Docker 访问被收窄为一组固定操作，不提供通用 API 代理，常驻服务仍是三个。

按 [ADR-0011](./0011-planning-authority-and-evidence-revisions.md) 补充：四项是开放门槛，运行中仍须检查隔离/租约/执行端与存储健康；具体未就绪原因如实区分。回退须先停止新增执行、回收并对账，再撤除管理能力，真实 Run 不改用假动作。“一键”描述操作入口，不表示可以绕过异步收尾与 unknown 核对。

## 后果

- P1 内可跑通的协议面是已知的窄集合。出现 DNS、UDP、IPv6 或 loopback TCP 需求时必须新增 profile 与对应验证，不能就地放宽现有规则。
- `environment_unsupported` 在 P1 期间仍是默认结论；任何「已支持真实目标测试」的表述都必须先满足上述四项门槛。
- 在 Windows 之外部署并希望真实执行的人需要先完成该宿主的 profile 验收；P1 不为这条路径提供承诺，也不把它作为 P1 的验收条件。

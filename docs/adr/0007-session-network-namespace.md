# ADR-0007：每会话网关网络命名空间

日期：2026-10-09。状态：本机技术验证通过，待 P1 产品接入。

Windows Docker Desktop 的本地探针中，独立 internal bridge 的跨子网转发方案没有让授权流量到达网关 FORWARD 链。采用 Docker 的 [container 网络模式](https://docs.docker.com/engine/network/#container-networks)：可信网关持有每会话网络命名空间，普通用户工具共享该网络栈，保留独立 PID、文件系统和进程权限。网关没有平台密钥或 Docker socket，只有可信 Runner 管理端接触 Docker API。

网关在工具开始执行前安装默认拒绝的 INPUT/OUTPUT/FORWARD 规则，只放行精确 IPv4/TCP 端点及对应已建立返回流；不设全局 ESTABLISHED 放行。保护地址优先，Docker 内置 DNS 与 IPv6 明确阻断。取消先撤销规则，再停止工具 PID namespace 并移除出口端点。该方案可在受测版本下控制具有默认 NAT 出口的工具流量，规则局限于本会话，验证结果见 [隔离记录](../validation/0002-windows-isolation.md)。

当前 v1 的 DNS、UDP、IPv6 与 loopback TCP 限制是明确的 profile 能力边界；后续研究需求通过单独 profile 与验证扩展。此决定不改变普通用户执行、有限特权和准备/目标环境分离原则，也不直接启用默认演示 Runner 的真实执行能力。

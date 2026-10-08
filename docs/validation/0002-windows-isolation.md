# Windows Docker Desktop 隔离技术验证

记录日期：2026-10-09。对应 Issue #2。执行时间：2026-10-08 23:58:10–23:58:42（Asia/Shanghai，完整 UTC 时间保存在原始报告）。本记录证明本机指定环境下的本地靶场结果，不开放产品真实执行入口。

## 环境、权限与入口

Windows 11 x86_64，Docker Desktop 4.94.0、Engine 29.8.2、WSL 3.0.1.0 / NAT、内核 `6.18.40.1-microsoft-standard-WSL2`、cgroup v2，防火墙为 iptables 1.8.9 / nf_tables。探针镜像为固定 digest 的 Python 3.12 / Debian bookworm，固定安装 iptables 1.8.9-2 和 iproute2 6.1.0-3；Kali WSL 未参与测试。

当前入口为 `python deploy/verify_isolation.py`（先安装 deploy/verification-requirements.txt）；原始结果使用替换前的 Windows 包装入口，网络逻辑不变。仅本次可信管理容器挂 Docker socket，用于创建和回收带本轮唯一标签的靶场资源；网关只获 NET_ADMIN。工具容器为 UID 10001、cap_drop=ALL、no-new-privileges、只读根文件系统，使用独立 PID 与文件系统，共享本会话网关的网络命名空间。目标是两个自行创建的固定 TCP 回显容器，另有控制网络模拟容器；只有本地授权端点可通过白名单。

目标 bridge 带 NAT 默认出口，session/control bridge 为 internal + isolated。网关在工具启动前安装默认拒绝规则；IPv6 明确禁用。所有规则在本轮容器网络命名空间中应用，未执行 Windows 防火墙命令或修改 Docker VM 的全局策略。原始报告包含实际命令、参数、输出、事件时间、镜像 ID、策略 hash 与回收结果，位于被 Git 忽略的 `runtime/isolation/<run>/report.json`。

## 结果

39 项实际探针全部通过，cleanup_errors 为空，执行后无本轮残留容器或网络。

| 验证 | 观察 |
| --- | --- |
| 默认拒绝 | 两个已确认监听的目标及控制模拟端点均不可连接 |
| 精确放行 | 只允许目标 A 的 TCP 7000；目标 B 和已确认监听的 TCP 7001 被阻断 |
| 协议限制 | UDP 与 Docker 内置 DNS 请求被 OUTPUT 规则丢弃，丢包计数增加；发送可能返回 EPERM，此错误按实际阻断记录 |
| 带默认出口的保护 | app:8000、runner:8001、postgres:5432 的实际网络地址，Windows IPv4 接口、Docker host gateway、元数据地址及文档保留地址的连接均被拒绝，逐项确认过滤器丢包计数增加 |
| 范围优先级 | 显式添加元数据目标也被 ScopeDenied 拒绝；合法私有目标可以放行 |
| 工具权限 | 普通用户尝试修改防火墙、路由或创建 raw socket 均失败 |
| IPv6 | disable_ipv6=1，无 IPv6 接口地址，IPv6 连接不可用 |
| 连接撤销 | 从撤销请求到持续 TCP 客户端检测中断为 1.379 秒；撤销后新连接被阻断 |
| 父进程退出与取消 | 双重 fork 子进程实际 reparent 到容器 PID 1、UID 10001；取消后工具容器与同 PID namespace 的观察容器停止，网关目标网络端点移除 |

原始报告 SHA-256：`3d9272435618488f546ac743639061f202056b3b8ef699ba06764e23528f61af`。

策略文件 SHA-256：`b71b0cd61ca8f0617c163f0e04c3b175a0d2b16fadc6287a90cf3f08968c5f53`。

管理脚本 SHA-256：`81b5a31ed7a1f1c5372d459fc06b07a86388006dca2bd55dd194db7c65d540aa`。测试时源码树存在未提交实现，报告已记录 source_tree_dirty=true；使用实际脚本与镜像 hash 标识测试制品。

## 已处理的失败与边界

早期使用跨 bridge 路由转发的探针未到达网关 FORWARD 链，授权端点不可达，该轮报告为失败且资源已回收。最终采用每会话独立网络命名空间，在共享命名空间的 OUTPUT/INPUT 直接限制工具流量；未改变宿主全局转发设置。另一次 UDP 探针把内核 EPERM 当作脚本失败，随后修正探针并结合真实丢包计数验证。

本 profile 仅验证 IPv4/TCP 与当前指纹，不支持 DNS、UDP、IPv6 或 loopback TCP。Docker/WSL/内核、策略或工具镜像改变后需要重跑入口。P1 仍需将该网络生命周期、执行票据、租约、资源/取消和 Kali 工具环境接入真实 Runner 并复验。默认 Compose Runner 没有 Docker socket，尚未使用本技术验证 profile，因此继续报告 `environment_unsupported`。

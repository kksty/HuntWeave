# ADR-0006：Windows 工作区与 Docker Desktop 首个部署入口

日期：2026-10-08。状态：已接受。

用户决定直接在当前 Windows 工作区开发，并先交付 Windows 上可部署的版本。因此首个部署入口使用 Windows 11 + Docker Desktop WSL2 后端，替代总纲早期的原生 Linux 优先路线；Linux 宿主支持以后通过独立 profile 和验证加入。Docker Desktop 自己的 `docker-desktop` 环境承载 Linux 容器运行时，单独安装的 Kali WSL 与项目无依赖关系；Kali 衍生工具镜像仍按 ADR-0005 构建。

Docker Desktop 的容器出口与 Windows 宿主网络之间存在受管理 VM/后端边界，因此不能直接复用未经验证的 Linux 主机防火墙规则。P0 用专用本地靶场验证出口许可、连接撤销和进程回收，全部通过前真实执行返回 `environment_unsupported`。版本变化后按 profile 重跑验证，不修改 Windows 全局防火墙或无关 Docker 网络。

# ADR-0008：共用 Compose 与 Python 部署入口

日期：2026-10-09。状态：已接受。

用户要求保留 Docker 的跨平台部署能力，当前 Linux 达到理论可部署即可。因此 Windows + Docker Desktop 和 Linux + Docker Engine 共用同一份 Compose、镜像、数据库迁移及 Python 初始化/验证入口；移除 PowerShell 脚本。Windows WSL 与 Linux 本地 socket 只在环境采集和 Docker socket 路径处区分，业务逻辑与隔离规则共用。

本机 Windows 保留真实隔离记录；Linux profile 明确为原生宿主验证待完成，使用同一探针入口但不继承 Windows 的通过状态。当前默认 Runner 继续演示模式。部署端只需 Python 3.10+ 运行初始化，额外 psutil 依赖仅用于可选隔离验证；Linux secret 父目录为 0700，文件为 0444，以兼容不同容器 UID 的文件绑定读取，同时限制宿主目录访问。

# ADR-0001：采用 HuntWeave 作为项目开发名称

日期：2026-10-08（Asia/Shanghai）

状态：当前开发基线；可由后续明确的产品命名决定修订。

2026-10-08 后续用户命名决定：项目品牌与仓库文档统一使用 HuntWeave，代码标识保持 `huntweave`。

## 原因

原开发名称 ScopeHunter 与现有安全工具同名。该工具用于寻找漏洞赏金项目范围内的目标，与本项目同属安全领域，容易混淆，因此弃用该名称。[现有 ScopeHunter 项目](https://github.com/blackhatethicalhacking/ScopeHunter)

本项目需要一个能表达主动研究和多 Agent 协作、便于作为仓库与 Python 包标识使用的名称，同时保持“只面向自有或明确授权目标”的定位。

## 决定

- 项目名称：**HuntWeave**。
- 仓库建议名、Python 包名、配置前缀基名：`huntweave`；环境变量前缀使用 `HUNTWEAVE_`。
- 英文说明：`Agentic Security Validation Platform`。
- 中文说明：面向已授权目标的多 Agent 安全测试平台。

Hunt 对应发现线索、提出假设与验证问题；Weave 对应 Collector、Worker、Reviewer 和人工复审之间的协作，以及证据的关联。

## 公开名称初查

以下为本次实际查询结果，不是对所有公开/私有项目或商标的穷尽检索。

| 检查项 | 方法 | 2026-10-08 结果 |
| --- | --- | --- |
| GitHub 仓库名 | API 查询 `HuntWeave in:name fork:true` | `total_count=0`，`incomplete_results=false` |
| GitHub 分隔符变体 | 分别查询 `hunt-weave in:name fork:true`、`hunt_weave in:name fork:true` | 两次各返回 5 个分词相关结果；将仓库名转小写并去除 `-`/`_` 后，没有等于 `huntweave` 的名称 |
| 公开网页 | 搜索 `"HuntWeave"` 与常见分词/连字符写法 | 本次结果未发现以完整名称 HuntWeave 命名的明确软件项目；存在无关的分词结果 |
| PyPI | 查询 `https://pypi.org/pypi/huntweave/json` | HTTP 404，未返回已发布包信息 |
| npm | 查询 `https://registry.npmjs.org/huntweave` | HTTP 404，未返回已发布包信息 |

分隔符变体查询返回的仓库为：`girlnewsie/weaver-hunter`、`JD8765/Hunter-Weave-Assistant`、`sathirak/weavers-little-hunter`、`ForgeGit/Hunter_melee_weave_TBC`、`AIHackathon-weave/summer-treasure-hunt`。记录这些结果是为了区分“查询有结果”和“确实重名”，未将这些仓库作为本项目架构来源。

可复查入口：[GitHub 名称查询 API](https://api.github.com/search/repositories?q=HuntWeave%20in%3Aname%20fork%3Atrue&per_page=10)、[PyPI 包元数据](https://pypi.org/pypi/huntweave/json)、[npm 包元数据](https://registry.npmjs.org/huntweave)。这些是实时端点，未来结果可能变化。

本次还排除了已有明确同名仓库的 [ScopeWeaver](https://github.com/urai-group/ScopeWeaver) 和 [ProbeWeave](https://github.com/saneax/ProbeWeave)；没有因它们在某次网页搜索中不显眼就认定可用。

本次未核验商标、域名、GitHub 组织名或包注册资格，也未预订任何名称。零检索结果及 404 不等于全球唯一或可以注册；发布时再次核对相应命名空间。

## 影响与验证

更新根目录 `PROJECT.md`、`README.md`、`AGENTS.md` 的当前名称及规划中的源码包目录。旧名称仅在本决策的历史原因中保留。

名称统一不改变产品范围、四角色工作流、技术栈或授权边界。英文名称与代码标识保持一致，本次调整无需源码或数据库兼容迁移。

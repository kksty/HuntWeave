# P1-① 单 Run 目标上限收紧到文档值：一致性验证

日期：2026-10-09（Asia/Shanghai）。对应 Issue [#13](https://github.com/kksty/HuntWeave/issues/13)，依据 P1 规格第 4 节①档与第 5 节，以及 `PROJECT.md` §12「保守开发默认值」。本轮只把实现收紧到文档已有值，并补边界检查；真实执行仍未开放，验收只用纯函数与既有非集成检查。实现提交 `c1a4ec2`；检查的命名与断言按两轴代码审查意见调整，见 `7321082`。

## 问题与判定

`PROJECT.md` §12 的保守开发默认值表写着「单 Run 导入上限 100 个 IP」，`backend/src/huntweave/runs/inputs.py` 却在目标数超过 **5000** 时才拒绝，且该数字是内联字面量，没有常量名可与文档对照。`README.md` 的输入说明当时已写 100，但附带「实现仍在收紧到该值」的说明，即三处（文档、README、实现）处于两份口径。

裁决沿用 Issue 正文的「已定裁决」：**以文档为准，上限 100 个 IP**；需要更大规模时另行提出需求并同时改文档与代码。本轮不改变计数口径——上限计的是**去重后的不同目标**，重复行不占额度，这与 `PROJECT.md` §12「100 个 IP」的「个」一致。

## 实现

| 改动点 | 行为 |
| --- | --- |
| 上限常量 | 新增模块级 `TARGET_LIMIT = 100`，取代内联字面量 `5000`；注释写明出处为 `PROJECT.md` §12 与计数口径 |
| 拒绝语义 | 目标数超过 `TARGET_LIMIT` 时仍抛 `ServiceError("target_limit_exceeded", 422)`，原因码与状态码不变，不新增原因码 |
| 拒绝时机 | 未改动。限值在 `preview_targets` 内生效，`runs/service.py:create_scope` 在写入 `AuthorizationScope` 之前调用它，而 Run 必须引用已存在的授权快照，因此拒绝结构性地发生在 Run 创建之前 |
| 文档一致性 | `README.md` 输入说明删除「实现仍在收紧到该值，见 #13」的过期从句，保留 `PROJECT.md` §12 出处并补明计数口径；`PROJECT.md` §12 本已是 100，无需改动（故不触发「修改默认值须有版本记录」） |

## 验证

环境：Windows 主机，Python 3.12.14，`backend/.venv`（pytest 9.1.1）。按 `README.md` 的本地纯检查约定在 `backend/` 目录内执行。

| 行为 | 结果 |
| --- | --- |
| 101 个不同目标被拒绝 | 新增单元检查：先断言 100 个目标可预览，再断言追加第 101 个时抛 `ServiceError`，且 `reason_code == "target_limit_exceeded"`、`status_code == 422` |
| 100 个目标通过（边界下沿） | 同一条检查的前半段：`192.0.2.1`–`192.0.2.100` 预览 `valid` |
| 重复行不占额度 | 新增单元检查：100 个不同目标各重复一次（200 行输入）预览 `valid`，固定去重计数口径 |
| 红→绿顺序 | 先写检查并运行，确认失败于 `DID NOT RAISE ServiceError`（当时限值为 5000）；改为 `TARGET_LIMIT = 100` 后通过 |
| 目标输入定点检查 | `python -m pytest tests/test_run_inputs.py -q` → 17 项通过（本轮新增 2 项） |
| 既有路径不回退 | `python -m pytest -m "not integration" -q` → 68 项通过、4 项跳过、37 项按标记排除 |
| 静态检查 | `ruff check src tests` 全部通过；`mypy --config-file pyproject.toml src` 在 35 个源文件上无问题 |
| 两轴代码审查 | 对 `5474730...c1a4ec2` 跑 Standards 与 Spec 两轴：确认上限来源唯一、去重计数口径成立、拒绝先于 Run 创建；按意见把检查命名改为领域术语（`target`/`targets`，对齐 `GLOSSARY.md` 与 `TargetRow`/`TargetPreview`），并让去重检查直接断言计数（200 行输入 → 100 个目标）。未采纳「让检查引用 `TARGET_LIMIT`」：那会使边界断言自证，100/101 作为来自规格的独立字面量保留 |

## 未达成与限制

- **未做可配置化**：Issue 验收标准第 3 条是条件句（「若上限改为可配置」）。本轮选择不引入配置项，因此不涉及默认值与配置版本记录；上限目前只由常量表达，调整需改代码并同步文档。
- **未加集成层检查**：`POST /api/v1/scopes` 的 422 与「未建 scope/Run 行」未单独用一次性栈断言，依据是 `create_scope` 的调用顺序已结构性保证，且本仓库集成检查需 Docker 与一次性栈、按 `README.md` 手工执行。此项取舍见「待人工确认」。
- **预览接口一并拒绝**：`POST /api/v1/targets/preview` 与 `create_scope` 共用 `preview_targets`，因此粘贴超过 100 行时**预览本身**返回 422，操作员拿不到逐行合法/重复/错误清单来定位。Issue 的验收标准要求「超过 100 时拒绝」，故本轮按拒绝实现，未改为「预览照常、提交才拒」。
- **既有授权快照不受新上限约束**：上限只在 `preview_targets` 内校验，`runs/service.py:create_run` 不重新校验快照的目标数。因此本提交之前按旧上限（5000）建立的 `AuthorizationScope` 仍可创建目标数超过 100 的 Run，新上限只约束此后新建的快照。本轮未在 Run 创建时补校验，因为 Issue 的验收标准要求的是拒绝先于 Run 创建，未要求对既有快照回溯设限。
- **IPv6 目标占用额度但不可提交**：可规范化的 IPv6 目标会计入 `targets` 并被计入上限，同时又因 `ipv6_environment_unsupported` 不能提交。因此 100 个可提交的 IPv4 加 1 个 IPv6 会得到 `target_limit_exceeded`，而不是 IPv6 原因码。本轮按「保持计数口径不变」处理，未改为只统计可提交目标；该项在收紧前即已如此，非本轮引入。
- **前端未受影响**：本轮不改接口形状与界面，未重复运行浏览器流程与故障探针；`docs/STATUS.md` 记载的 `real_execution_ready=false` 与「开发演示 / 假执行」标注不变。能力诚实性属 [#10](https://github.com/kksty/HuntWeave/issues/10)、`version_conflict` 静默重发属 [#11](https://github.com/kksty/HuntWeave/issues/11)，本轮均未触及。
- **未验证真实规模行为**：100 是保守开发默认值，尚未按部署容量校准；`PROJECT.md` §12 说明该表需经靶场与实际部署校准。

## 待人工确认

处置已记录在 [#13 的关闭评论](https://github.com/kksty/HuntWeave/issues/13#issuecomment-6084734366)（2026-10-09），原表述保留：第 1、3 项接受现状，第 2、6 项顺延 [#19](https://github.com/kksty/HuntWeave/issues/19)，第 4 项顺延 [#21](https://github.com/kksty/HuntWeave/issues/21)，第 5 项记入 `STATUS` 已知限制。

- [ ] 上限计**去重后的不同目标**（重复行不占额度）是否符合 `PROJECT.md` §12「100 个 IP」的意图，或应改为按原始输入行数封顶。
- [ ] 是否接受「预览接口同样按上限拒绝」：操作员粘贴超限清单时得到 422 原因码而非逐行清单。若需改为「预览不设限、仅提交设限」，属独立切片并会影响 #19 的输入区交互。
- [ ] 本轮只做 Seam A（纯函数）而未补 `POST /api/v1/scopes` 的集成断言，是否可接受；若要求补，需 Docker 与一次性栈。
- [ ] 上限是否需要成为可配置项（当前为常量，改动需改代码）。若需要，按 Issue 验收标准第 3 条须同时给出默认值 100 与配置版本记录。
- [ ] 既有授权快照是否需要回溯设限：当前 `create_run` 不重新校验，本提交前建立的超限快照仍可创建 Run。若要求封堵，须在 Run 创建路径补一次校验（属新行为，非 #13 裁决范围）。
- [ ] 计入额度但不可提交的 IPv6 目标是否应从上限统计中排除（当前计入，故可能先报 `target_limit_exceeded` 而非 `ipv6_environment_unsupported`）。

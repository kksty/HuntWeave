# 0021 V-A token 层与字体自托管验证

对应 Issue：[#33 V-A token 层与字体自托管（NASA-punk 视觉系统）](https://github.com/kksty/HuntWeave/issues/33)。规格：[0009 视觉设计系统](../specs/0009-visual-design-system.md) §3、§4、§11；决定：[ADR-0024](../adr/0024-nasa-punk-visual-language.md)；分层验证依据：[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)。

本记录覆盖 V-A 的实际验证证据：token 层是否唯一色值来源、对比度实测值与文档是否一致、字体获取与许可、中文字族与 Latin 等宽的分工、构建产物体积。**V-B（#34）的换肤与灰度截图回归不在本记录范围**，浏览器验收由协调人统一调度。

## 1. 环境与入口

| 项 | 值 |
| --- | --- |
| 工作树 | `D:\code\huntweave-wt\33-visual-tokens`（分支 `codex/33-visual-tokens`，自 `origin/main` 的 `7c56643`） |
| 宿主 | Windows 11 x86_64；Node v26.9.0（本机 PATH 无 Python/`uv`，字体子集化全部用 Node 完成） |
| 前端依赖 | `frontend/node_modules` 由 `npm ci` 安装；本切片新增 6 个 devDependency（见 §4） |
| 涉及文件 | `frontend/src/tokens.css`、`frontend/src/fonts.css`、`frontend/src/style.css`、`frontend/src/main.ts`、`frontend/src/assets/fonts/`、`frontend/scripts/`、`frontend/package.json`、`.github/workflows/checks.yml`、`docs/specs/0009-visual-design-system.md` §3/§4 |
| 未涉及 | 未改后端、未改组件结构与信息架构、未引入 CSS 框架、未做深色主题 |
| 未执行 | 未起 Docker 栈、未跑 Playwright、未做灰度截图（见 §8） |

可复现命令（工作目录 `frontend/`）：

```powershell
npm run build          # vue-tsc --noEmit && vite build
npm run check:design   # 16 项机械检查（不联网；5b 需要先 build）
npm run check          # build && check:design，CI 用的同一条链
npm run fonts:build    # 只在需要重建中文子集时运行（需要网络）
```

## 2. 实施评审（PROJECT §14）

对照真实代码盘点换肤前的实际状态：

| 事实 | 换肤前（`git show 7c56643:frontend/src/style.css`） |
| --- | --- |
| 设计 token 层 | **不存在**。`style.css` 只有 `--console-line: #dce3d9` 一个变量，其余色值全部写死 |
| 写死色值 | **29 个不同十六进制色值、42 处出现**；另有 3 处命名色 `white` 与 1 处 `transparent` |
| 组件内色值 | `App.vue`、`RunConsole.vue`、`workspace.ts` **均为 0 处**十六进制与命名色——两个 `.vue` **没有 `<style>` 块**，模板里也没有内联 `style=""`。这条前提是验收 1「机械检查可证」成立的前提，已写进检查输出 |
| 字体 | `:root{font-family:Inter,system-ui,sans-serif}`；等宽处写 `ui-monospace,monospace`。**没有自托管字体，也没有中文字族** |
| 字体相关依赖 | `frontend/package.json` 无任何字体包 |

切片边界：V-A 的交付面是 **token 层 + 字体层**；把既有控制台**换成** NASA-punk 的外观（直角、接缝、身份元素、几何收敛、灰度回归）属 V-B/V-C（#34）。因此本票对 `style.css` 只做两件事：色值改走 `var(--hw-*)`、字体族改走 `var(--hw-font-*)`。**圆角、间距、字号、描边宽度、焦点环与响应式断点全部保持换肤前的取值**——这一点由 §3 的机械核对证明，不靠自我声明。

## 3. 行为与结果

### 3.1 色值：29 个写死值全部接入 token 层

`frontend/src/tokens.css` 声明 **9 个字面量色值**（`--hw-base`/`panel`/`ink`/`line`/`accent`/`accent-ink`/`stripe-1..3`；`--hw-stripe-4` 与 `--hw-accent-ink` 同值 `#a8432f`，不重复计数）加 8 个槽位别名（`--hw-surface`→panel、`--hw-surface-sunken`→base、`--hw-text`/`--hw-text-muted`→ink、`--hw-border`/`--hw-border-soft`→line、`--hw-focus-ring`→accent、`--hw-text-on-solid`→panel），以及 `--hw-hazard` 斜纹。`style.css` 里 29 个不同写死值逐个有去处（映射表见 §6）；**没有一处取值保持不变**——原色板是绿调浅色，换色板必然改值，这不是遗漏。

### 3.2 机械核对：`style.css` 只是替换，没有改几何

这是本记录最重要的一条证据。做法：取 `git show 7c56643:frontend/src/style.css` 作为换肤前基线，把两份文件里所有色值（`#rrggbb` 与 `white`）、字体族与 `var(--hw-*)` 引用统一替换成占位符、去掉注释与空白后**逐字符比较**：

```
PURE SUBSTITUTION: True  (old=5165 new=5165)
```

归一化后两份样式表**完全相同**。由此可以断言 `style.css` 没有引入任何几何、层级或规则集合上的改动——没有新增裸 `dl` 规则、没有把 `th` 套上「只给 Latin 标签」的字距（该 token 自己在 `tokens.css` 写着「中文标签不套用」）、圆角与间距仍是换肤前的 `14px`/`28px`/`36px` 等原始取值。

首稿曾在这些位置动了手脚（圆角 `14px→0`、字号 `25px→24px`、多处 padding/margin 变化、新增裸 `dl` 规则）又在注释里声称「几何与换肤前一致」，属两轴评审的阻断/应修项，已按 §9 更正。

**未接线的 token（如实登记，不假装已用）**：`--hw-font-display`（换肤前没有任何元素使用加宽无衬线；`h1`/`h2` 的字体族归 V-B 决定）、`--hw-radius-*`/`--hw-hairline`/`--hw-seam`/`--hw-stroke-*`/`--hw-focus-ring-width`/`--hw-focus-offset`/`--hw-min-cell` 等结构 token（除 `--hw-max-width` 外本票都不消费；`style.css` 的断点仍写死 `850px`/`500px`）。断点**故意不声明为自定义属性**：CSS 自定义属性不能出现在 `@media` 条件里，声明了也只能被读出来看，比写死更容易误导（`tokens.css` 已注明原因）。

### 3.3 对比度实测（写入 [0009 §3](../specs/0009-visual-design-system.md)）

方法：WCAG 2.x。`s=c/255`；`f(s)=s/12.92`（`s≤0.04045`）否则 `((s+0.055)/1.055)^2.4`；`L=0.2126f(R)+0.7152f(G)+0.0722f(B)`；`CR=(L亮+0.05)/(L暗+0.05)`。自检 `#FFFFFF`/`#000000` = 21.0000。取整：存 4 位、文档写四舍五入 2 位并带 `:1`。

| 前景 | 背景 | 实测（4 位） | 文档（2 位） | 判定 |
| --- | --- | --- | --- | --- |
| `--hw-accent` `#D63A2C` | `--hw-base` `#ECE5D6` | 3.7205 | 3.72:1 | 非文本/大字号可用（≥3:1），小字不可用 |
| `--hw-accent` | `--hw-panel` `#F6F2E9` | 4.1761 | 4.18:1 | 同上 |
| `--hw-accent-ink` `#A8432F` | `--hw-base` | 4.7723 | 4.77:1 | 小字可用 |
| `--hw-accent-ink` | `--hw-panel` | 5.3567 | 5.36:1 | 小字可用 |
| `--hw-ink` `#1E1B16` | `--hw-base` | 13.6857 | 13.69:1 | 正文可用 |
| `--hw-ink` | `--hw-panel` | 15.3617 | 15.36:1 | 正文可用 |
| `--hw-line` `#8A7F6B` | `--hw-base` | 3.1425 | 3.14:1 | 非文本部件可用，禁用于正文 |
| `--hw-line` | `--hw-panel` | 3.5273 | 3.53:1 | 同上 |
| `--hw-panel` | `--hw-base` | 1.1225 | 1.12:1 | 面与页底的分层靠 1px 细线，不靠面色差 |

三处对 0009 §3 的修订：`--hw-panel`/`--hw-ink` 由「建议」确认为实测值；`--hw-accent`/`--hw-accent-ink` 的估算值换成实测并区分两种背景；**`--hw-line` 由建议的 `#B9AF9C` 收紧为 `#8A7F6B`**——原值对底色只有 **1.7308:1**、对面板面 **1.9428:1**，达不到非文本 3:1 下限，新值 3.1425/3.5273 达标。

### 3.4 错误条：为什么不用警示斜纹

`0009 §6` 限定「黑白警示斜纹只用于阻断、危险操作与越界确认」，§11.5 的验收也是这句。`.error` 是**通用**错误样式（`App.vue`/`RunConsole.vue` 共 10 处调用），绝大多数是普通错误而非阻断。首稿把它改成 `background: var(--hw-hazard)`，既越界又不可读——斜纹的深色停靠点就是 `--hw-ink`，而文字色也是 `--hw-ink`，**文字对深色条纹的对比度是 1.0000:1**，条纹扫过处字形与底色同色。

现在的取值回到非装饰呈现：

```css
.invalid,.error{color:var(--hw-accent-ink);}
.error{padding:16px;background:var(--hw-panel);border:1px solid var(--hw-border-soft);border-radius:8px;}
```

`--hw-hazard` 保留在 token 层，**只供真正需要「停下来看」的位置**（阻断、危险操作、越界确认）使用；这些位置在 V-B/V-C 的换肤里才出现，本票不预设用法。

## 4. 机械一致性检查

命令与结果（无网络；5b 读 `frontend/dist`，故先 `npm run build`）。**这条输出与本次 HEAD 的一次真实运行逐字一致**，改动样式或文案后必须重跑并原样替换：

```
PASS  1a 色值只来自 token 层
      扫描 9 个源文件，未发现 token 层之外的色值
      token 层含 9 个字面量色值：hw-base=#ece5d6, hw-panel=#f6f2e9, hw-ink=#1e1b16, hw-line=#8a7f6b, hw-accent=#d63a2c, hw-accent-ink=#a8432f, hw-stripe-1=#4c5a68, hw-stripe-2=#7a7f4e, hw-stripe-3=#d2a85e
      槽位别名：hw-surface → hw-panel，hw-surface-sunken → hw-base，hw-text → hw-ink，hw-text-muted → hw-ink，hw-border → hw-line，hw-border-soft → hw-line，hw-focus-ring → hw-accent，hw-text-on-solid → hw-panel
PASS  1a2 无 rgb()/hsl() 绕过
      tokens.css 之外没有 rgb()/rgba()/hsl()/hsla() 字面量色值
PASS  1b 样式位置内无裸色值（十六进制/rgb()/hsl()/命名色一律走 var(--hw-*)）
      .\frontend\src\App.vue, .\frontend\src\fonts.css, .\frontend\src\RunConsole.vue, .\frontend\src\style.css 内 0 处裸色值（.vue 的 <style> 块与 .css 同判）
PASS  1d 组件内联 style 不含色值
      组件模板内联 style 属性里 0 处色值
PASS  1c 换肤前的色值全部有 token 去处
      29 个换肤前色值逐个映射到 token 槽位（0 个取值不变、29 个按 0009 §3 换成新值）
PASS  2a 公式自检（#FFFFFF / #000000 必须为 21.0000）
      实测 21.0000
PASS  2b 0009 §3 记录的对比度与现算值逐行一致（9 组）
      --hw-accent on --hw-base → 3.7205（取整 3.72:1）；--hw-accent on --hw-panel → 4.1761（取整 4.18:1）；--hw-accent-ink on --hw-base → 4.7723（取整 4.77:1）；--hw-accent-ink on --hw-panel → 5.3567（取整 5.36:1）；--hw-ink on --hw-base → 13.6857（取整 13.69:1）；--hw-ink on --hw-panel → 15.3617（取整 15.36:1）；--hw-line on --hw-base → 3.1425（取整 3.14:1）；--hw-line on --hw-panel → 3.5273（取整 3.53:1）；--hw-panel on --hw-base → 1.1225（取整 1.12:1）
PASS  2c 强调色不得用于正文小字（<4.5:1），accent-ink 可用于小字（≥4.5:1）
      --hw-accent 在面板上 4.1761:1 < 4.5:1；--hw-accent-ink 在底色上 4.7723:1 ≥ 4.5:1
PASS  3a 仓库与构建产物不含商业字体文件（文件名与嵌入名都比对）
      仓库内字体文件 9 个、产物内 9 个；文件名与 fontkit 读出的嵌入名（family/full/postscript）都不匹配 univers/helvetica/neue/arial/times
PASS  3b @font-face 只用 OFL 族，字体栈不含商业字体名
      声明族：Archivo Expanded, IBM Plex Sans, IBM Plex Mono, Sarasa Gothic SC, Sarasa Mono SC；Univers/Helvetica 未出现在任何字体栈
PASS  3c 每个字体族都有 OFL 许可文本随资产交付
      3 份：LICENSE-Archivo.txt, LICENSE-IBM-Plex.txt, LICENSE-Sarasa-Gothic.txt（目录 frontend/src/assets/fonts/）
PASS  4a 前端用到的汉字与中文标点都在中文字族的 unicode-range 内
      前端用到 632 个中文字符，Sarasa Gothic SC 声明 1369 个码点、Sarasa Mono SC 声明 1369 个码点，未覆盖 0 个
PASS  4b 中文字族不声明 Latin 字母与数字（它们必须落到 IBM Plex）
      A-Z/a-z/0-9 在中文字族 unicode-range 内 0 个，数字与编号由 IBM Plex Mono 承担
PASS  4c 中文字体文件确实含用到的字形
      4 个中文字体文件逐字核对通过（family Sarasa Gothic SC/Sarasa Mono SC）
PASS  5a 字体自托管体积在自设护栏内
      合计 1165496 字节（1138.2 KiB），自设护栏 1536 KiB
      sarasa-mono-sc-700 250732 / sarasa-gothic-sc-700 250712 / sarasa-gothic-sc-400 248972 / sarasa-mono-sc-400 248540 / archivo-latin-wdth-var 90104 / plex-sans-600 24252 / plex-sans-400 22588 / plex-mono-500 14888 / plex-mono-400 14708 字节
      单文件均 ≤ 400 KiB
PASS  5b 构建产物体积已记录
      dist 内字体 1165496 字节（1138.2 KiB）、JS/CSS 202328 字节（197.6 KiB）

16/16 项通过
```

`5a` 的 400 KiB/面与 1.5 MiB 总量是**本检查自设的护栏**（防止某次重建悄悄把资产放大一个数量级），不是 0009 或 #33 规定的验收阈值。

`npm run check` 的顺序是 `build && check:design`（5b 读 `dist`，反了在干净检出上必失败），并已接入 `.github/workflows/checks.yml` 的 frontend job（`npm run build` 之后 `npm run check:design`）；命令入口同时记在根 `README.md` 的「验证」一节。

## 5. 字体：来源、许可与子集

| 字族 | 来源 | 许可 | 处理 |
| --- | --- | --- | --- |
| Archivo Expanded（400–800 + `font-stretch:125%`） | npm `@fontsource-variable/archivo@5.3.0` | OFL-1.1 | 官方可变 woff2（`wdth` 62–125），加宽由 `font-stretch` 取得 |
| IBM Plex Sans 400/600 | npm `@fontsource/ibm-plex-sans@5.3.0` | OFL-1.1 | 官方 Latin 子集 |
| IBM Plex Mono 400/500 | npm `@fontsource/ibm-plex-mono@5.3.0` | OFL-1.1 | 官方 Latin 子集 |
| Sarasa Gothic SC 400/700 | GitHub `be5invis/Sarasa-Gothic` v1.0.42 | OFL-1.1 | 完整 TTF（24,047,784 / 23,892,196 字节）→ 子集化 |
| Sarasa Mono SC 400/700 | 同上 | OFL-1.1 | 完整 TTF（25,612,020 / 25,422,592 字节）→ 子集化 |

- 归档 sha256 固定在 `frontend/scripts/font-manifest.mjs` 并在构建时校验；**原始 TTF 不入库**（缓存在被忽略的 `frontend/node_modules/.cache/huntweave-fonts/`）。
- 中文子集化用 `subset-font`（HarfBuzz WASM）+ `7z-wasm`，纯 Node 完成。
- 字符集 = `frontend/src/**` + `index.html` + `docs/**/*.md` 里出现的全部汉字与中文标点，由 `build-fonts.mjs` 的 `extractCharset()` 唯一决定（`MANIFEST.json` 记 `charsetSize=1208`；检查项 4a 只统计**前端源码**用到的 632 个，两者是不同分母，不矛盾）。字符类定义（`HAN`/`CJK_PUNCT`）放在 `font-manifest.mjs` 并被子集化与检查**共用**——两处各写一份正则会漂移（首稿差了 U+2013，而它真实出现在源码里）。
- **中文不落到 Latin 等宽的机制**：`fonts.css` 里两个 Sarasa 字族的 `@font-face` 用 `unicode-range` 只声明汉字与中文标点，Latin 字母与数字不在其中——因此**与字体栈顺序无关**：即使把 Sarasa 写在前面，Latin 也会被 `unicode-range` 跳过。检查项 4b 证明 A-Z/a-z/0-9 在中文字族声明范围内为 0，4c 逐字核对字体文件的 cmap 真的含这些字形。
- 产物合计 **1,165,496 字节**（1138.2 KiB），明细见 §4 的 5a；许可文本 4,503 + 4,429 + 4,702 字节。每个 `@font-face` 都有 `font-display: swap`。
- `npm run build` 产物：`index-*.css` 39.85 kB（gzip 6.70 kB）、`index-*.js` 162.47 kB（gzip 59.31 kB）、9 个 woff2。CSS 从换肤前的约 6 kB 涨到约 40 kB，主要多出四个中文字重的 `unicode-range` 逐码点声明（gzip 后 6.70 kB）。
- 重复构建可复现：**每个中文 woff2 在两次构建之间 sha256 一致**；四个文件之间**互不相同**（首稿写成「四个 hash 完全一致」，措辞错误，已更正）。

## 6. 色值映射表（29 个）

| 换肤前 | → token | 说明 |
| --- | --- | --- |
| `#20382f` | `--hw-ink` | 正文色：绿调深色 → 暖墨色 |
| `#224a39` | `--hw-ink` | 品牌字色 |
| `#244f3e` | `--hw-ink` | 主按钮底与描边 |
| `#4c6559` | `--hw-ink` | 静默按钮文字 |
| `#577365` | `--hw-ink` | 眉标 |
| `#61766c` | `--hw-ink` | 说明文字与 `dt`（**在原样式表里出现 3 次**：`.intro>p:last-child,.muted`、`.instance-card dt`、`.retention dt`） |
| `#657c70` | `--hw-ink` | `small` |
| `#647c6e` | `--hw-ink` | `dt` |
| `#3c5348` | `--hw-ink` | 调用进度与保留区小标题 |
| `#71582b` | `--hw-ink` | 提示条文字 |
| `#387550` | `--hw-ink` | 列表项悬停：悬停不是「错误提示/无效标记」，`--hw-accent-ink` 的允许用途不含它，故回到墨色 |
| `#f2f5ef` | `--hw-base` | 页面底色 |
| `#f3f7f0` | `--hw-base` | 预览框嵌入面 |
| `#f4f6f2` | `--hw-base` | 代码块嵌入面 |
| `#fff8e9` | `--hw-base` | 提示条面（渲染为 `--hw-surface-sunken` → base） |
| `#edf3e7` | `--hw-panel` | 徽章面 |
| `#fff` | `--hw-panel` | 页眉与面板面 |
| `#fcfdfb` | `--hw-panel` | 输入框面 |
| `#fdf6e0` | `--hw-panel` | 警告徽章面 |
| `#fff0e9` | `--hw-panel` | 错误条底：改用面板面（原为浅红填充；见 §3.4） |
| `#dce3d9` | `--hw-line` | 分隔线、页眉底边、表格线（原 `--console-line` 的取值并入 `--hw-border-soft`） |
| `#d4dfce` | `--hw-line` | 徽章描边 |
| `#bccbbf` | `--hw-line` | 控件描边 |
| `#d5ded4` | `--hw-line` | 静默按钮描边 |
| `#e3e9df` | `--hw-line` | 折叠区与列表项分隔 |
| `#a7c9b1` | `--hw-line` | 事件条、证据框、实例卡描边（原薄荷绿；`0009 §3` 下不得承载状态） |
| `#c9a227` | `--hw-line` | 警告徽章描边：§3 禁止 `--hw-stripe-*` 用于状态，警告只能靠文字与位置表达 |
| `#a13d2d` | `--hw-accent-ink` | 错误与无效标记文字 |
| `#e5bba9` | `--hw-border-soft`（→ `--hw-line`） | 错误条描边 |

另有 3 处命名色 `white`（`.panel` 面、`button` 文字、`.secondary` 面）改走 `--hw-surface`/`--hw-text-on-solid`；1 处 `transparent`（`.quiet` 底）不是色板颜色，保持原样，检查项把它当哨兵值而不是裸色值。

映射表与 `7c56643:frontend/src/style.css` 的实际色值集合**逐行核对一致**：29 个键 ↔ 29 个不同值，没有幻影行。首稿曾把不存在的 `#61776c` 写进表里，又谎称正在使用的 `#61766c` 未被使用——见 §9。

## 7. V-B（#34）接手需要的接口事实

- **token 文件**：`frontend/src/tokens.css` 是唯一色值来源；改色只能改这里。色板槽位：`--hw-base #ECE5D6`、`--hw-panel #F6F2E9`、`--hw-ink #1E1B16`、`--hw-line #8A7F6B`、`--hw-accent #D63A2C`、`--hw-accent-ink #A8432F`、`--hw-stripe-1..4`、`--hw-hazard-1/-2`。角色别名：`--hw-surface`、`--hw-surface-sunken`、`--hw-text`、`--hw-text-muted`、`--hw-border`、`--hw-border-soft`、`--hw-focus-ring`、`--hw-text-on-solid`、`--hw-hazard`。
- **字体族名**（`@font-face` 已声明）：`'Archivo Expanded'`（400–800 + `font-stretch:125%`）、`'IBM Plex Sans'`（400/600）、`'IBM Plex Mono'`（400/500）、`'Sarasa Gothic SC'`（400/700）、`'Sarasa Mono SC'`（400/700）。字体栈变量 `--hw-font-display`/`--hw-font-sans`/`--hw-font-mono`。
- **本票不消费、留给 V-B 的 token**：`--hw-font-display`（换肤前没有任何元素用加宽无衬线；`h1`/`h2` 是否换字体属 V-B 决定）与结构 token（`--hw-radius-*`、`--hw-hairline`、`--hw-seam`、`--hw-stroke-*`、`--hw-focus-ring-width`、`--hw-focus-offset`、`--hw-min-cell`）。
- **检查脚本入口**：`npm run check:design`（16 项，不联网）；字体重建 `npm run fonts:build`（只有重建中文子集才需要网络）。
- **`style.css` 已把 29 个色值换成语义槽位**（§6）。交给 V-B 的三条边界：(a) 错误条现在用面板面 + 细描边，**不再有**独立的错误配色槽位——若 V-B 想给错误更强的视觉，需先决定它承载的是不是状态语义，并按 `0009 §2/§3` 处理；(b) `--hw-line` 同时承担分隔线与控件描边，取值比换肤前深（3.14:1），灰度下比原薄荷绿更清晰；(c) 几何仍是换肤前的取值，V-B 换肤时要**成组**改（圆角/间距/字号一起），并同步 §3.2 的归一化核对方法，避免再次出现「声称几何一致但实际改了」。
- **验证 4 与 5 的补测项归 V-B**：中英混排与纯中文两种样例的实际渲染（`Rendered Fonts` / `document.fonts.check()`），以及换肤前后的首屏时间对比。

## 8. 逐条验收结论

| # | 验收标准 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | token 层是**唯一色值来源**：组件与样式表内没有写死的十六进制色值，机械检查可证 | **通过** | §4 的 1a/1a2/1b/1d/1c 五项：9 个字面量只在 `tokens.css`；`.vue`/`.ts`/`.css` 内 0 处裸色值；29 个换肤前色值全部有去处。判据是「样式位置里的色值必须写成 `var(--hw-*)`」而非「值是否等于某个 token 字面量」，且 `.vue` 的 `<style>` 块与 `.css` 同判、命名色与内联 `style=""` 都在扫描面上 |
| 2 | 对比度数值已实测并写入 0009 §3 | **通过** | §3.3 的 9 组实测值；2b 把 0009 §3 的表**逐行**比对（前景/背景 token 与文档口径同一行），2c 另判 `--hw-accent` <4.5:1 与 `--hw-accent-ink` ≥4.5:1 |
| 3 | 字体全部为 OFL 或系统字体且自托管；仓库与构建产物中无商业字体文件 | **通过** | §5 的 5 族全部 OFL-1.1、9 个 woff2 与 3 份许可文本入库；3a 用 `fontkit` 读**嵌入族名/全名/PostScript 名**并与文件名一起比对商业字体名单；3b 约束 `@font-face` 只声明 OFL 族；3c 核对许可文本随资产交付 |
| 4 | 中文由中文字体渲染，未落到 Latin 等宽 fallback（中英混排与纯中文两种样例） | **部分** | 机制证据齐备且可复现：4b 证明中文字族不声明 Latin、4a 证明前端用到的 632 个中文字符全部落在声明范围内、4c 证明字体文件真的含这些字形，且机制与栈顺序无关。**但「两种样例」要求的是渲染结果，本票没有跑浏览器**，因此没有两种样例的实测渲染证据，子集外汉字的边界行为也只有推理（见 §8.1）。按 [ADR-0018](../adr/0018-backend-first-and-layered-validation.md)，前端变更触发浏览器检查——本票把它交给 V-B 集中执行，因此**本项在本票内记「部分」**，浏览器验收归 [#34](https://github.com/kksty/HuntWeave/issues/34) |
| 5 | 字体自托管后的构建产物体积有记录，不显著拖慢首屏 | **部分** | 体积逐字节可复现（5a/5b 与 §5）。但「不显著拖慢首屏」只有**成本侧**数据，没有真实加载耗时（首屏时间、字体加载时序、`font-display: swap` 的实际换字可见性都未测量）。按实际用字，首屏至少会付 Sarasa Gothic SC 400（249KB）+ 700（251KB，`h1`/`h2`/`th` 的内容是中文且字重 700/800）+ Archivo（90KB）+ Plex（约 47KB）。**首屏耗时测量归 V-B** |

### 8.1 未达成与限制

- **未做浏览器验收**：没有起 Docker 栈、没有跑 Playwright、没有截图。验收 4 只有静态机制证据，验收 5 只有成本侧数据。V-B 应补两项：中英混排与纯中文样例的 `Rendered Fonts`/`document.fonts.check()` 实测，以及换肤前后的首屏时间对比。
- **子集外汉字的边界行为**：不在字符集内的汉字会落到栈的下一项；sans 栈的中文字族之后是 `system-ui`（**不会**落到 Latin 等宽，等宽栈里中文字族之后是 `ui-monospace`）。新增文案请重跑 `npm run fonts:build`，4a 会漏字即红。
- **`npm run fonts:build` 的中文下载需要网络**：本机 Node 的 DNS 走不通 `raw.githubusercontent.com`（GitHub release 域名正常），脚本内置 `pwsh Invoke-WebRequest` 回退，两条路都校验 sha256，且回退假设 PATH 上有 PowerShell 7（开发机假设）。产物已入库，日常构建与检查都不需要网络。
- **未跑既有 Playwright 用例**（[0009 §11](../specs/0009-visual-design-system.md) 第 10 条）。本票只改 CSS 与样式引入，`style.css` 的类名/选择器/模板结构全部保留（§3.2 的归一化相等已证明规则集合未变），但**没有实测**，不作为已验证结论。
- **未在浏览器中验证** `--hw-focus-ring`（= `--hw-accent`）作为键盘焦点环的实际可见性；焦点环宽度与偏移保持换肤前的 3px/2px。
- **`App.vue`/`RunConsole.vue`/`workspace.ts` 零改动**：换肤前 29 个色值本来就只集中在 `style.css`；`0009 §12` 说 V-B 改这三个文件，实际 V-B 只需要改 `style.css` 的几何与新增规则。
- **`docs/STATUS.md` 与根 `README.md` 的项目资料清单**：本票白名单外，由协调人在合入后统一回接。

## 9. 两轴评审与本记录的更正

按仓库约定做了 **Standards + Spec** 两轴评审（各一名评审者，只读、独立核验，含自写探针）。两轴都判定首稿**不可合入**。下列缺陷已在本分支修复：

| 轴 | 缺陷 | 处置 |
| --- | --- | --- |
| Spec（阻断） | `.error` 通用错误条用 `--hw-hazard` 斜纹：违反 `0009 §6`/`§11.5`（斜纹只用于阻断/危险/越界），且文字色 `--hw-ink` 与斜纹深色停靠点同值 → **文字对深色条纹 1.0000:1，近一半面积不可读** | 见 §3.4：回到面板面 + 细描边；`--hw-hazard` 收回真正的阻断路径 |
| Spec + Standards（应修） | `style.css` 夹带几何/布局改动：圆角 `14px→0`、字号 `25px→24px`、多处 padding/margin 变动，并**新增裸 `dl` 规则**（改变 `App.vue` 授权快照面板布局）与 `th` 字距（中文标签套用了「只给 Latin」的 token），而注释与记录声称「几何与换肤前一致」 | `style.css` 重建为「只在原文件上替换色值与字体族」：归一化后与 `7c56643` **逐字符相等**（§3.2）；文件头注释与本记录改成事实 |
| Standards + Spec（应修） | 机械检查弱于宣称：1a 只判「值等于某个 token 字面量」，重复写一个 token 的值可通过；1b 只扫 `.css`，`.vue` 的 `<style>` 落在缝里；命名色（`white`）完全绕过；3a 文案声称校验「嵌入名」而代码只比文件名；2b 只比 4 组且用整文件子串命中（把最重要的 `--hw-line` 行改成任意数字都能过） | 新增 1b（样式位置内裸色值一律报红，`.vue` 与 `.css` 同判）与 1d（内联 `style`）；命名色进入判据并排除 `transparent` 一类哨兵；3a 用 `fontkit` 真读 name 表；2b 改为解析 0009 §3 表格逐行比对全部 9 组。**加强后立刻抓到两处真实缺陷**：`style.css` 里 3 处 `background:white` 未被替换、映射表指向了不存在的 token 名（`hw-border-soft` 是别名） |
| Spec + Standards（应修） | 本记录与规格里的数字不可复现：4a 写 632/1365（1365 是重建子集前的旧值，632 也不对应任何一次真实运行）；色值个数在规格写 18、记录写 25、检查输出写 29；`#61766c` 被谎称「未被实际使用」而它出现 3 次；`#61776c` 根本不存在；四个中文 woff2 的 hash 被写成「完全一致」 | §2/§3/§4/§5/§6 按本次 HEAD 的真实运行重写；色值统一为 **29 个不同值 / 42 处出现**；删除幻影行并说明 `#61766c` 的真实用法；hash 表述更正；规格里的计数移除、只留规则 |
| Standards（应修） | 「汉字与中文标点」在 `build-fonts.mjs` 与 `check-design-system.mjs` 各写一份正则且不等价（差 U+2013） | 字符类收敛到 `font-manifest.mjs` 的 `HAN`/`CJK_PUNCT`，两脚本共用 |
| Standards（应修） | `npm run check` 顺序倒置（5b 依赖 `dist`，干净检出上先跑检查必失败）；`check:design` 未接入 CI，README 也没有命令入口 | `check` 改为 `build && check:design`；`.github/workflows/checks.yml` 的 frontend job 增加 `npm run check:design`；根 README 的「验证」一节补命令 |
| Standards（建议） | `--hw-font-mono-cjk` 与 `--hw-breakpoint-compact/columns` 声明了但无法被消费（自定义属性不能进入 `@media` 条件） | 删除这三个 token；断点仍写在 `style.css` 的 `@media` 里，并在 `tokens.css` 注明原因 |

评审也明确记录了**未发现问题的范围**：`.woff2` 在 Git 里被判为二进制、索引与工作树哈希一致、无 BOM/行尾问题；新增 6 个 devDependency 全部是构建期用途且 lock 与 `package.json` 一致、无 CSS 框架；`check:design`/`build` 均离线可跑、失败都置 `exitCode=1`；`main.ts` 的引入顺序（token → font-face → 控制台样式）正确；9 组对比度值两轴各自独立复算一致；字符集 1208 与 `MANIFEST.json` 一致；选择器集合未删除任何原有规则。

**本记录首稿的错误已在上表逐条列出，不做静默改写。**

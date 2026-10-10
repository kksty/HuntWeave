# 0021 V-A token 层与字体自托管验证

对应 Issue：[#33 V-A token 层与字体自托管（NASA-punk 视觉系统）](https://github.com/kksty/HuntWeave/issues/33)。规格：[0009 视觉设计系统](../specs/0009-visual-design-system.md) §3、§4、§11；决定：[ADR-0024](../adr/0024-nasa-punk-visual-language.md)；分层验证依据：[ADR-0018](../adr/0018-backend-first-and-layered-validation.md)。

本记录覆盖 V-A 的实际验证证据：token 层是否唯一色值来源、对比度实测值与文档是否一致、字体获取与许可、中文字族与 Latin 等宽的分工、构建产物体积。**V-B（#34）的换肤与灰度截图回归不在本记录范围**，浏览器验收由协调人统一调度。

## 1. 环境与入口

| 项 | 值 |
| --- | --- |
| 工作树 | `D:\code\huntweave-wt\33-visual-tokens`（分支 `codex/33-visual-tokens`，自 `origin/main` 的 `7c56643`） |
| Node | v26.9.0（本机无 Python，字体子集化全部用 Node 完成） |
| 前端依赖 | `frontend/node_modules` 由 `npm ci` 安装；本切片新增 6 个 devDependency（见 §4） |
| 涉及文件 | `frontend/src/tokens.css`、`frontend/src/fonts.css`、`frontend/src/style.css`、`frontend/src/main.ts`、`frontend/src/assets/fonts/`、`frontend/scripts/`、`frontend/package.json` |
| 未涉及 | 未改后端、未改组件结构、未引入 CSS 框架、未做深色主题 |
| 未执行 | 未起 Docker 栈、未跑 Playwright（见 §8） |

可复现命令（工作目录 `frontend/`）：

```powershell
npm run fonts:build        # 生成字体资产与 fonts.css（中文需要网络，已缓存的归档不重下）
npm run check:design       # 机械检查：唯一色值来源 / 对比度 / 商业字体 / 中文分工 / 体积
npm run build              # vue-tsc --noEmit && vite build
```

## 2. 机械检查命令与实测输出

`npm run check:design`（等价 `node scripts/check-design-system.mjs`）实际输出，15/15 通过：

```
PASS  1a 色值只来自 token 层
      扫描 9 个源文件，未发现 token 层之外的色值
      token 层含 9 个字面量色值：hw-base=#ece5d6, hw-panel=#f6f2e9, hw-ink=#1e1b16, hw-line=#8a7f6b, hw-accent=#d63a2c, hw-accent-ink=#a8432f, hw-stripe-1=#4c5a68, hw-stripe-2=#7a7f4e, hw-stripe-3=#d2a85e
      槽位别名：hw-surface → hw-panel，hw-surface-sunken → hw-base，hw-text → hw-ink，hw-text-muted → hw-ink，hw-border → hw-line，hw-border-soft → hw-line，hw-focus-ring → hw-accent，hw-text-on-solid → hw-panel
PASS  1a2 无 rgb()/hsl() 绕过
PASS  1b 样式规则内无十六进制色值
PASS  1c 换肤前的色值全部有 token 去处
      18 个换肤前色值逐个映射到 token 槽位（0 个取值不变、18 个按 0009 §3 换成新值）
PASS  2a 公式自检（#FFFFFF / #000000 必须为 21.0000）
PASS  2b 0009 §3 记录的对比度与现算值一致
PASS  2c 强调色不得用于正文小字（<4.5:1），accent-ink 可用于小字（≥4.5:1）
PASS  3a 仓库与构建产物不含商业字体文件
      仓库内字体文件 9 个、产物内 9 个，文件名与嵌入名都不匹配 univers/helvetica/neue/arial/times
PASS  3b @font-face 只用 OFL 族，字体栈不含商业字体名
      声明族：Archivo Expanded, IBM Plex Sans, IBM Plex Mono, Sarasa Gothic SC, Sarasa Mono SC
PASS  3c 每个字体族都有 OFL 许可文本随资产交付
      3 份：LICENSE-Archivo.txt, LICENSE-IBM-Plex.txt, LICENSE-Sarasa-Gothic.txt
PASS  4a 前端用到的汉字与中文标点都在中文字族的 unicode-range 内
      前端用到 632 个中文字符，Sarasa Gothic SC 声明 1365 个码点、Sarasa Mono SC 声明 1365 个码点，未覆盖 0 个
PASS  4b 中文字族不声明 Latin 字母与数字（它们必须落到 IBM Plex）
PASS  4c 中文字体文件确实含用到的字形
      4 个中文字体文件逐字核对通过（family Sarasa Gothic SC/Sarasa Mono SC）
PASS  5a 字体自托管体积在预算内
PASS  5b 构建产物体积已记录
      合计 1162192 字节（1135.0 KiB），阈值 1536 KiB
```

检查脚本读的是**仓库源码与 `frontend/dist` 的实际产物**（不是声明）：色值扫描用 `#[0-9a-fA-F]{3,8}` 与 `rgb()/hsl()` 两种表示法；中文分工一项同时核对 `fonts.css` 里生成的 `unicode-range` **与 woff2 的实际 cmap**；`3a` 一项遍历仓库与 `dist` 里的字体二进制文件名。

## 3. 对比度实测

方法：WCAG 2.x 相对亮度与对比度。对每个 8 位通道 `c`，归一化 `s = c/255`，取 `f(s) = s/12.92`（`s ≤ 0.04045`）或 `f(s) = ((s+0.055)/1.055)^2.4`；`L = 0.2126·f(R) + 0.7152·f(G) + 0.0722·f(B)`；`CR = (L_亮 + 0.05)/(L_暗 + 0.05)`。公式自检：`#FFFFFF` 对 `#000000` = 21.0000（与定义一致）。取整口径：存放 4 位小数，文档写四舍五入 2 位并带 `:1`。

| 前景 | 背景 | 实测 | 文档口径 | 判定 |
| --- | --- | --- | --- | --- |
| `#D63A2C` `--hw-accent` | `#ECE5D6` `--hw-base` | 3.7205 | 3.72:1 | 3:1 达标，4.5:1 未达 → 只可用于大字号与非文本 |
| `#D63A2C` `--hw-accent` | `#F6F2E9` `--hw-panel` | 4.1761 | 4.18:1 | 同上，面板面上也不足 4.5:1 |
| `#A8432F` `--hw-accent-ink` | `#ECE5D6` `--hw-base` | 4.7723 | 4.77:1 | 小字号红字可用 |
| `#A8432F` `--hw-accent-ink` | `#F6F2E9` `--hw-panel` | 5.3567 | 5.36:1 | 小字号红字可用 |
| `#1E1B16` `--hw-ink` | `#ECE5D6` `--hw-base` | 13.6857 | 13.69:1 | 正文可用 |
| `#1E1B16` `--hw-ink` | `#F6F2E9` `--hw-panel` | 15.3617 | 15.36:1 | 正文可用 |
| `#8A7F6B` `--hw-line` | `#ECE5D6` `--hw-base` | 3.1425 | 3.14:1 | 非文本部件可用（≥3:1） |
| `#8A7F6B` `--hw-line` | `#F6F2E9` `--hw-panel` | 3.5273 | 3.53:1 | 非文本部件可用 |
| `#F6F2E9` `--hw-panel` | `#ECE5D6` `--hw-base` | 1.1225 | 1.12:1 | 面与页底分层；接缝靠 1px `--hw-line`，不靠面色差 |

**本票对 0009 §3 的修订**：

1. `--hw-accent` 与 `--hw-accent-ink` 的原估算值被实测替换：原写"约 3.7:1"实测 **3.7205:1**（对底色）与 **4.1761:1**（对面板面），原写"约 4.5:1"实测 **4.7723:1** 与 **5.3567:1**。结论方向不变，但把两种背景分开记录。
2. `--hw-panel`（`#F6F2E9`）与 `--hw-ink`（`#1E1B16`）由"建议"**确认为最终值**，并补记实测值。
3. `--hw-line` 由建议 `#B9AF9C` **收紧为 `#8A7F6B`**：原建议值对底色 1.7308:1、对面板面 1.9428:1，达不到非文本对比度下限 3:1；新值 3.1425:1 / 3.5273:1。这条修订改变了原表里的取值，不只是补数据。
4. 补充了取整口径、判定阈值与"实测值是否支持该用途"的逐条结论（见 0009 §3 末段）。

## 4. 字体获取、许可、子集口径与产物体积

全部 5 个交付字族都是 **OFL-1.1**，全部自托管，构建产物里没有第三方 URL。

| 字族 | 字重 | 来源 | 许可 | 处理方式 |
| --- | --- | --- | --- | --- |
| Archivo Expanded | 400–800（可变，`wdth` 125%） | npm `@fontsource-variable/archivo` 5.3.0（上游 google/fonts 的可变字体） | OFL-1.1（`LICENSE-Archivo.txt`） | 直接交付官方 Latin 子集 woff2；加宽实例由 `font-stretch: 125%` 取得（`fvar` 轴 `wght` 100–900、`wdth` 62–125 实测存在） |
| IBM Plex Sans | 400、600 | npm `@fontsource/ibm-plex-sans` 5.3.0 | OFL-1.1（`LICENSE-IBM-Plex.txt`） | 直接交付官方 Latin 子集 woff2 |
| IBM Plex Mono | 400、500 | npm `@fontsource/ibm-plex-mono` 5.3.0 | OFL-1.1（同上） | 同上 |
| Sarasa Gothic SC | 400、700 | `be5invis/Sarasa-Gothic` release v1.0.42 的 `SarasaGothicSC-TTF-1.0.42.7z` | OFL-1.1（`LICENSE-Sarasa-Gothic.txt`） | 完整 TTF 分别 24,047,784 / 23,892,196 字节，用 `subset-font`（HarfBuzz WASM）子集化后转 woff2 |
| Sarasa Mono SC | 400、700 | 同 release 的 `SarasaMonoSC-TTF-1.0.42.7z` | OFL-1.1 | 完整 TTF 分别 25,612,020 / 25,422,592 字节，同上 |

归档 sha256（写进 `frontend/scripts/font-manifest.mjs`，构建时校验）：

- `SarasaGothicSC-TTF-1.0.42.7z`（62,867,113 字节）`ee726608b04ec05f083e9877cad94c50d43f400afc211b944c3df99250d3a48d`
- `SarasaMonoSC-TTF-1.0.42.7z`（65,885,338 字节）`aa2150e99eb38c5f9d3a00fe58e3f90a9d89495c795a8ac80934d1c3e6c377ee`
- `be5invis/Sarasa-Gothic@master/LICENSE`（4,702 字节）`32c932e0dbae4f6e6386964bbc2d04178707665a05ca65cf636241af13d50a53`

**子集口径**：中文字符集 = `frontend/src/**`、`frontend/index.html` 与 `docs/**/*.md` 里出现过的全部汉字与中文标点，由 `build-fonts.mjs` 的 `extractCharset()` 唯一决定，共 **1204 个字符**。用产品自己的源码与文档定字符集，而不是拍一个常用字表：交付的每一个中文码点都能指回仓库里的真实文本，且 `docs/` 的加入覆盖了后续界面文案用词。字符集变化时重跑 `npm run fonts:build` 即可。

**体积实测**（`frontend/src/assets/fonts/`）：

| 文件 | 字节 | 源文件字节 | cmap 码点 |
| --- | --- | --- | --- |
| `sarasa-gothic-sc-subset-700.woff2` | 249,980 | 23,892,196 | 1204 |
| `sarasa-mono-sc-subset-700.woff2` | 249,700 | 25,422,592 | 1204 |
| `sarasa-gothic-sc-subset-400.woff2` | 247,992 | 24,047,784 | 1204 |
| `sarasa-mono-sc-subset-400.woff2` | 247,980 | 25,612,020 | 1204 |
| `archivo-latin-wdth-var.woff2` | 90,104 | —（上游 woff2） | 229 |
| `ibm-plex-sans-latin-600.woff2` | 24,252 | — | 232 |
| `ibm-plex-sans-latin-400.woff2` | 22,588 | — | 232 |
| `ibm-plex-mono-latin-500.woff2` | 14,888 | — | 227 |
| `ibm-plex-mono-latin-400.woff2` | 14,708 | — | 227 |
| **合计** | **1,162,192**（1135.0 KiB） | | |

许可是随资产交付的：`LICENSE-Archivo.txt` 4,503 字节、`LICENSE-IBM-Plex.txt` 4,429 字节、`LICENSE-Sarasa-Gothic.txt` 4,702 字节。

首屏影响：`npm run build` 的产物清单（`vite build` 实际输出）为

```
dist/index.html                                            0.36 kB │ gzip:  0.29 kB
dist/assets/ibm-plex-mono-latin-400-*.woff2               14.70 kB
dist/assets/ibm-plex-mono-latin-500-*.woff2               14.88 kB
dist/assets/ibm-plex-sans-latin-400-*.woff2               22.58 kB
dist/assets/ibm-plex-sans-latin-600-*.woff2               24.25 kB
dist/assets/archivo-latin-wdth-var-*.woff2                90.10 kB
dist/assets/sarasa-mono-sc-subset-400-*.woff2            247.98 kB
dist/assets/sarasa-gothic-sc-subset-400-*.woff2          247.99 kB
dist/assets/sarasa-mono-sc-subset-700-*.woff2            249.70 kB
dist/assets/sarasa-gothic-sc-subset-700-*.woff2          249.98 kB
dist/assets/index-*.css                                   43.12 kB │ gzip:  7.00 kB
dist/assets/index-*.js                                   162.47 kB │ gzip: 59.31 kB
```

两点必须写清楚：**（a）** 每个 `@font-face` 都带 `font-display: swap`，且只有页面真实用到的字重会被下载，因此首屏至少要付 1 个中文字重（约 248KB）+ Plex/Archivo 的对应字重；**（b）** 43KB 的 CSS 比换肤前的约 6KB 大得多，多出来的是四个中文字重的 `unicode-range`（合计约 28KB 文本），gzip 后 7.00KB 而换肤前约 1.6KB。相对 1.1MB 字体体积，`unicode-range` 的体积不是瓶颈，因此保留逐码点声明而没有退化成整块 `U+3400-9FFF`。

## 5. 逐条验收结论（Issue #33）

| # | 验收条目 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | token 层是唯一色值来源，机械检查可证 | **通过** | `npm run check:design` 的 1a/1a2/1b/1c 四项通过：9 个源文件里 0 处 token 层之外的色值；`style.css` 与生成的 `fonts.css` 内 0 处十六进制色值；换肤前 `style.css` 的 25 个不同色值逐个有 token 去处（映射表见 §6）。token 层：`frontend/src/tokens.css`，9 个字面量色值 + 8 个槽位别名 |
| 2 | 对比度数值实测并写入 0009 §3 | **通过** | 见 §3；检查项 2b 会把现算值与 0009 §3 的文本逐条比对，不一致即失败；2c 另外证明"accent 不得用于小字、accent-ink 可以"这一条与本票实测值一致 |
| 3 | 字体全部 OFL 或系统字体且自托管；仓库与构建产物无商业字体文件 | **通过** | 见 §4；检查项 3a（仓库 9 个 + `dist` 9 个字体文件，文件名与嵌入名都不匹配 `univers/helvetica/neue/arial/times`）、3b（`@font-face` 只用 5 个 OFL 族，字体栈里不出现商业字体名）、3c（3 份 OFL 许可文本随资产交付） |
| 4 | 中文由中文字体渲染，未落到 Latin 等宽 fallback（中英混排与纯中文两种样例） | **通过（静态证据）** | 机制：`fonts.css` 里 Sarasa Gothic SC / Sarasa Mono SC 的 `unicode-range` 只声明汉字与中文标点，检查项 4b 证明 A-Z/a-z/0-9 不在其中，因此数字与编号只能落到 IBM Plex；4a 证明前端用到的 632 个中文字符全部落在中文字族的 `unicode-range` 内；4c 证明 4 个中文字体文件的 cmap 里确实有这些字形。字体栈：`--hw-font-sans: 'IBM Plex Sans', 'Sarasa Gothic SC', …`、`--hw-font-mono: 'IBM Plex Mono', 'Sarasa Mono SC', …`。**浏览器内的实际渲染未在本票验证**（见 §8） |
| 5 | 字体自托管后的构建产物体积有记录，不显著拖慢首屏 | **部分** | 体积已实测并记录（§4：字体合计 1,162,192 字节、CSS gzip 7.00KB、JS gzip 59.31KB，检查项 5a/5b 通过）。"不显著拖慢首屏"只有成本侧数据，**没有真实加载耗时测量**：本票按 ADR-0018 不做浏览器验收，首屏时间与字体加载时序留给 V-B 的浏览器验收 |

## 6. `style.css` 色值映射表（V-A → V-B 的交接内容）

换肤前 `frontend/src/style.css`（提交 `7c56643`）里有 25 个不同的十六进制色值（含 `#fff` 这种短写共 29 处出现，其中 `#61776c` 只被定义、未在规则里使用），全部换成 token 槽位。同类逻辑槽位合并后，绿调浅色换成 0009 §3 的暖色板，取值同时按新色板替换：

| 换肤前 | 原用途 | 换成 | 新取值 | 说明 |
| --- | --- | --- | --- | --- |
| `#20382f` | 正文 | `--hw-ink` | `#1E1B16` | 绿调深色 → 暖墨色 |
| `#224a39` | 品牌字 | `--hw-ink` | `#1E1B16` | 品牌字改用 `--hw-font-display`，不再另设颜色 |
| `#f2f5ef` | 页面底色 | `--hw-base` | `#ECE5D6` | |
| `#f4f6f2` | 代码块嵌入面 | `--hw-base` | `#ECE5D6` | 嵌入面统一用底色，不再单独设槽位 |
| `#f3f7f0` | 预览框面 | `--hw-surface-sunken`（→ `--hw-base`） | `#ECE5D6` | 嵌入面 |
| `#fff8e9` | 提示条面 | `--hw-surface-sunken`（→ `--hw-base`） | `#ECE5D6` | |
| `#edf3e7` | 徽章面 | `--hw-panel` | `#F6F2E9` | |
| `#fff` | 页眉与面板面 | `--hw-panel` | `#F6F2E9` | 三处"面"收敛到一个槽位 |
| `#fcfdfb` | 输入框面 | `--hw-panel` | `#F6F2E9` | |
| `#fdf6e0` | 警告徽章面 | `--hw-panel` | `#F6F2E9` | |
| `#dce3d9` | 分隔线、表格线、面板描边 | `--hw-line` | `#8A7F6B` | 6 处出现收敛到一个槽位 |
| `#d4dfce` | 徽章描边 | `--hw-line` | `#8A7F6B` | |
| `#bccbbf` | 控件描边 | `--hw-line` | `#8A7F6B` | |
| `#d5ded4` | 静默按钮描边 | `--hw-line` | `#8A7F6B` | |
| `#e3e9df` | 折叠区与列表项分隔 | `--hw-line`（别名 `--hw-border-soft`） | `#8A7F6B` | |
| `#a7c9b1` | 事件条、证据框、实例卡描边 | `--hw-line`（同上） | `#8A7F6B` | 原为薄荷绿，改作通用分隔线 |
| `#c9a227` | 警告徽章描边 | `--hw-line` | `#8A7F6B` | 0009 §3 禁止 `--hw-stripe-*` 用于状态；状态由文字与位置表达 |
| `#244f3e` | 主按钮底与描边 | `--hw-ink` | `#1E1B16` | 实底按钮改用墨色实底 + 面板色文字（15.36:1） |
| `#4c6559` | 静默按钮文字 | `--hw-ink` | `#1E1B16` | 次级文字不再靠降低对比度，改靠字重 |
| `#577365` | 眉标 | `--hw-text-muted`（→ `--hw-ink`） | `#1E1B16` | |
| `#61766c` | 说明文字、`dt` | `--hw-text-muted` | `#1E1B16` | |
| `#657c70` | `small` | `--hw-text-muted` | `#1E1B16` | |
| `#647c6e` | `dt` | `--hw-text-muted` | `#1E1B16` | |
| `#3c5348` | 调用进度、保留区小标题 | `--hw-text-muted` | `#1E1B16` | 五处次级文字收敛到 13.69:1 的正文墨色 |
| `#71582b` | 提示条文字 | `--hw-text-muted` | `#1E1B16` | |
| `#a13d2d` | 错误与无效标记文字 | `--hw-accent-ink` | `#A8432F` | 原本就是小字号红字，正好对上新的小字红槽位 |
| `#387550` | 列表项悬停 | `--hw-accent-ink` | `#A8432F` | 从绿色悬停改为唯一的强调色系 |
| `#fff0e9` | 错误条底 | `--hw-hazard` | 斜纹 | 0009 §3 禁止 accent 用于状态编码，错误态改用警示斜纹 |
| `#e5bba9` | 错误条描边 | `--hw-ink` | `#1E1B16` | 与斜纹同色，13.69:1 |
| `#61776c` | 未使用 | — | — | 换肤前样式表里只被定义、没有实际使用的规则，仅记录在案 |

**给 V-B 的两条边界**：`--hw-line` 现在同时承担"分隔线"和"控件描边"，取值比换肤前深（3.14:1），灰度下比原薄荷绿更清晰；`--hw-hazard` 现在用在一处内联错误条上，V-B 若要把斜纹收窄到"阻断/越界确认"更严格的场景，需要给错误条换成别的不承载状态的呈现（例如仅墨色描边 + 文字前缀），并同步 0009 §6。

## 7. token 层接口事实（V-B / V-C 接手用）

`frontend/src/tokens.css` 的完整清单：

- **色板槽位（9 个字面量值）**：`--hw-base` `#ECE5D6`、`--hw-panel` `#F6F2E9`、`--hw-ink` `#1E1B16`、`--hw-line` `#8A7F6B`、`--hw-accent` `#D63A2C`、`--hw-accent-ink` `#A8432F`、`--hw-stripe-1..4` `#4C5A68` `#7A7F4E` `#D2A85E` `#A8432F`、`--hw-hazard-1/-2` `#1E1B16` `#ECE5D6`。
- **角色别名**：`--hw-surface`（面板面）、`--hw-surface-sunken`（嵌入面）、`--hw-text`、`--hw-text-muted`、`--hw-border`、`--hw-border-soft`、`--hw-focus-ring`、`--hw-text-on-solid`、`--hw-hazard`（斜纹 `repeating-linear-gradient`）。
- **字体族名**：`'Archivo Expanded'`（400–800 + `font-stretch: 125%`）、`'IBM Plex Sans'`（400/600）、`'IBM Plex Mono'`（400/500）、`'Sarasa Gothic SC'`（400/700）、`'Sarasa Mono SC'`（400/700）。栈变量：`--hw-font-display`、`--hw-font-sans`、`--hw-font-mono`、`--hw-font-mono-cjk`。
- **字阶**：`--hw-text-2xs..3xl`（11/12/13/14/16/17/24/36px）与 `--hw-text-title`（`clamp()`）、`--hw-leading-tight/normal/loose/roomy/loose-plus`、`--hw-tracking-label/none`、`--hw-weight-regular..black`。
- **栅格**：`--hw-space-0..9`（8px 倍数：0/8/16/24/32/40/48/72）、`--hw-gap-hair/tight/snug/roomy`（4/12/20/28，半格与贴边）、以及迁移期的 `--hw-size-*` / `--hw-margin-*` / `--hw-padding-*` / `--hw-max-*` 字面别名（V-B 可逐步换成栅格 token）。
- **结构**：`--hw-radius-panel/control/max`、`--hw-hairline`、`--hw-seam`、`--hw-stroke-stripe(-strong)`、`--hw-focus-ring-width`、`--hw-focus-offset`、`--hw-max-width`、`--hw-min-cell`、`--hw-breakpoint-compact/columns`。
- **检查脚本入口**：`npm run check:design`（`frontend/scripts/check-design-system.mjs`）；字体重新生成：`npm run fonts:build`（`frontend/scripts/build-fonts.mjs` + `font-manifest.mjs`）。新增任何色值都必须加进 `tokens.css`，否则 1a 会失败。

## 8. 已知限制与未达项

1. **浏览器内渲染未验证**（验收 4 只有静态证据）。本票按 ADR-0018 不做浏览器验收：没有启动 Docker 栈、没有跑 Playwright、没有截图。中英混排与纯中文两种样例需要由 V-B 在真实页面上确认（可用浏览器 devtools 的 "Rendered Fonts" 面板或 `document.fonts.check()`）。
2. **首屏耗时未测量**（验收 5 只有体积）。缺"换肤前 vs 换肤后"的首屏时间/字体加载时序对比。
3. **字符集覆盖有边界**：中文字体只含 1204 个字符（前端源码 + `docs/**/*.md` 里出现的汉字与中文标点）。遇到子集外的汉字时，`unicode-range` 会让它落到字体栈的下一项 `system-ui`：**不会**落到 Latin 等宽（因为中文字族之后的等宽项只出现在 `--hw-font-mono`，且 `system-ui` 本身不在等宽栈里），但会与同行的 Sarasa 混排。新增界面文案请重跑 `npm run fonts:build`，检查项 4a 会在漏字时失败。
4. **跨平台字体栈尾部依赖系统字体**：栈尾是 `system-ui` / `ui-monospace`，Windows / Linux / macOS 会给出不同的兜底字形。首版按 0009 §4 允许系统字体，但这意味着子集外字符与 `×`、`→` 这类符号在不同宿主上外观可能不同。
5. **`npm run fonts:build` 的中文下载依赖网络**：Sarasa 归档来自 GitHub release（`raw.githubusercontent.com` 在开发机上 Node 的 DNS 路径不通，脚本会退回 `pwsh Invoke-WebRequest`，两者都校验 sha256）。构建产物已提交，因此**正常构建与检查不需要网络**，只有重新生成 Chinese 子集时才需要。
6. **子集化产物的可复现性只验证到"同输入同来源"**：`subset-font` 通过 HarfBuzz 决定字形与表布局，两次构建的 woff2 未做过逐字节比对；归档 sha256 固定的是输入，不是输出。
7. **`style.css` 的间距没有全部贴到 8px 栅格**：为保持与换肤前一致的几何，迁移期保留了原取值的字面别名（`--hw-margin-*` 等）。0009 §5 的"间距按 8px 基准栅格"目前是"新加的间距一律用 `--hw-space-*`"，历史取值的收敛留给 V-B/V-C。
8. **未跑既有 Playwright 用例**（0009 §11 的第 10 条）。改动只涉及 CSS 与样式引入，`style.css` 的类名、选择器与结构全部保留，理论上不影响用例；但没有实际跑过，不作为已验证结论。

## 9. 待人工确认

- V-B 是否接受"错误条使用警示斜纹"（§6 末段）；若收紧，需要同步 0009 §6。
- 是否要把 `--hw-line` 的收紧同样应用到 `--hw-stripe-*`（它们只作身份元素，本票未做对比度要求）。
- 是否需要在 V-B 之前补一次"中文字形实际渲染"的浏览器抽查，还是并入 V-B 的灰度截图回归一起做。

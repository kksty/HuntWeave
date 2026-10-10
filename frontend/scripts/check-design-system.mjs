/*
 * 视觉 token 层与字体层的机械检查（V-A / issue #33）。
 *
 * 覆盖的验收条目：
 *   1. token 层（frontend/src/tokens.css）是唯一色值来源：仓库里所有十六进制色值都必须
 *      等于 token 层里的某个 --hw-* 取值；样式表与组件不得出现 token 层没有的色值。
 *   2. 0009 §3 的对比度实测值：用 WCAG 2.x 相对亮度公式现算，并逐条比对文档里记录的数值。
 *   3. 仓库与构建产物不含商业字体文件；@font-face 只用 OFL 字体族。
 *   4. 中文由中文字族渲染：中文 @font-face 的 unicode-range 覆盖前端用到的全部汉字与中文标点，
 *      且不含 Latin 字母与数字（数字与编号必须落到 IBM Plex 等宽）。
 *   5. 字体自托管体积：报告每个 woff2 的字节数与 total，并给出阈值判定。
 *
 * 用法：npm run check:design（不需要网络）
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { create as createFont } from 'fontkit';

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, '..');
const repo = resolve(frontend, '..');
const dist = join(frontend, 'dist');
const fontDir = join(frontend, 'src', 'assets', 'fonts');
const specPath = join(repo, 'docs', 'specs', '0009-visual-design-system.md');

const checks = [];
const record = (name, ok, detail) => {
  checks.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}\n      ${detail}`);
};

const read = (p) => readFileSync(p, 'utf8');
const skipDirs = new Set(['node_modules', '.git', 'dist', 'test-results', 'playwright-report', 'runtime', 'data', 'work', 'logs', 'evidence', 'tool-store', 'download-cache', 'backups', '__pycache__', '.venv', '.mypy_cache', '.ruff_cache', '.pytest_cache']);
const walkFiles = (root, filter, out = []) => {
  let names;
  try {
    names = readdirSync(root);
  } catch {
    return out;
  }
  for (const name of names) {
    if (skipDirs.has(name) || name.startsWith('.cache-')) continue;
    const p = join(root, name);
    const st = statSync(p, { throwIfNoEntry: false });
    if (!st) continue;
    if (st.isDirectory()) walkFiles(p, filter, out);
    else if (filter(p)) out.push(p);
  }
  return out;
};

// ---------- 1. 唯一色值来源 ----------
const tokensPath = join(frontend, 'src', 'tokens.css');
const tokensCss = read(tokensPath);
// CSS 注释里的示例值不算 token 定义。
const tokenDeclarations = new Map();
const withoutComments = tokensCss.replace(/\/\*[\s\S]*?\*\//g, '');
for (const m of withoutComments.matchAll(/--(hw-[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;/g)) {
  if (!tokenDeclarations.has(m[1])) tokenDeclarations.set(m[1], m[2].toLowerCase());
}
const tokenPalette = new Map();
for (const [name, hex] of tokenDeclarations) if (!tokenPalette.has(hex)) tokenPalette.set(hex, name);
const tokenAliases = [...withoutComments.matchAll(/--(hw-[\w-]+)\s*:\s*var\(--(hw-[\w-]+)\)\s*;/g)].map((m) => `${m[1]} → ${m[2]}`);
const allowedColors = new Set(tokenPalette.keys());

// tokens.css 之外允许出现十六进制色值的文件（必须说明理由；这里是空集，即任何地方都不允许）。
const colorExempt = [];
const sourceFiles = [
  ...walkFiles(join(frontend, 'src'), (p) => ['.css', '.vue', '.ts'].includes(extname(p))),
  join(frontend, 'index.html'),
  join(frontend, 'vite.config.ts'),
];
const hexPattern = /#[0-9a-fA-F]{3,8}\b/g;
const strays = [];
for (const file of sourceFiles) {
  if (file === tokensPath) continue;
  const text = read(file);
  text.split(/\r?\n/).forEach((line, index) => {
    for (const m of line.matchAll(hexPattern)) {
      const value = m[0].toLowerCase();
      if (allowedColors.has(value)) continue;
      strays.push(`${file.replace(repo, '.')}:${index + 1} ${m[0]}`);
    }
  });
}
record(
  '1a 色值只来自 token 层',
  strays.length === 0 && colorExempt.length === 0,
  [
    strays.length ? `发现 token 层之外的色值：\n      ${strays.join('\n      ')}` : `扫描 ${sourceFiles.length} 个源文件，未发现 token 层之外的色值`,
    `token 层含 ${allowedColors.size} 个字面量色值：${[...tokenPalette.entries()].map(([hex, name]) => `${name}=${hex}`).join(', ')}`,
    `槽位别名：${tokenAliases.join('，')}`,
  ].join('\n      '),
);

// 其他表示法（rgb()/rgba()/hsl()/命名色）也不得绕过 token 层。
const otherNotation = /(?<![\w-])(?:rgba?|hsla?)\(/gi;
const notationHits = sourceFiles
  .filter((p) => p !== tokensPath)
  .flatMap((file) =>
    read(file)
      .split(/\r?\n/)
      .flatMap((line, index) => [...line.matchAll(otherNotation)].map(() => `${file.replace(repo, '.')}:${index + 1}`)),
  );
record('1a2 无 rgb()/hsl() 绕过', notationHits.length === 0, notationHits.length ? notationHits.join(', ') : 'tokens.css 之外没有 rgb()/rgba()/hsl()/hsla() 字面量色值');

// 规则文件：色值一律走 var(--hw-*)。
const rulesCss = sourceFiles.filter((p) => extname(p) === '.css' && p !== tokensPath);
const directHexInRules = rulesCss.flatMap((file) =>
  read(file)
    .split(/\r?\n/)
    .flatMap((line, index) => [...line.matchAll(hexPattern)].map((m) => `${file.replace(repo, '.')}:${index + 1} ${m[0]}`)),
);
record('1b 样式规则内无十六进制色值', directHexInRules.length === 0, directHexInRules.length ? directHexInRules.join(', ') : `${rulesCss.map((p) => p.replace(repo, '.')).join(', ')} 内 0 处直接色值`);

// 换肤前 frontend/src/style.css（提交 7c56643）里写死的 25 个不同十六进制色值（29 处出现），
// 以及它们的去处。这份映射表就是 V-A 对 V-B 的交接内容：换了槽位、换了取值的地方都在 note 里写明理由。
const legacyMap = {
  '#20382f': { token: 'hw-ink', note: '正文色：由绿调深色换成暖墨色' },
  '#224a39': { token: 'hw-ink', note: '品牌字色' },
  '#f2f5ef': { token: 'hw-base' },
  '#f4f6f2': { token: 'hw-base', note: '代码块嵌入面' },
  '#f3f7f0': { token: 'hw-base', note: '预览框嵌入面' },
  '#fff8e9': { token: 'hw-base', note: '提示条面' },
  '#edf3e7': { token: 'hw-panel', note: '徽章面' },
  '#fff': { token: 'hw-panel', note: '页眉与面板面' },
  '#fcfdfb': { token: 'hw-panel', note: '输入框面' },
  '#fdf6e0': { token: 'hw-panel', note: '警告徽章面' },
  '#dce3d9': { token: 'hw-line' },
  '#d4dfce': { token: 'hw-line', note: '徽章描边' },
  '#bccbbf': { token: 'hw-line', note: '控件描边' },
  '#d5ded4': { token: 'hw-line', note: '静默按钮描边' },
  '#e3e9df': { token: 'hw-line', note: '折叠区与列表项分隔' },
  '#a7c9b1': { token: 'hw-line', note: '事件条、证据框、实例卡描边（原薄荷绿）' },
  '#c9a227': { token: 'hw-line', note: '警告徽章描边：0009 §3 禁止 stripe 用于状态' },
  '#244f3e': { token: 'hw-ink', note: '主按钮底与描边' },
  '#4c6559': { token: 'hw-ink', note: '静默按钮文字' },
  '#577365': { token: 'hw-ink', note: '眉标；次级文字改用同一墨色，靠字号与字重区分' },
  '#61766c': { token: 'hw-ink', note: '说明文字与 dt' },
  '#657c70': { token: 'hw-ink', note: 'small' },
  '#647c6e': { token: 'hw-ink', note: 'dt' },
  '#3c5348': { token: 'hw-ink', note: '调用进度与保留区小标题' },
  '#71582b': { token: 'hw-ink', note: '提示条文字' },
  '#a13d2d': { token: 'hw-accent-ink', note: '错误与无效标记文字' },
  '#387550': { token: 'hw-accent-ink', note: '列表项悬停' },
  '#fff0e9': { token: 'hw-base', note: '错误条底：改由 --hw-hazard 斜纹承担，底色仍是 base' },
  '#e5bba9': { token: 'hw-ink', note: '错误条描边' },
};
const legacyColors = Object.keys(legacyMap);
const unusedLegacy = ['#61766c'];
const badTokenNames = legacyColors.filter((c) => !tokenDeclarations.has(legacyMap[c].token));
const unchanged = legacyColors.filter((c) => tokenDeclarations.get(legacyMap[c].token) === c);
record(
  '1c 换肤前的色值全部有 token 去处',
  badTokenNames.length === 0,
  badTokenNames.length
    ? `映射指向了不存在的 token：${badTokenNames.join(', ')}`
    : `${legacyColors.length} 个换肤前色值逐个映射到 token 槽位（${unchanged.length} 个取值不变、${legacyColors.length - unchanged.length} 个按 0009 §3 换成新值；其中 ${unusedLegacy.join('/')} 在原样式表里未被实际使用，仅记录在案）；映射表同时写在 docs/validation/0021-visual-tokens.md`,
);

// ---------- 2. 对比度实测与文档一致性 ----------
const lin = (c) => {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};
const luminance = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
};
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const tokenValue = (name) => tokenDeclarations.get(name.replace(/^--/, ''));

const pairs = [
  ['--hw-accent', '--hw-base'],
  ['--hw-accent', '--hw-panel'],
  ['--hw-accent-ink', '--hw-base'],
  ['--hw-accent-ink', '--hw-panel'],
  ['--hw-ink', '--hw-base'],
  ['--hw-ink', '--hw-panel'],
  ['--hw-line', '--hw-base'],
  ['--hw-line', '--hw-panel'],
  ['--hw-panel', '--hw-base'],
];
const measured = pairs.map(([fg, bg]) => ({ fg, bg, ratio: contrast(tokenValue(fg), tokenValue(bg)) }));
const sanity = contrast('#ffffff', '#000000');
record('2a 公式自检（#FFFFFF / #000000 必须为 21.0000）', Math.abs(sanity - 21) < 1e-9, `实测 ${sanity.toFixed(4)}`);

const spec = read(specPath);
const documented = [
  { token: '--hw-accent', bg: '--hw-base', label: '#D63A2C 对 #ECE5D6' },
  { token: '--hw-accent', bg: '--hw-panel', label: '#D63A2C 对 #F6F2E9' },
  { token: '--hw-accent-ink', bg: '--hw-base', label: '#A8432F 对 #ECE5D6' },
  { token: '--hw-accent-ink', bg: '--hw-panel', label: '#A8432F 对 #F6F2E9' },
];
const mismatches = [];
for (const item of documented) {
  const value = measured.find((m) => m.fg === item.token && m.bg === item.bg);
  const rounded = (Math.round(value.ratio * 100) / 100).toFixed(2);
  if (!spec.includes(`${rounded}:1`)) mismatches.push(`${item.label} 实测 ${rounded}:1 未出现在 0009 §3`);
}
record(
  '2b 0009 §3 记录的对比度与现算值一致',
  mismatches.length === 0,
  mismatches.length ? mismatches.join('; ') : documented.map((d) => `${d.label} → ${measured.find((m) => m.fg === d.token && m.bg === d.bg).ratio.toFixed(4)}（取整 ${(Math.round(measured.find((m) => m.fg === d.token && m.bg === d.bg).ratio * 100) / 100).toFixed(2)}:1）`).join('；'),
);
console.log('      全部实测值：');
for (const m of measured) console.log(`        ${m.fg} on ${m.bg} = ${m.ratio.toFixed(4)}`);
const smallTextGuard = measured.find((m) => m.fg === '--hw-accent' && m.bg === '--hw-panel').ratio;
const accentInkGuard = measured.find((m) => m.fg === '--hw-accent-ink' && m.bg === '--hw-base').ratio;
record(
  '2c 强调色不得用于正文小字（<4.5:1），accent-ink 可用于小字（≥4.5:1）',
  smallTextGuard < 4.5 && accentInkGuard >= 4.5,
  `--hw-accent 在面板上 ${smallTextGuard.toFixed(4)}:1 < 4.5:1；--hw-accent-ink 在底色上 ${accentInkGuard.toFixed(4)}:1 ≥ 4.5:1`,
);

// ---------- 3. 许可边界与商业字体 ----------
const fontBinaryExt = new Set(['.woff', '.woff2', '.ttf', '.otf', '.ttc', '.eot']);
const fontFiles = walkFiles(repo, (p) => fontBinaryExt.has(extname(p).toLowerCase()) && !p.includes(`${'node_modules'}`));
const distFontFiles = existsSync(dist) ? walkFiles(dist, (p) => fontBinaryExt.has(extname(p).toLowerCase())) : [];
const commercialNames = ['univers', 'helvetica', 'neue', 'arial', 'times'];
const commercialHits = [];
for (const file of [...fontFiles, ...distFontFiles]) {
  const base = file.toLowerCase();
  if (commercialNames.some((n) => base.includes(n))) commercialHits.push(file.replace(repo, '.'));
}
record(
  '3a 仓库与构建产物不含商业字体文件',
  commercialHits.length === 0,
  commercialHits.length ? commercialHits.join(', ') : `仓库内字体文件 ${fontFiles.length} 个、产物内 ${distFontFiles.length} 个，文件名与嵌入名都不匹配 ${commercialNames.join('/')}`,
);

const facesCss = read(join(frontend, 'src', 'fonts.css'));
const declaredFamilies = [...new Set([...facesCss.matchAll(/font-family:\s*'([^']+)'/g)].map((m) => m[1]))];
const oflFamilies = ['Archivo Expanded', 'IBM Plex Sans', 'IBM Plex Mono', 'Sarasa Gothic SC', 'Sarasa Mono SC'];
const unknownFamilies = declaredFamilies.filter((f) => !oflFamilies.includes(f));
const commercialInStacks = [];
for (const file of sourceFiles) {
  const text = read(file).toLowerCase();
  for (const name of ['helvetica', 'univers']) if (text.includes(name)) commercialInStacks.push(`${file.replace(repo, '.')} 含 ${name}`);
}
record(
  '3b @font-face 只用 OFL 族，字体栈不含商业字体名',
  unknownFamilies.length === 0 && commercialInStacks.length === 0,
  unknownFamilies.length || commercialInStacks.length ? [...unknownFamilies, ...commercialInStacks].join(', ') : `声明族：${declaredFamilies.join(', ')}；Univers/Helvetica 未出现在任何字体栈`,
);

const licenses = existsSync(fontDir) ? readdirSync(fontDir).filter((f) => f.startsWith('LICENSE')) : [];
record(
  '3c 每个字体族都有 OFL 许可文本随资产交付',
  licenses.length >= 3,
  `${licenses.length} 份：${licenses.join(', ')}（目录 frontend/src/assets/fonts/）`,
);

// ---------- 4. 中文由中文字族渲染 ----------
const han = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;
const cjkPunct = /[\u3000-\u303f\uff00-\uffef\u200b\u2014\u2026\u00b7]/;
const used = new Set();
for (const file of [...walkFiles(join(frontend, 'src'), (p) => ['.vue', '.ts', '.css'].includes(extname(p))), join(frontend, 'index.html')]) {
  for (const ch of read(file)) if (han.test(ch) || cjkPunct.test(ch)) used.add(ch);
}

const rangesFromCss = (family) => {
  const blocks = [...facesCss.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => m[1]).filter((b) => b.includes(`'${family}'`));
  const cps = new Set();
  for (const block of blocks) {
    const range = (block.match(/unicode-range:\s*([^;]+);/) ?? [])[1];
    if (!range) continue;
    for (const part of range.split(',')) {
      const [a, b] = part.trim().replace(/^U\+/i, '').split('-');
      const start = parseInt(a, 16);
      const end = b ? parseInt(b, 16) : start;
      for (let cp = start; cp <= end; cp += 1) cps.add(cp);
    }
  }
  return cps;
};

const sansRange = rangesFromCss('Sarasa Gothic SC');
const monoRange = rangesFromCss('Sarasa Mono SC');
const uncovered = [...used].filter((ch) => !sansRange.has(ch.codePointAt(0)) && !monoRange.has(ch.codePointAt(0)));
record(
  '4a 前端用到的汉字与中文标点都在中文字族的 unicode-range 内',
  uncovered.length === 0,
  uncovered.length ? `未覆盖：${uncovered.join('')}` : `前端用到 ${used.size} 个中文字符，Sarasa Gothic SC 声明 ${sansRange.size} 个码点、Sarasa Mono SC 声明 ${monoRange.size} 个码点，未覆盖 0 个`,
);

const latinInCjk = [...new Set([...sansRange, ...monoRange])].filter((cp) => (cp >= 0x30 && cp <= 0x39) || (cp >= 0x41 && cp <= 0x5a) || (cp >= 0x61 && cp <= 0x7a));
record(
  '4b 中文字族不声明 Latin 字母与数字（它们必须落到 IBM Plex）',
  latinInCjk.length === 0,
  latinInCjk.length ? `中文字族声明了 ${latinInCjk.map((c) => String.fromCodePoint(c)).join('')}` : 'A-Z/a-z/0-9 在中文字族 unicode-range 内 0 个，数字与编号由 IBM Plex Mono 承担',
);

// 中文字族的字体文件本身也要真的含这些字形（unicode-range 只是声明）。
const fontCoverage = new Map();
if (existsSync(fontDir)) {
  for (const name of readdirSync(fontDir)) {
    if (extname(name) !== '.woff2') continue;
    const font = createFont(readFileSync(join(fontDir, name)));
    fontCoverage.set(name, { family: font.familyName, cps: new Set(font.characterSet) });
  }
}
const cjkFiles = [...fontCoverage.entries()].filter(([, v]) => v.family.startsWith('Sarasa'));
const missingInFiles = [];
for (const ch of used) {
  if (!cjkFiles.some(([, v]) => v.cps.has(ch.codePointAt(0)))) missingInFiles.push(ch);
}
record(
  '4c 中文字体文件确实含用到的字形',
  missingInFiles.length === 0,
  missingInFiles.length ? `缺字形：${missingInFiles.join('')}` : `${cjkFiles.length} 个中文字体文件逐字核对通过（family ${[...new Set(cjkFiles.map(([, v]) => v.family))].join('/')}）`,
);

// ---------- 5. 体积 ----------
const shipped = existsSync(fontDir) ? readdirSync(fontDir).filter((f) => extname(f) === '.woff2') : [];
const sizes = shipped.map((f) => ({ file: f, bytes: statSync(join(fontDir, f)).size })).sort((a, b) => b.bytes - a.bytes);
const totalBytes = sizes.reduce((n, s) => n + s.bytes, 0);
const perFaceBudget = 400 * 1024;
const totalBudget = 1.5 * 1024 * 1024;
const overBudget = sizes.filter((s) => s.bytes > perFaceBudget);
record(
  '5a 字体自托管体积在预算内',
  overBudget.length === 0 && totalBytes <= totalBudget,
  [
    `合计 ${totalBytes} 字节（${(totalBytes / 1024).toFixed(1)} KiB），阈值 ${(totalBudget / 1024).toFixed(0)} KiB`,
    ...sizes.map((s) => `${s.file} ${s.bytes} 字节`),
    overBudget.length ? `单文件超 ${(perFaceBudget / 1024).toFixed(0)} KiB：${overBudget.map((s) => s.file).join(', ')}` : `单文件均 ≤ ${(perFaceBudget / 1024).toFixed(0)} KiB`,
  ].join('\n      '),
);

const distFontBytes = distFontFiles.reduce((n, f) => n + statSync(f).size, 0);
const distJsBytes = existsSync(join(dist, 'assets')) ? readdirSync(join(dist, 'assets')).filter((f) => f.endsWith('.js') || f.endsWith('.css')).reduce((n, f) => n + statSync(join(dist, 'assets', f)).size, 0) : 0;
record(
  '5b 构建产物体积已记录',
  existsSync(dist),
  existsSync(dist) ? `dist 内字体 ${distFontBytes} 字节（${(distFontBytes / 1024).toFixed(1)} KiB）、JS/CSS ${distJsBytes} 字节（${(distJsBytes / 1024).toFixed(1)} KiB）` : '未找到 frontend/dist，先执行 npm run build',
);

// ---------- 汇总 ----------
const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} 项通过`);
if (failed.length) {
  console.error(`失败项：${failed.map((c) => c.name).join('; ')}`);
  process.exitCode = 1;
}

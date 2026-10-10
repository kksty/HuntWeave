/*
 * 生成自托管字体资产（V-A / issue #33）。
 *
 * 输入：node_modules 里 @fontsource 的 Latin 文件 + 从上游 release 下载并缓存的 Sarasa 完整 TTF。
 * 输出：frontend/src/assets/fonts/ 下的 woff2 与许可文本，以及 frontend/src/fonts.css。
 *
 * 关键取舍：Sarasa Gothic SC / Sarasa Mono SC 完整 TTF 各 23–25MB，直接交付会拖垮首屏，
 * 因此按「前端源码 + 产品文档里真实出现过的汉字与中文标点」子集化（字符集由 extractCharset()
 * 唯一决定，可复现）。Latin 字母与数字不进入中文子集，只由 IBM Plex / Archivo 承担，
 * 这样中文永远落在中文字族，不会落到 Latin 等宽 fallback（0009 §4）。
 *
 * 用法：npm run fonts:build（中文需要网络；已缓存的归档不会重复下载）
 * 校验：npm run check:design 会重新读取产物证明覆盖与 unicode-range
 */

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { create as createFont } from 'fontkit';
import SevenZip from '7z-wasm';
import subsetFont from 'subset-font';
import { cacheDir, latinFaces, sarasaFaces, sources } from './font-manifest.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, '..');
const cache = join(frontend, cacheDir);
const outDir = join(frontend, 'src', 'assets', 'fonts');

/** 字符集来源：这些文件里的汉字与中文标点决定中文子集的覆盖范围。 */
const charsetRoots = [
  { path: join(frontend, 'src'), exts: ['.ts', '.vue', '.css'] },
  { path: join(frontend, 'index.html'), exts: ['.html'], file: true },
  { path: resolve(frontend, '..', 'docs'), exts: ['.md'] },
];

const han = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;
const cjkPunct = /[\u3000-\u303f\uff00-\uffef\u2000-\u206f\u00b7\u2014\u2026]/;

const log = (...args) => console.log('[fonts]', ...args);

function walk(root, exts, visit) {
  let names;
  try {
    names = readdirSync(root);
  } catch {
    return;
  }
  for (const name of names) {
    if (name.startsWith('.') || name === 'node_modules' || name === 'dist' || name === 'test-results') continue;
    const p = join(root, name);
    const st = statSync(p, { throwIfNoEntry: false });
    if (!st) continue;
    if (st.isDirectory()) walk(p, exts, visit);
    else if (exts.includes(extname(p))) visit(p);
  }
}

/** 收集需要交付给中文子集的字符：真实出现过的汉字 + 中文标点。 */
export function extractCharset() {
  const chars = new Set();
  const visit = (file) => {
    const text = readFileSync(file, 'utf8');
    for (const ch of text) if (han.test(ch) || cjkPunct.test(ch)) chars.add(ch);
  };
  for (const root of charsetRoots) {
    if (root.file) {
      if (existsSync(root.path)) visit(root.path);
    } else {
      walk(root.path, root.exts, visit);
    }
  }
  return [...chars].sort((a, b) => a.codePointAt(0) - b.codePointAt(0));
}

async function download(url, dest, sha256) {
  if (existsSync(dest)) {
    log('cached', dest);
  } else {
    mkdirSync(dirname(dest), { recursive: true });
    let ok = false;
    try {
      const res = await fetch(url, { redirect: 'follow' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
      ok = true;
    } catch (error) {
      // 有些本机（含本票的开发机）Node 的 DNS/TLS 走不通 raw.githubusercontent.com，
      // 但系统代理下的 PowerShell 可以；这里退一步用它，仍然校验 sha256。
      log(`node fetch failed (${error.message})，改用 pwsh Invoke-WebRequest`);
      const ps = `$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri '${url}' -OutFile '${dest}' -UseBasicParsing`;
      try {
        execFileSync('pwsh', ['-NoProfile', '-Command', ps], { stdio: ['ignore', 'ignore', 'pipe'] });
        ok = existsSync(dest);
      } catch (psError) {
        throw new Error(`无法取得 ${url}：node fetch 与 pwsh 下载都失败（${psError.message}）`);
      }
    }
    if (!ok) throw new Error(`无法取得 ${url}`);
    log('downloaded', dest);
  }
  const buf = readFileSync(dest);
  const actual = createHash('sha256').update(buf).digest('hex');
  if (sha256 && actual !== sha256) throw new Error(`sha256 mismatch for ${dest}: ${actual} != ${sha256}`);
  return { buffer: buf, sha256: actual, bytes: buf.length };
}

/** 从 7z 归档里取出指定成员（7z-wasm 只在自己的虚拟文件系统里写文件）。 */
async function extractFrom7z(archiveBuffer, members) {
  const sevenZip = await SevenZip({ print: () => {}, printErr: () => {} });
  sevenZip.FS.writeFile('archive.7z', new Uint8Array(archiveBuffer));
  const code = sevenZip.callMain(['x', 'archive.7z', '-y']);
  if (code !== 0) throw new Error(`7z failed with exit ${code}`);
  const out = {};
  for (const [member, dest] of Object.entries(members)) out[dest] = Buffer.from(sevenZip.FS.readFile(member));
  return out;
}

// 只保留真正有字形的码点，并剔除 gid 0（.notdef）：fontkit 的 characterSet 是码点数组，
// 对若干字体把 U+0000/U+000D 之类的已废弃 cmap 表项也算进来，写进 unicode-range 会误导浏览器。
const cmapOf = (buffer) => {
  const font = createFont(buffer);
  return [...font.characterSet].filter((code) => code >= 0x20 && code <= 0x10ffff && font.glyphForCodePoint(code).id !== 0).sort((a, b) => a - b);
};

/** 把码点合并成紧凑的 unicode-range 片段（相邻或只差一个码点的合并）。 */
function toUnicodeRange(codepoints, gap = 1) {
  const runs = [];
  for (const cp of codepoints) {
    const last = runs[runs.length - 1];
    if (last && cp - last[1] <= gap + 1) last[1] = cp;
    else runs.push([cp, cp]);
  }
  return runs.map(([a, b]) => (a === b ? `U+${a.toString(16).toUpperCase()}` : `U+${a.toString(16).toUpperCase()}-${b.toString(16).toUpperCase()}`)).join(',');
}

async function main() {
  mkdirSync(cache, { recursive: true });
  mkdirSync(outDir, { recursive: true });

  const charset = extractCharset();
  const subsetText = charset.join('');
  log(`charset: ${charset.length} 字符（汉字与中文标点，来自 frontend/src、frontend/index.html 与 docs/）`);

  const faces = [];

  // ---- 1. Latin 字面：直接交付 @fontsource 的 woff2 与许可文本 ----
  for (const face of latinFaces) {
    const src = join(frontend, 'node_modules', face.pkg, face.source);
    if (!existsSync(src)) throw new Error(`missing ${src}；先执行 npm ci`);
    const buffer = readFileSync(src);
    writeFileSync(join(outDir, face.out), buffer);
    if (face.license) {
      log(`${face.out}: ${buffer.length} bytes（许可文本）`);
      continue;
    }
    const font = createFont(buffer);
    const cps = cmapOf(buffer);
    faces.push({
      family: face.family,
      out: face.out,
      weight: face.weight,
      stretch: face.stretch ?? null,
      bytes: buffer.length,
      axes: font.variationAxes ? Object.keys(font.variationAxes) : [],
      unicodeRange: toUnicodeRange(cps),
      codepoints: cps.length,
    });
    log(`${face.out}: ${buffer.length} bytes，family=${font.familyName}，axes=${font.variationAxes ? Object.keys(font.variationAxes).join('+') : '—'}`);
  }

  // ---- 2. 上游原始文件（Sarasa TTF 包与许可文本）----
  const raw = {};
  for (const source of sources) {
    const dest = join(cache, source.file);
    const got = await download(source.url, dest, source.sha256);
    log(`source ${source.id}: ${got.bytes} bytes sha256=${got.sha256}`);
    if (source.license) {
      writeFileSync(join(outDir, source.file), got.buffer);
      continue;
    }
    const members = await extractFrom7z(got.buffer, source.members);
    for (const [name, buffer] of Object.entries(members)) {
      raw[name] = buffer;
      log(`extracted ${name}: ${buffer.length} bytes`);
    }
  }

  // ---- 3. 中文：Sarasa Gothic SC / Sarasa Mono SC 子集化 ----
  for (const face of sarasaFaces) {
    const src = raw[face.src];
    if (!src) throw new Error(`missing source ${face.src}`);
    const out = await subsetFont(src, subsetText, { targetFormat: 'woff2' });
    writeFileSync(join(outDir, face.out), out);
    const font = createFont(out);
    if (font.familyName !== face.family) throw new Error(`family mismatch: ${font.familyName} != ${face.family}`);
    const cps = cmapOf(out);
    // 0009 §4：中文子集不得吞掉 Latin 字母、数字与编号——它们必须由 IBM Plex 承担。
    const latinLeak = cps.filter((cp) => (cp >= 0x30 && cp <= 0x39) || (cp >= 0x41 && cp <= 0x5a) || (cp >= 0x61 && cp <= 0x7a));
    if (latinLeak.length) throw new Error(`${face.out} 的 cmap 含 Latin 字母或数字：${latinLeak.map((c) => String.fromCodePoint(c)).join('')}`);
    faces.push({
      family: face.family,
      out: face.out,
      weight: face.weight,
      stretch: null,
      bytes: out.length,
      axes: [],
      unicodeRange: toUnicodeRange(cps),
      codepoints: cps.length,
      sourceBytes: src.length,
    });
    log(`${face.out}: ${src.length} -> ${out.length} bytes，family=${font.familyName}`);
  }

  // ---- 4. fonts.css ----
  const css = [
    '/*',
    ' * 自托管 @font-face（V-A / issue #33）。由 npm run fonts:build 生成，不要手改。',
    ' * 族名与 frontend/src/tokens.css 的 --hw-font-* 一一对应。',
    ' * 中文只会落在 Sarasa Gothic SC / Sarasa Mono SC：这两个 @font-face 的 unicode-range',
    ' * 只覆盖汉字与中文标点，Latin 字母、数字与编号由 IBM Plex 承担。',
    ' * 全部字体为 OFL-1.1，许可文本见同目录 LICENSE-*.txt。',
    ' */',
    '',
  ];
  for (const family of ['Archivo Expanded', 'IBM Plex Sans', 'IBM Plex Mono', 'Sarasa Gothic SC', 'Sarasa Mono SC']) {
    for (const face of faces.filter((f) => f.family === family)) {
      css.push('@font-face {');
      css.push(`  font-family: '${family}';`);
      css.push('  font-style: normal;');
      css.push(`  font-weight: ${face.weight};`);
      if (face.stretch) css.push(`  font-stretch: ${face.stretch};`);
      css.push('  font-display: swap;');
      css.push(`  src: url('./assets/fonts/${face.out}') format('woff2');`);
      css.push(`  unicode-range: ${face.unicodeRange};`);
      css.push('}');
      css.push('');
    }
  }
  writeFileSync(join(frontend, 'src', 'fonts.css'), css.join('\n'));

  const total = faces.reduce((n, f) => n + f.bytes, 0);
  writeFileSync(
    join(outDir, 'MANIFEST.json'),
    `${JSON.stringify({ generatedBy: 'frontend/scripts/build-fonts.mjs', charsetSize: charset.length, totalBytes: total, faces }, null, 2)}\n`,
  );
  log(`done: ${faces.length} 个 @font-face，字体文件合计 ${total} bytes`);
}

main().catch((error) => {
  console.error('[fonts] failed:', error.message);
  process.exitCode = 1;
});

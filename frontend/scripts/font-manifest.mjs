/*
 * 字体来源清单（V-A / issue #33）。
 *
 * 只列 OFL 字体，分两类来源：
 *  - Latin 字面（Archivo、IBM Plex Sans、IBM Plex Mono）：取自 @fontsource 的 npm 包，
 *    它们已经在 node_modules 里，构建时不需要网络，也不重新子集化。
 *    Archivo 用可变字体（wdth 62–125 轴），加宽实例由 CSS 的 font-stretch: 125% 取得。
 *  - 中文（Sarasa Gothic SC / Sarasa Mono SC）：上游只发布 20MB+ 的完整 TTF，
 *    构建时从 GitHub release 下载（sha256 固定）并用 subset-font 子集化。
 *
 * 原始文件下载进 frontend/node_modules/.cache/huntweave-fonts/（被忽略），
 * 子集化后的 woff2 与许可文本提交进 frontend/src/assets/fonts/。
 * 仓库与构建产物都不得包含商业字体文件；Univers Extended / Helvetica 只允许出现在字体栈尾部。
 */

export const cacheDir = 'node_modules/.cache/huntweave-fonts';

/** 需要下载并缓存的原始文件（只有中文需要网络）。 */
export const sources = [
  {
    id: 'sarasa-gothic-sc',
    url: 'https://github.com/be5invis/Sarasa-Gothic/releases/download/v1.0.42/SarasaGothicSC-TTF-1.0.42.7z',
    file: 'SarasaGothicSC-TTF-1.0.42.7z',
    sha256: 'ee726608b04ec05f083e9877cad94c50d43f400afc211b944c3df99250d3a48d',
    note: 'Sarasa Gothic SC 1.0.42 TTF 包（62,867,113 字节），OFL-1.1，be5invis/Sarasa-Gothic release',
    members: {
      'SarasaGothicSC-Regular.ttf': 'SarasaGothicSC-Regular.ttf',
      'SarasaGothicSC-Bold.ttf': 'SarasaGothicSC-Bold.ttf',
    },
  },
  {
    id: 'sarasa-mono-sc',
    url: 'https://github.com/be5invis/Sarasa-Gothic/releases/download/v1.0.42/SarasaMonoSC-TTF-1.0.42.7z',
    file: 'SarasaMonoSC-TTF-1.0.42.7z',
    sha256: 'aa2150e99eb38c5f9d3a00fe58e3f90a9d89495c795a8ac80934d1c3e6c377ee',
    note: 'Sarasa Mono SC 1.0.42 TTF 包（65,885,338 字节），OFL-1.1，be5invis/Sarasa-Gothic release',
    members: {
      'SarasaMonoSC-Regular.ttf': 'SarasaMonoSC-Regular.ttf',
      'SarasaMonoSC-Bold.ttf': 'SarasaMonoSC-Bold.ttf',
    },
  },
  {
    id: 'sarasa-license',
    url: 'https://raw.githubusercontent.com/be5invis/Sarasa-Gothic/master/LICENSE',
    file: 'LICENSE-Sarasa-Gothic.txt',
    sha256: '32c932e0dbae4f6e6386964bbc2d04178707665a05ca65cf636241af13d50a53',
    note: 'Sarasa Gothic 的 OFL-1.1 许可文本（4,702 字节）',
    license: true,
  },
];

/** 交付的字面：Latin 直接复制 node_modules 里 @fontsource 的文件。 */
export const fontsourcePackages = [
  { package: '@fontsource-variable/archivo', version: '5.3.0' },
  { package: '@fontsource/ibm-plex-sans', version: '5.3.0' },
  { package: '@fontsource/ibm-plex-mono', version: '5.3.0' },
];

export const latinFaces = [
  {
    family: 'Archivo Expanded',
    pkg: '@fontsource-variable/archivo',
    source: 'files/archivo-latin-wdth-normal.woff2',
    out: 'archivo-latin-wdth-var.woff2',
    weight: '400 800',
    stretch: '125%',
    note: 'Archivo 可变字体（wght 100–900、wdth 62–125），加宽实例由 font-stretch: 125% 取得',
  },
  { family: 'IBM Plex Sans', pkg: '@fontsource/ibm-plex-sans', source: 'files/ibm-plex-sans-latin-400-normal.woff2', out: 'ibm-plex-sans-latin-400.woff2', weight: '400', stretch: null },
  { family: 'IBM Plex Sans', pkg: '@fontsource/ibm-plex-sans', source: 'files/ibm-plex-sans-latin-600-normal.woff2', out: 'ibm-plex-sans-latin-600.woff2', weight: '600', stretch: null },
  { family: 'IBM Plex Mono', pkg: '@fontsource/ibm-plex-mono', source: 'files/ibm-plex-mono-latin-400-normal.woff2', out: 'ibm-plex-mono-latin-400.woff2', weight: '400', stretch: null },
  { family: 'IBM Plex Mono', pkg: '@fontsource/ibm-plex-mono', source: 'files/ibm-plex-mono-latin-500-normal.woff2', out: 'ibm-plex-mono-latin-500.woff2', weight: '500', stretch: null },
  {
    family: 'IBM Plex Sans',
    pkg: '@fontsource/ibm-plex-sans',
    source: 'LICENSE',
    out: 'LICENSE-IBM-Plex.txt',
    license: true,
  },
  {
    family: 'Archivo Expanded',
    pkg: '@fontsource-variable/archivo',
    source: 'LICENSE',
    out: 'LICENSE-Archivo.txt',
    license: true,
  },
];

/** 需要从完整 TTF 子集化的中文字面。 */
export const sarasaFaces = [
  { src: 'SarasaGothicSC-Regular.ttf', out: 'sarasa-gothic-sc-subset-400.woff2', family: 'Sarasa Gothic SC', weight: '400' },
  { src: 'SarasaGothicSC-Bold.ttf', out: 'sarasa-gothic-sc-subset-700.woff2', family: 'Sarasa Gothic SC', weight: '700' },
  { src: 'SarasaMonoSC-Regular.ttf', out: 'sarasa-mono-sc-subset-400.woff2', family: 'Sarasa Mono SC', weight: '400' },
  { src: 'SarasaMonoSC-Bold.ttf', out: 'sarasa-mono-sc-subset-700.woff2', family: 'Sarasa Mono SC', weight: '700' },
];

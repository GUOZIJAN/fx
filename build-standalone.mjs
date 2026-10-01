// 把 dist/ 打包成单文件版 HTML：内联 CSS / JS / 头像，去掉 Toy SDK 与 Google Fonts。
// 运行：node build-standalone.mjs  →  生成 releases/fx-standalone.html（源文件不做任何修改）
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = dirname(fileURLToPath(import.meta.url));
const dist = join(root, 'dist');
const read = name => readFile(join(dist, name), 'utf8');

const [html, css, js, avatarBase64] = await Promise.all([
  read('index.html'),
  read('styles.css'),
  read('app.js'),
  readFile(join(dist, 'assets', 'kurumi-avatar.jpg')).then(buf => buf.toString('base64')),
]);

const avatarUri = `data:image/jpeg;base64,${avatarBase64}`;

// Google Fonts 国内手机加载慢/失败，单文件版直接去掉，回落到 'Microsoft YaHei', sans-serif
// 注意 @import 的 URL 里含分号（wght@400;500），必须按整行删，不能按分号截断
const cssInline = css.replace(/^@import[^\n]*\n/m, '');
const jsInline = js.replaceAll(
  `'./assets/kurumi-avatar.jpg'`,
  () => `'${avatarUri}'`,
);

const out = html
  // 去掉 Toy SDK：代码里所有 window.toy 调用都有判空守卫，不加载 SDK 即自动降级为纯浏览器行为
  .replace(/<script src="\/\/s1\.hdslb\.com[^"]*"><\/script>\s*/i, '')
  .replace(/<link rel="stylesheet" href="\.\/styles\.css[^"]*">/i, () => `<style>\n${cssInline}\n</style>`)
  // defer 对内联脚本无效，改为放到 </body> 前，时序等价
  .replace(/<script defer src="\.\/app\.js[^"]*"><\/script>\s*/i, '')
  .replace(/src="\.\/assets\/kurumi-avatar\.jpg"/, () => `src="${avatarUri}"`)
  .replace('</body>', () => `<script>\n${jsInline}\n</script>\n</body>`)
  .replace('<meta charset="UTF-8">', '<meta charset="UTF-8">\n  <!-- 单文件版：由 build-standalone.mjs 生成，请勿手改 -->');

const target = join(root, 'releases', 'fx-standalone.html');
await writeFile(target, out, 'utf8');
console.log(`OK -> ${target} (${(Buffer.byteLength(out, 'utf8') / 1024).toFixed(0)} KB)`);

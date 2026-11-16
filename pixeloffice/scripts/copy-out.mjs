// 把 next build 的靜態輸出（out/）換到 ../web/pixel（先清空舊的）。
import { cpSync, existsSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, '..', 'out');
const dst = join(here, '..', '..', 'web', 'pixel');
if (!existsSync(src)) {
  console.error('找不到 out/，請先執行 next build');
  process.exit(1);
}
rmSync(dst, { recursive: true, force: true });
cpSync(src, dst, { recursive: true });
console.log(`已輸出到 ${dst}`);

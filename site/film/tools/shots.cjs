#!/usr/bin/env node
// Look at the film at fixed moments, headless.
//
//   node site/film/tools/shots.cjs --out DIR [--T 0,0.5,1.4] [--theme dark|light] [--device desktop|phone] [--reduced] [--sheet]
//
// Serves site/public/ on a local port (so the page finds ../diagrams/ as it does on the documentation site), opens
// film/index.html in Chromium with software WebGL, and for each film time T asks the page to go there
// (window.__film.setT), waits for it to settle and saves a screenshot. Prints console errors, failed requests and
// what the renderer reports (draw calls, triangles). Exit status 1 when the page logged an error, a request failed,
// the page scrolls sideways or it never became ready. Needs Playwright and ffmpeg (for --sheet); neither is a
// dependency of the site.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require('playwright');

const arg = (name, dflt) => { const i = process.argv.indexOf('--' + name); return i < 0 ? dflt : (process.argv[i + 1] && !process.argv[i + 1].startsWith('--') ? process.argv[i + 1] : true); };
const SITE = path.resolve(__dirname, '..', '..', 'public');
const out = path.resolve(arg('out', 'shots'));
const theme = arg('theme', 'dark');
const device = arg('device', 'desktop');
const times = String(arg('T', '0,1.3,1.9,2.6,3.5,4.5,5.5,6.0,6.6,7.5,8.5,9.5,10.5,11.6,12.3,13.3')).split(',').map(Number);
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.glb': 'model/gltf-binary', '.bin': 'application/octet-stream', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.woff2': 'font/woff2' };

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]);
  const file = path.join(SITE, rel.endsWith('/') ? rel + 'index.html' : rel);
  if (!file.startsWith(SITE) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end('not found'); return; }
  res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-store' });
  fs.createReadStream(file).pipe(res);
});

(async () => {
  fs.mkdirSync(out, { recursive: true });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const view = device === 'phone' ? { width: 390, height: 844 } : { width: 1440, height: 900 };
  const context = await browser.newContext({ viewport: view, deviceScaleFactor: 1, colorScheme: theme, reducedMotion: arg('reduced', false) ? 'reduce' : 'no-preference', isMobile: device === 'phone', hasTouch: device === 'phone' });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  page.on('requestfailed', (r) => problems.push('request failed: ' + r.url().replace(base, '') + ' ' + (r.failure() || {}).errorText));
  page.on('response', (r) => { if (r.status() >= 400) problems.push('http ' + r.status() + ': ' + r.url().replace(base, '')); });
  await page.goto(base + 'film/index.html', { waitUntil: 'load' });
  const ready = await page.evaluate(() => Promise.race([
    (async () => { for (let i = 0; i < 600 && !window.__film; i++) await new Promise((r) => setTimeout(r, 50)); if (!window.__film) return 'no window.__film'; await window.__film.ready; return 'ready'; })(),
    new Promise((r) => setTimeout(() => r('timed out waiting for window.__film.ready'), 90000)),
  ]));
  console.log('page:', ready, '| theme', theme, '| device', device, view.width + 'x' + view.height);
  const files = [];
  if (ready === 'ready') {
    for (const T of times) {
      const t0 = Date.now();
      await page.evaluate((t) => window.__film.setT(t), T);
      await page.waitForTimeout(350);
      const info = await page.evaluate(() => (window.__film.info ? window.__film.info() : {}));
      const file = path.join(out, `${device}_${theme}_T${T.toFixed(2).replace('.', '_')}.jpg`);
      await page.screenshot({ path: file, type: 'jpeg', quality: 82 });
      files.push(file);
      console.log(`T=${T.toFixed(2)} calls=${info.calls} tris=${info.triangles} tokens=${JSON.stringify(info.readout || null)} ${Date.now() - t0}ms`);
    }
  } else { await page.screenshot({ path: path.join(out, `${device}_${theme}_notready.jpg`), type: 'jpeg', quality: 80 }); problems.push(ready); }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 1) problems.push(`the page scrolls sideways by ${overflow}px`);
  await browser.close();
  server.close();
  if (arg('sheet', false) && files.length > 1) {
    const cols = device === 'phone' ? Math.min(8, files.length) : Math.min(4, files.length);
    const rows = Math.ceil(files.length / cols);
    const list = path.join(out, `_${device}_${theme}_list.txt`);
    fs.writeFileSync(list, files.map((f) => `file '${f}'\nduration 1`).join('\n') + '\n');
    const sheet = path.join(out, `sheet_${device}_${theme}.jpg`);
    const w = device === 'phone' ? 260 : 640;
    execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', list, '-vf', `scale=${w}:-1,tile=${cols}x${rows}:padding=4:color=0x444444`, '-frames:v', '1', '-q:v', '3', sheet]);
    fs.unlinkSync(list);
    console.log('sheet:', sheet);
  }
  for (const p of [...new Set(problems)]) console.log('PROBLEM', p);
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

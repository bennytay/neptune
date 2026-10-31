// Render the film's shots in headless Chromium (three.js on the CPU, SwiftShader): no GPU needed.
//
//   node demo/film/web/render.js film [--only 04_jam] [--quality draft|preview|final]
//        [--width 1920 --height 1080] [--every N] [--frames A B] [--workers 2]
//
// Reads <film>/web/ (export_web.py) and <film>/shots.json, writes
// <film>/frames/<shot>/rgb_NNNN.png; demo/film/cut.sh makes the video. Needs `npm install` in
// demo/film/web and Playwright's Chromium.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const film = path.resolve(args[0] || 'film');
const opt = (name, fallback) => { const i = args.indexOf(`--${name}`); return i < 0 ? fallback : args[i + 1]; };
const only = (() => { const i = args.indexOf('--only'); if (i < 0) return null; const out = []; for (let k = i + 1; k < args.length && !args[k].startsWith('--'); k++) out.push(args[k]); return out; })();
const quality = opt('quality', 'preview');
const width = +opt('width', 1920), height = +opt('height', 1080), every = +opt('every', 1), workers = +opt('workers', 2);
const range = (() => { const i = args.indexOf('--frames'); return i < 0 ? null : [+args[i + 1], +args[i + 2]]; })();

const here = __dirname;
const types = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.bin': 'application/octet-stream' };
const server = http.createServer((req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0]);
  const file = url === '/data/film-shots.json' ? path.join(film, 'shots.json')
    : url.startsWith('/data/') ? path.join(film, 'web', url.slice(6)) : path.join(here, url);
  fs.readFile(file, (err, body) => {
    if (err) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' }); res.end(body);
  });
});

(async () => {
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const port = server.address().port;
  let list = JSON.parse(fs.readFileSync(path.join(film, 'web', 'shots.json'))).map((s) => s.name);
  if (only) list = list.filter((n) => only.some((p) => n.startsWith(p)));
  const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const jobs = [];
  for (const name of list) {
    const total = JSON.parse(fs.readFileSync(path.join(film, 'web', `${name}.json`))).frames;
    const [a, b] = range ? [range[0], Math.min(range[1], total - 1)] : [0, total - 1];
    for (let f = a; f <= b; f += every) jobs.push([name, f]);
  }
  const started = Date.now(); let done = 0;
  const worker = async () => {
    const page = await browser.newPage({ viewport: { width, height } });
    page.on('pageerror', (e) => console.error('page error:', e.message));
    page.on('console', (m) => { if (m.type() === 'error') console.error('console:', m.text()); });
    await page.goto(`http://127.0.0.1:${port}/render.html?data=/data`);
    await page.waitForFunction(() => window.ready, null, { timeout: 120000 });
    let current = null;
    while (jobs.length) {
      const [name, f] = jobs.shift();
      if (name !== current) { await page.evaluate(([n, w, h, q]) => window.setup(n, w, h, q), [name, width, height, quality]); current = name; }
      await page.evaluate((k) => window.frame(k), f);
      const out = path.join(film, 'frames', name); fs.mkdirSync(out, { recursive: true });
      await page.screenshot({ path: path.join(out, `rgb_${String(f).padStart(4, '0')}.png`) });
      done += 1;
      if (done % 20 === 0 || !jobs.length) {
        const per = (Date.now() - started) / 1000 / done;
        console.log(`${done} frames, ${per.toFixed(1)} s each; ${jobs.length} to go (~${Math.round(jobs.length * per / 60)} min)`);
      }
    }
    await page.close();
  };
  await Promise.all(Array.from({ length: workers }, worker));
  await browser.close(); server.close();
})().catch((e) => { console.error(e); process.exit(1); });

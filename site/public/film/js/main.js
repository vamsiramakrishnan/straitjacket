// Minimal page logic (placeholder until the page helper's version replaces it): film time from scroll, one render loop,
// window.__film for the screenshot tool.
import { measure, timeAt, scrollFor } from './chapters.js';

const TOKENS = ['bg', 'surface', 'surface2', 'ink', 'muted', 'faint', 'grid', 'blue', 'blue2', 'amber', 'amber2', 'red', 'red2', 'green'];
const readPalette = () => { const cs = getComputedStyle(document.documentElement); return Object.fromEntries(TOKENS.map((t) => [t, cs.getPropertyValue('--' + t).trim()])); };
let done; const ready = new Promise((r) => { done = r; });
const api = { ready, setT() {}, getT: () => 0, info: () => ({}), evaluate: null };
window.__film = api;

async function boot() {
  const canvas = document.getElementById('stage');
  const [{ createFilm }, tracks, data, bin] = await Promise.all([
    import('./scene/index.js'), import('./scene/tracks.js'),
    fetch('data/film.json').then((r) => r.json()), fetch('data/lines.bin').then((r) => r.arrayBuffer()),
  ]);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const layout = () => (innerWidth >= 900 ? 'wide' : 'narrow');
  const film = await createFilm({ canvas, labels: document.getElementById('labels'), data, lines: new Uint8Array(bin), palette: readPalette(), quality: 'hi', reducedMotion: reduced });
  document.documentElement.classList.add('film-on');
  let boxes = measure();
  const size = () => { film.resize(innerWidth, innerHeight, Math.min(devicePixelRatio || 1, 2), layout()); boxes = measure(); };
  size();
  addEventListener('resize', size);
  const hudV = document.getElementById('hud-value'), hudN = document.getElementById('hud-note');
  let T = 0;
  const draw = (seconds) => {
    T = timeAt(boxes, scrollY);
    film.render(T, seconds);
    const r = tracks.evaluate(T).readout;
    if (r) { hudV.textContent = r.value; hudN.textContent = r.note; hudV.parentElement.dataset.tone = r.tone; }
  };
  const loop = (ms) => { draw(ms / 1000); requestAnimationFrame(loop); };
  Object.assign(api, {
    setT(t) { scrollTo(0, scrollFor(boxes, t)); draw(0); },
    getT: () => T,
    info: () => ({ ...film.info(), readout: tracks.evaluate(T).readout }),
    evaluate: tracks.evaluate,
  });
  draw(0);
  done();
  requestAnimationFrame(loop);
}
boot().catch((e) => { console.error(e); document.documentElement.classList.add('film-off'); done(); });

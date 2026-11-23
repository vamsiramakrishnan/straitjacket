// STUB (to be replaced by the scene helper): camera and readout as a pure function of film time T.
const lerp = (a, b, t) => a + (b - a) * t;
const sm = (a, b, x) => { const t = Math.min(Math.max((x - a) / (b - a), 0), 1); return t * t * (3 - 2 * t); };
const KEYS = [[0, [-0.8, 2.3, 9.5], [-0.6, 1.1, -0.8]], [2, [3.6, 1.5, 4.6], [2.4, 1.0, 0]], [3, [0.5, 1.6, 3.2], [22, 1.0, 0]], [4, [2.4, 1.1, 4.4], [2.4, 0.98, 0]],
  [6, [-1.4, 1.3, 0.4], [-1.4, 1.0, -3.6]], [7, [1.9, 0.75, 1.6], [1.45, 0.45, 0.2]], [9, [0.5, 1.6, 3.2], [22, 1.0, 0]], [10, [-9, 1.2, 3.4], [-9, 1.0, -0.5]],
  [11, [4, 3.2, 7], [4, 0.8, 0]], [12, [3, 24, 2], [3, 0, 0]], [14, [-0.8, 3, 11], [-0.6, 1.1, -0.8]]];
export function evaluate(T) {
  let k = 0; while (k < KEYS.length - 2 && T >= KEYS[k + 1][0]) k++;
  const [a, b] = [KEYS[k], KEYS[k + 1]]; const t = sm(a[0], b[0], T);
  const mix = (i) => [0, 1, 2].map((j) => lerp(a[i][j], b[i][j], t));
  const flood = sm(1.08, 1.85, T) * (1 - sm(4.1, 4.7, T));
  const value = T < 5 ? Math.round(302628 * flood).toLocaleString('en-US') : T < 6.1 ? '0' : '531';
  const tone = T < 1 ? 'idle' : T < 5 ? 'loss' : 'bound';
  return { cam: { pos: mix(1), target: mix(2), fov: 32 }, p: { flood }, readout: { value, note: T < 5 ? 'resident in the prompt' : 'the digest', tone } };
}

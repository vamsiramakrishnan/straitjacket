// STUB (to be replaced by the scene helper): the interface of the film with a placeholder scene.
import * as THREE from '../../vendor/three.module.min.js';
import { evaluate } from './tracks.js';

export async function createFilm({ canvas, labels, data, lines, palette, quality, reducedMotion }) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.toneMapping = THREE.NeutralToneMapping;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 200);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x222222, 1.2));
  const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(4, 8, 6); scene.add(key);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(120, 120), new THREE.MeshStandardMaterial({ roughness: 1 }));
  floor.rotation.x = -Math.PI / 2; scene.add(floor);
  const grid = new THREE.GridHelper(76.8, 120); grid.position.y = 0.002; scene.add(grid);
  const box = (w, h, d, x, y, z, m) => { const b = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m); b.position.set(x, y, z); scene.add(b); return b; };
  const mat = new THREE.MeshStandardMaterial({ roughness: 0.7 });
  const red = new THREE.MeshStandardMaterial({ roughness: 0.8 });
  box(1.0, 1.55, 0.75, -5.0, 0.775, 0, mat); box(0.12, 1.84, 1.2, -1.6, 0.92, 0, mat); box(4.4, 1.98, 0.62, -1.4, 0.99, -3.6, mat);
  const frames = []; for (let k = 0; k < 7; k++) { box(2.8, 0.12, 0.66, 2.4 + 3.3 * k, 0.12, 0, mat); box(2.8, 0.12, 0.66, 2.4 + 3.3 * k, 1.84, 0, mat); frames.push(box(2.5, 1.55, 0.56, 2.4 + 3.3 * k, 0.98, 0, red)); }
  const apply = (p) => { scene.background = new THREE.Color(p.bg); scene.fog = new THREE.Fog(p.bg, 18, 70); floor.material.color.set(p.bg); mat.color.set(p.surface2); red.color.set(p.red);
    grid.material.color.set(p.grid); grid.material.vertexColors = false; grid.material.needsUpdate = true; };
  apply(palette);
  let last = 0;
  return {
    render(T) { const t0 = performance.now(); const s = evaluate(T); camera.position.set(...s.cam.pos); camera.lookAt(...s.cam.target); camera.fov = s.cam.fov; camera.updateProjectionMatrix();
      frames.forEach((f, k) => { const u = k === 0 ? s.p.flood : (T > 2.2 && T < 3 ? 1 : 0); f.scale.y = Math.max(u, 0.001); f.position.y = 0.2 + 0.775 * u; f.visible = u > 0.002; });
      renderer.render(scene, camera); last = performance.now() - t0; },
    resize(w, h, dpr) { renderer.setPixelRatio(dpr); renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); },
    setPalette: apply, setQuality() {},
    info: () => ({ calls: renderer.info.render.calls, triangles: renderer.info.render.triangles, frameMs: last, propsLoaded: false }),
    dispose() { renderer.dispose(); },
  };
}

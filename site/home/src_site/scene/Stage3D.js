import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import {
  createBackdropMaterial,
  createDatumGridMaterial,
  createEvidenceStreamMaterial,
  createGateApertureMaterial,
  createScanBeamMaterial,
  createRelayArcMaterial,
} from '../shaders/index.js';

const STATION_GATE = new THREE.Vector3(0, 0, 0);
const STATION_ANCHOR = new THREE.Vector3(3.2, 0, -9.8);
const STATION_ROUTER = new THREE.Vector3(-2.6, 0, -19.6);

const KEYFRAMES = [
  // 0.00 Hero — Containment Gate sweeping 3/4 view framed cleanly in open right half
  {
    p: 0.0,
    cam: new THREE.Vector3(2.15, 1.54, 3.85),
    look: new THREE.Vector3(-0.62, 0.62, 0.0),
    focus: STATION_GATE,
    accent: new THREE.Color('#3b82f6'),
    secondary: new THREE.Color('#d97706'),
    horizon: new THREE.Color('#0c172b'),
  },
  // 0.20 Evidence Fates — Intake nozzle & turbulent flood on open left half
  {
    p: 0.2,
    cam: new THREE.Vector3(-2.35, 1.42, 3.35),
    look: new THREE.Vector3(0.35, 0.66, 0.0),
    focus: STATION_GATE,
    accent: new THREE.Color('#ef4444'),
    secondary: new THREE.Color('#3b82f6'),
    horizon: new THREE.Color('#1c0d14'),
  },
  // 0.38 Four Gates & Digest Anatomy — Close side-oblique profile on open right half
  {
    p: 0.38,
    cam: new THREE.Vector3(0.45, 1.55, 2.95),
    look: new THREE.Vector3(-0.48, 0.72, 0.0),
    focus: STATION_GATE,
    accent: new THREE.Color('#2563eb'),
    secondary: new THREE.Color('#f59e0b'),
    horizon: new THREE.Color('#0a182d'),
  },
  // 0.58 Content Anchors — Anchor Caliper + holo plaque framed unobstructed on right half
  {
    p: 0.58,
    cam: new THREE.Vector3(2.15, 1.52, -5.95),
    look: new THREE.Vector3(2.55, 0.76, -9.8),
    focus: STATION_ANCHOR,
    accent: new THREE.Color('#22c55e'),
    secondary: new THREE.Color('#f59e0b'),
    horizon: new THREE.Color('#091b18'),
  },
  // 0.78 Cross-Harness ACP Routing — 7-port Harness Router on open left half
  {
    p: 0.78,
    cam: new THREE.Vector3(-0.55, 2.22, -15.95),
    look: new THREE.Vector3(-1.95, 0.54, -19.6),
    focus: STATION_ROUTER,
    accent: new THREE.Color('#60a5fa'),
    secondary: new THREE.Color('#f59e0b'),
    horizon: new THREE.Color('#0d162c'),
  },
  // 1.00 Receipts & Quickstart — Panoramic architectural constellation
  {
    p: 1.0,
    cam: new THREE.Vector3(4.9, 4.75, -4.6),
    look: new THREE.Vector3(-0.15, 0.45, -10.2),
    focus: new THREE.Vector3(0.2, 0, -9.8),
    accent: new THREE.Color('#3b82f6'),
    secondary: new THREE.Color('#f59e0b'),
    horizon: new THREE.Color('#091325'),
  },
];

function createAnchorHoloTexture(textTop, textMain, colorHex) {
  const c = document.createElement('canvas');
  c.width = 512;
  c.height = 192;
  const ctx = c.getContext('2d');
  ctx.fillStyle = 'rgba(8, 13, 22, 0.88)';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.strokeStyle = colorHex;
  ctx.lineWidth = 4;
  ctx.strokeRect(4, 4, c.width - 8, c.height - 8);

  ctx.fillStyle = colorHex;
  ctx.font = '600 22px monospace';
  ctx.fillText(textTop, 24, 46);

  ctx.fillStyle = '#e2e8f0';
  ctx.font = '700 30px monospace';
  ctx.fillText(textMain, 24, 108);

  ctx.fillStyle = '#94a3b8';
  ctx.font = '500 20px monospace';
  ctx.fillText('0 WRONG CONTENT · 1,920 LOOKUPS', 24, 158);

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

export class Stage3D {
  constructor(canvas, callbacks = {}) {
    this.canvas = canvas;
    this.callbacks = callbacks;
    this.scrollProgress = 0;
    this.targetScroll = 0;
    this.pointer = new THREE.Vector2(0, 0);
    this.smoothPointer = new THREE.Vector2(0, 0);
    this.clock = new THREE.Clock();

    this.evidenceMode = 0;
    this.activeGateIdx = 0;
    this.anchorShiftTarget = 0.0;
    this.anchorShiftCurrent = 0.0;
    this.anchorColorTarget = new THREE.Color('#22c55e');
    this.activeRouteBays = [2, 0, 5, 1];

    this.loadedModels = {};
    this.modelCount = 0;
    this.webglReady = false;
    this.keyframeStops = KEYFRAMES.map((k) => k.p);

    this._initRenderer();
    if (!this.webglReady) return;

    this._initScene();
    this._initLighting();
    this._initEnvironmentAndShaders();
    this._initStations();
    this._loadModels();
    this._bindEvents();
    this._animate();
  }

  _initRenderer() {
    try {
      this.renderer = new THREE.WebGLRenderer({
        canvas: this.canvas,
        antialias: true,
        powerPreference: 'high-performance',
        alpha: false,
      });
    } catch (err) {
      console.warn('WebGL unavailable, falling back to CSS atmosphere:', err);
      this.callbacks.onWebGLFallback?.();
      return;
    }

    this.webglReady = true;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.22;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2('#070b14', 0.028);

    const aspect = window.innerWidth / Math.max(window.innerHeight, 1);
    this.camera = new THREE.PerspectiveCamera(42, aspect, 0.1, 140);
    this.camera.position.copy(KEYFRAMES[0].cam);
    this.currentLook = KEYFRAMES[0].look.clone();
    this.currentFocus = STATION_GATE.clone();
    this.camera.lookAt(this.currentLook);

    // Studio metallic environment map via RoomEnvironment
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    const roomEnv = new RoomEnvironment();
    this.envRT = pmrem.fromScene(roomEnv, 0.04);
    this.scene.environment = this.envRT.texture;
    roomEnv.dispose();
    pmrem.dispose();
  }

  _initLighting() {
    const hemi = new THREE.HemisphereLight('#c7dcff', '#0b1120', 0.95);
    this.scene.add(hemi);

    const key = new THREE.DirectionalLight('#f8fafc', 2.6);
    key.position.set(6, 10, 7);
    this.scene.add(key);

    const rim = new THREE.DirectionalLight('#f59e0b', 1.45);
    rim.position.set(-7, 5, -9);
    this.scene.add(rim);

    // Station accent point lights
    this.gateLight = new THREE.PointLight('#3b82f6', 4.5, 9, 1.5);
    this.gateLight.position.set(0, 1.6, 1.4);
    this.scene.add(this.gateLight);

    this.anchorLight = new THREE.PointLight('#22c55e', 4.2, 9, 1.5);
    this.anchorLight.position.set(STATION_ANCHOR.x, 1.65, STATION_ANCHOR.z + 1.5);
    this.scene.add(this.anchorLight);

    this.routerLight = new THREE.PointLight('#60a5fa', 4.6, 10, 1.5);
    this.routerLight.position.set(STATION_ROUTER.x, 2.0, STATION_ROUTER.z + 1.3);
    this.scene.add(this.routerLight);
  }

  _initEnvironmentAndShaders() {
    // 1. Volumetric Backdrop Dome
    this.backdropMat = createBackdropMaterial();
    const dome = new THREE.Mesh(new THREE.SphereGeometry(75, 32, 24), this.backdropMat);
    this.scene.add(dome);

    // 2. Architectural Precision Datum Floor Grid
    this.gridMat = createDatumGridMaterial();
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(90, 90), this.gridMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(0, -0.005, -10);
    this.scene.add(floor);

    // Connecting optical datum rails joining all 3 stations
    this._buildStationRails();
  }

  _buildStationRails() {
    const railPoints = [
      new THREE.Vector3(0, 0.025, 0),
      new THREE.Vector3(1.6, 0.025, -4.9),
      new THREE.Vector3(STATION_ANCHOR.x, 0.025, STATION_ANCHOR.z),
      new THREE.Vector3(0.4, 0.025, -14.7),
      new THREE.Vector3(STATION_ROUTER.x, 0.025, STATION_ROUTER.z),
    ];
    const curve = new THREE.CatmullRomCurve3(railPoints);
    const tubeGeo = new THREE.TubeGeometry(curve, 96, 0.024, 8, false);
    this.corridorRailMat = createRelayArcMaterial();
    this.corridorRailMat.uniforms.uColor.value.set('#3b82f6');
    this.corridorRailMat.uniforms.uActive.value = 0.68;
    const tube = new THREE.Mesh(tubeGeo, this.corridorRailMat);
    this.scene.add(tube);
  }

  _initStations() {
    /* ---------- STATION 0: CONTAINMENT GATE ---------- */
    this.gateGroup = new THREE.Group();
    this.gateGroup.position.copy(STATION_GATE);
    this.scene.add(this.gateGroup);

    // GPU Particle Evidence Stream (4,200 particles)
    const count = 4200;
    const positions = new Float32Array(count * 3);
    const seeds = new Float32Array(count);
    const phases = new Float32Array(count);
    const tracks = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      positions[i * 3] = 0;
      positions[i * 3 + 1] = 0.68;
      positions[i * 3 + 2] = 0;
      seeds[i] = Math.random();
      phases[i] = Math.random();
      const r = Math.random();
      if (i < 16) {
        tracks[i] = 2.0; // The quiet needle at line 14,238
      } else if (r < 0.85) {
        tracks[i] = 0.0; // Diverted raw log flood
      } else {
        tracks[i] = 1.0; // Bounded 531-token digest stream
      }
    }

    const pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    pGeo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));
    pGeo.setAttribute('aPhase', new THREE.BufferAttribute(phases, 1));
    pGeo.setAttribute('aTrack', new THREE.BufferAttribute(tracks, 1));

    this.streamMat = createEvidenceStreamMaterial();
    this.streamPoints = new THREE.Points(pGeo, this.streamMat);
    this.streamPoints.frustumCulled = false;
    this.gateGroup.add(this.streamPoints);

    // 4 Optical Gate Aperture Shader Rings inside containment_gate's 4 rings
    this.gateApertureMats = [];
    const ringXs = [-0.45, -0.15, 0.15, 0.45];
    ringXs.forEach((rx, idx) => {
      const mat = createGateApertureMaterial(idx);
      const plane = new THREE.Mesh(new THREE.PlaneGeometry(0.92, 0.92), mat);
      plane.position.set(rx, 0.68, 0);
      plane.rotation.y = Math.PI / 2;
      this.gateGroup.add(plane);
      this.gateApertureMats.push(mat);
    });

    // Subtle artifact-store datum rim beneath chamber
    const storeRim = new THREE.Mesh(
      new THREE.RingGeometry(1.15, 1.21, 48),
      new THREE.MeshBasicMaterial({
        color: '#2563eb',
        transparent: true,
        opacity: 0.35,
        side: THREE.DoubleSide,
      })
    );
    storeRim.rotation.x = -Math.PI / 2;
    storeRim.position.y = 0.008;
    this.gateGroup.add(storeRim);

    /* ---------- STATION 1: ANCHOR CALIPER ---------- */
    this.anchorGroup = new THREE.Group();
    this.anchorGroup.position.copy(STATION_ANCHOR);
    this.anchorGroup.rotation.y = -0.26;
    this.scene.add(this.anchorGroup);

    this.scanBeamMat = createScanBeamMaterial();
    this.scanBeamMesh = new THREE.Mesh(new THREE.PlaneGeometry(0.42, 0.72), this.scanBeamMat);
    this.scanBeamMesh.position.set(0.06, 0.66, 0.0);
    this.anchorGroup.add(this.scanBeamMesh);

    // Floating holographic address plaque behind caliper
    this.holoTex = createAnchorHoloTexture('ANCHOR RELOCATED', 'L40:42@07407f1c → L42:44', '#22c55e');
    this.holoMat = new THREE.MeshBasicMaterial({
      map: this.holoTex,
      transparent: true,
      opacity: 0.92,
      side: THREE.DoubleSide,
    });
    this.holoCard = new THREE.Mesh(new THREE.PlaneGeometry(1.42, 0.52), this.holoMat);
    this.holoCard.position.set(0, 1.48, -0.35);
    this.anchorGroup.add(this.holoCard);

    /* ---------- STATION 2: HARNESS ROUTER ---------- */
    this.routerGroup = new THREE.Group();
    this.routerGroup.position.copy(STATION_ROUTER);
    this.scene.add(this.routerGroup);

    this.relayArcMats = [];
    this.harnessOrbs = [];

    for (let k = 0; k < 7; k++) {
      const angleRad = (k * 2 * Math.PI) / 7;
      const cosA = Math.cos(angleRad);
      const sinA = Math.sin(angleRad);
      // Matches the 7 radial docking ports of harness_router.glb (Y-up: X = cos, Z = -sin)
      const bayPos = new THREE.Vector3(cosA * 0.78, 0.36, -sinA * 0.78);
      const midPos = new THREE.Vector3(cosA * 1.38, 1.24, -sinA * 1.38);
      const crownPos = new THREE.Vector3(cosA * 0.28, 1.06, -sinA * 0.28);

      const arcCurve = new THREE.QuadraticBezierCurve3(bayPos, midPos, crownPos);
      const arcGeo = new THREE.TubeGeometry(arcCurve, 32, 0.016, 8, false);
      const arcMat = createRelayArcMaterial();
      arcMat.uniforms.uPhase.value = k / 7;
      const arcMesh = new THREE.Mesh(arcGeo, arcMat);
      this.routerGroup.add(arcMesh);
      this.relayArcMats.push(arcMat);

      const orbMat = new THREE.MeshStandardMaterial({
        color: '#60a5fa',
        emissive: '#2563eb',
        emissiveIntensity: 1.4,
        metalness: 0.3,
        roughness: 0.2,
      });
      const orb = new THREE.Mesh(new THREE.OctahedronGeometry(0.065, 0), orbMat);
      orb.position.copy(midPos);
      this.routerGroup.add(orb);
      this.harnessOrbs.push({ mesh: orb, basePos: midPos.clone(), angle: angleRad });
    }
  }

  _loadModels() {
    const loader = new GLTFLoader();
    const specs = [
      { key: 'containment_gate', url: './models/containment_gate.glb', parent: this.gateGroup, scale: 1 / 0.65 },
      { key: 'anchor_caliper', url: './models/anchor_caliper.glb', parent: this.anchorGroup, scale: 1.0 },
      { key: 'harness_router', url: './models/harness_router.glb', parent: this.routerGroup, scale: 1.0 },
    ];

    specs.forEach((spec) => {
      loader.load(
        spec.url,
        (gltf) => {
          const root = gltf.scene;
          root.scale.setScalar(spec.scale);
          let meshCount = 0;
          root.traverse((obj) => {
            if (obj.isMesh) {
              meshCount++;
              if (obj.material) {
                obj.material.envMapIntensity = 1.25;
                obj.material.needsUpdate = true;
              }
            }
          });
          spec.parent.add(root);
          this.loadedModels[spec.key] = { root, meshCount };
          this.modelCount++;
          this.callbacks.onModelLoaded?.(spec.key, this.modelCount, specs.length);
        },
        undefined,
        (err) => {
          console.error(`Failed loading ${spec.url}:`, err);
        }
      );
    });
  }

  setScrollProgress(p) {
    this.targetScroll = THREE.MathUtils.clamp(p, 0, 1);
  }

  setChapterStops(stops) {
    if (Array.isArray(stops) && stops.length === KEYFRAMES.length) {
      this.keyframeStops = stops.slice();
    }
  }

  setEvidenceMode(modeIndex) {
    this.evidenceMode = modeIndex;
    if (this.streamMat) {
      this.streamMat.uniforms.uMode.value = modeIndex;
    }
    if (this.gateLight) {
      if (modeIndex === 1) this.gateLight.color.set('#ef4444');
      else if (modeIndex === 2) this.gateLight.color.set('#f59e0b');
      else this.gateLight.color.set('#3b82f6');
    }
  }

  setActiveGate(idx) {
    this.activeGateIdx = idx;
  }

  setAnchorScenario(scenario) {
    this.anchorShiftTarget = scenario.shiftOffset;
    const colorMap = {
      green: '#22c55e',
      blue: '#3b82f6',
      amber: '#f59e0b',
      red: '#ef4444',
    };
    const hex = colorMap[scenario.tone] || '#22c55e';
    this.anchorColorTarget.set(hex);

    if (this.anchorLight) this.anchorLight.color.set(hex);
    if (this.holoTex) this.holoTex.dispose();
    const topLabel = scenario.status === 'REFUSED · SAFE' ? 'ANCHOR REFUSED · SAFE' : `ANCHOR ${scenario.status}`;
    const mainLabel =
      scenario.id === 'insert-above'
        ? 'L40:42@07407f1c → L42:44'
        : scenario.id === 'delete-above'
        ? 'L40:42@07407f1c → L35:37'
        : scenario.id === 'move-the-span'
        ? 'L40:42@07407f1c → L118:120'
        : 'REFRESH & REPLAN · 0 WRONG';
    this.holoTex = createAnchorHoloTexture(topLabel, mainLabel, hex);
    this.holoMat.map = this.holoTex;
    this.holoMat.needsUpdate = true;
  }

  setActiveRoute(activeBays) {
    this.activeRouteBays = activeBays;
  }

  _bindEvents() {
    this._onResize = () => {
      if (!this.renderer) return;
      const w = window.innerWidth;
      const h = Math.max(window.innerHeight, 1);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      this.renderer.setSize(w, h);
      if (this.streamMat) {
        this.streamMat.uniforms.uPixelRatio.value = Math.min(window.devicePixelRatio || 1, 2);
      }
    };

    this._onPointerMove = (e) => {
      this.pointer.x = (e.clientX / Math.max(window.innerWidth, 1)) * 2 - 1;
      this.pointer.y = -(e.clientY / Math.max(window.innerHeight, 1)) * 2 + 1;
    };

    window.addEventListener('resize', this._onResize, { passive: true });
    window.addEventListener('pointermove', this._onPointerMove, { passive: true });
  }

  _sampleKeyframes(p) {
    const stops = this.keyframeStops;
    let i = 0;
    while (i < KEYFRAMES.length - 2 && p > stops[i + 1]) i++;
    const a = KEYFRAMES[i];
    const b = KEYFRAMES[i + 1];
    const pA = stops[i];
    const pB = stops[i + 1];
    const rawT = THREE.MathUtils.clamp((p - pA) / Math.max(pB - pA, 0.0001), 0, 1);
    // Smoothstep ease for cinematic scroll deceleration
    const t = rawT * rawT * (3 - 2 * rawT);

    const isMobile = window.innerWidth < 768;
    const cam = new THREE.Vector3().lerpVectors(a.cam, b.cam, t);
    const look = new THREE.Vector3().lerpVectors(a.look, b.look, t);
    const focus = new THREE.Vector3().lerpVectors(a.focus, b.focus, t);

    if (isMobile) {
      // Center on the station horizontally, pull back for portrait FOV, and pitch down so the 3D instrument rises cleanly above the mobile card
      look.x = focus.x;
      look.y = focus.y - 0.58;
      const dir = cam.clone().sub(focus);
      dir.multiplyScalar(1.55);
      dir.y += 0.25;
      cam.copy(focus).add(dir);
    }

    const accent = new THREE.Color().lerpColors(a.accent, b.accent, t);
    const secondary = new THREE.Color().lerpColors(a.secondary, b.secondary, t);
    const horizon = new THREE.Color().lerpColors(a.horizon, b.horizon, t);
    return { cam, look, focus, accent, secondary, horizon };
  }

  _animate() {
    this._raf = requestAnimationFrame(() => this._animate());
    const dt = Math.min(this.clock.getDelta(), 0.05);
    const elapsed = this.clock.getElapsedTime();

    // Smooth scroll & pointer damping
    this.scrollProgress = THREE.MathUtils.lerp(this.scrollProgress, this.targetScroll, 1 - Math.exp(-7.5 * dt));
    this.smoothPointer.lerp(this.pointer, 1 - Math.exp(-4.0 * dt));

    const kf = this._sampleKeyframes(this.scrollProgress);

    // Apply subtle parallax around the interpolated camera position
    const parallaxX = this.smoothPointer.x * 0.24;
    const parallaxY = this.smoothPointer.y * 0.14;
    this.camera.position.set(kf.cam.x + parallaxX, kf.cam.y + parallaxY, kf.cam.z);
    this.currentLook.lerp(kf.look, 1 - Math.exp(-9.0 * dt));
    this.camera.lookAt(this.currentLook);

    // Update volumetric backdrop & datum grid shaders
    this.backdropMat.uniforms.uTime.value = elapsed;
    this.backdropMat.uniforms.uAccentColor.value.lerp(kf.accent, 0.08);
    this.backdropMat.uniforms.uSecondaryColor.value.lerp(kf.secondary, 0.08);
    this.backdropMat.uniforms.uHorizonColor.value.lerp(kf.horizon, 0.08);

    this.gridMat.uniforms.uTime.value = elapsed;
    this.gridMat.uniforms.uFocusPos.value.lerp(kf.focus, 0.1);
    this.gridMat.uniforms.uAccentColor.value.lerp(kf.accent, 0.08);

    this.corridorRailMat.uniforms.uTime.value = elapsed;

    // Station 0: Containment Gate particles + 4 gate iris rings
    this.streamMat.uniforms.uTime.value = elapsed;
    const gateEnd = this.keyframeStops[2] + (this.keyframeStops[3] - this.keyframeStops[2]) * 0.5;
    const gateFadeEnd = this.keyframeStops[3];
    const gateVisible = 1.0 - THREE.MathUtils.smoothstep(this.scrollProgress, gateEnd, gateFadeEnd);
    const overviewBoost = THREE.MathUtils.smoothstep(this.scrollProgress, 0.86, 0.98) * 0.65;
    this.streamMat.uniforms.uOpacity.value = Math.max(gateVisible, overviewBoost);

    this.gateApertureMats.forEach((mat, idx) => {
      mat.uniforms.uTime.value = elapsed;
      const isFocused = idx === this.activeGateIdx;
      const targetAct = isFocused ? 0.92 : 0.24;
      mat.uniforms.uActive.value = THREE.MathUtils.lerp(mat.uniforms.uActive.value, targetAct, 0.1);
      const targetCol = this.evidenceMode === 1 ? '#ef4444' : isFocused ? '#60a5fa' : '#2563eb';
      mat.uniforms.uColor.value.lerp(new THREE.Color(targetCol), 0.1);
    });

    // Station 1: Anchor Caliper scan beam & gentle holo hover
    this.anchorShiftCurrent = THREE.MathUtils.lerp(this.anchorShiftCurrent, this.anchorShiftTarget, 1 - Math.exp(-6.0 * dt));
    this.scanBeamMesh.position.x = 0.06 + this.anchorShiftCurrent * 0.45;
    this.scanBeamMat.uniforms.uTime.value = elapsed;
    this.scanBeamMat.uniforms.uColor.value.lerp(this.anchorColorTarget, 0.12);
    this.holoCard.position.y = 1.48 + Math.sin(elapsed * 1.4) * 0.025;

    // Station 2: Harness Router gentle orbital rotation & relay packets
    this.routerGroup.rotation.y = elapsed * 0.09;
    this.relayArcMats.forEach((mat, k) => {
      mat.uniforms.uTime.value = elapsed;
      const isActive = this.activeRouteBays.includes(k);
      mat.uniforms.uActive.value = THREE.MathUtils.lerp(mat.uniforms.uActive.value, isActive ? 1.0 : 0.16, 0.08);
      const targetCol = isActive ? '#3b82f6' : '#334155';
      mat.uniforms.uColor.value.lerp(new THREE.Color(targetCol), 0.1);
    });

    this.harnessOrbs.forEach((o, k) => {
      const isActive = this.activeRouteBays.includes(k);
      o.mesh.position.y = o.basePos.y + Math.sin(elapsed * 2.0 + k) * 0.04;
      o.mesh.rotation.y = elapsed * 1.3 + k;
      const s = isActive ? 1.15 : 0.65;
      o.mesh.scale.lerp(new THREE.Vector3(s, s, s), 0.1);
      o.mesh.material.emissiveIntensity = THREE.MathUtils.lerp(
        o.mesh.material.emissiveIntensity,
        isActive ? 2.0 : 0.25,
        0.1
      );
    });

    this.callbacks.onFrame?.({
      camZ: this.camera.position.z,
      progress: this.scrollProgress,
    });

    this.renderer.render(this.scene, this.camera);
  }

  getDebugState() {
    return {
      webglReady: this.webglReady,
      modelsLoaded: Object.keys(this.loadedModels),
      meshCounts: Object.fromEntries(
        Object.entries(this.loadedModels).map(([k, v]) => [k, v.meshCount])
      ),
      cameraPosition: this.camera ? this.camera.position.toArray().map((n) => Number(n.toFixed(2))) : null,
      scrollProgress: Number(this.scrollProgress.toFixed(3)),
      evidenceMode: this.evidenceMode,
      activeGateIdx: this.activeGateIdx,
    };
  }
}

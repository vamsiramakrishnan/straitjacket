import * as THREE from 'three';

/* --------------------------------------------------------------------------
 * 1. VOLUMETRIC INSTRUMENT BACKDROP DOME SHADER
 * -------------------------------------------------------------------------- */
export function createBackdropMaterial() {
  return new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      uTime: { value: 0 },
      uTopColor: { value: new THREE.Color('#05080f') },
      uHorizonColor: { value: new THREE.Color('#0c1729') },
      uAccentColor: { value: new THREE.Color('#2563eb') },
      uSecondaryColor: { value: new THREE.Color('#d97706') },
      uAccentStrength: { value: 0.32 },
    },
    vertexShader: /* glsl */ `
      varying vec3 vWorldDir;
      void main() {
        vec4 worldPos = modelMatrix * vec4(position, 1.0);
        vWorldDir = normalize(position);
        gl_Position = projectionMatrix * viewMatrix * worldPos;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uTopColor;
      uniform vec3 uHorizonColor;
      uniform vec3 uAccentColor;
      uniform vec3 uSecondaryColor;
      uniform float uAccentStrength;
      varying vec3 vWorldDir;

      float hash(vec2 p) {
        return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
      }

      float noise(vec2 p) {
        vec2 i = floor(p);
        vec2 f = fract(p);
        f = f * f * (3.0 - 2.0 * f);
        float a = hash(i);
        float b = hash(i + vec2(1.0, 0.0));
        float c = hash(i + vec2(0.0, 1.0));
        float d = hash(i + vec2(1.0, 1.0));
        return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
      }

      void main() {
        vec3 d = normalize(vWorldDir);
        float h = clamp(d.y * 0.5 + 0.5, 0.0, 1.0);
        vec3 col = mix(uHorizonColor, uTopColor, smoothstep(0.32, 0.85, h));

        // Subtle sweeping atmospheric light lobes
        float az = atan(d.z, d.x);
        float lobe1 = exp(-pow(d.y - 0.08, 2.0) * 10.0) * (0.5 + 0.5 * cos(az * 1.4 - uTime * 0.08));
        float lobe2 = exp(-pow(d.y + 0.04, 2.0) * 14.0) * (0.5 + 0.5 * sin(az * 2.0 + uTime * 0.06));
        float mist = noise(vec2(az * 2.5 + uTime * 0.03, d.y * 5.0));

        col += uAccentColor * lobe1 * (0.45 + 0.55 * mist) * uAccentStrength;
        col += uSecondaryColor * lobe2 * (0.35 + 0.65 * mist) * (uAccentStrength * 0.55);

        // Subtle anti-banding film grain
        float grain = (hash(gl_FragCoord.xy + fract(uTime)) - 0.5) * 0.012;
        col += grain;

        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });
}

/* --------------------------------------------------------------------------
 * 2. ARCHITECTURAL INSTRUMENT DATUM FLOOR GRID SHADER
 * -------------------------------------------------------------------------- */
export function createDatumGridMaterial() {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    uniforms: {
      uTime: { value: 0 },
      uFocusPos: { value: new THREE.Vector3(0, 0, 0) },
      uAccentColor: { value: new THREE.Color('#3b82f6') },
      uBaseColor: { value: new THREE.Color('#1e293b') },
    },
    vertexShader: /* glsl */ `
      varying vec3 vWorldPos;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorldPos = wp.xyz;
        gl_Position = projectionMatrix * viewMatrix * wp;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uFocusPos;
      uniform vec3 uAccentColor;
      uniform vec3 uBaseColor;
      varying vec3 vWorldPos;

      float gridLine(vec2 coord, float stepSize, float width) {
        vec2 g = abs(fract(coord / stepSize - 0.5) - 0.5) / (width / stepSize);
        float line = min(g.x, g.y);
        return 1.0 - min(line, 1.0);
      }

      void main() {
        vec2 p = vWorldPos.xz;
        float dist = length(p - uFocusPos.xz);

        float minor = gridLine(p, 0.5, 0.012);
        float major = gridLine(p, 2.0, 0.022);

        // Expanding sonar calibration ring from the active instrument
        float ringWave = fract(dist * 0.16 - uTime * 0.22);
        float pulse = smoothstep(0.92, 1.0, ringWave) * exp(-dist * 0.18);

        // Radial precision tick circle right under the active instrument
        float baseRing = exp(-abs(dist - 1.55) * 14.0) * 0.75;
        float outerRing = exp(-abs(dist - 2.65) * 16.0) * 0.35;

        float fade = exp(-dist * 0.085);
        float gridAlpha = (minor * 0.16 + major * 0.38) * fade;
        vec3 col = mix(uBaseColor * 2.2, uAccentColor, clamp(pulse * 1.4 + baseRing, 0.0, 1.0));

        float alpha = clamp(gridAlpha + (pulse * 0.42 + baseRing * 0.55 + outerRing * 0.3) * fade, 0.0, 0.85);
        if (alpha < 0.005) discard;

        gl_FragColor = vec4(col, alpha);
      }
    `,
  });
}

/* --------------------------------------------------------------------------
 * 3. GPU EVIDENCE PARTICLE STREAM SHADER (FLOOD vs TRUNC vs CONTAINED)
 * -------------------------------------------------------------------------- */
export function createEvidenceStreamMaterial() {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uMode: { value: 0.0 }, // 0 = Straitjacket, 1 = Naive Flood, 2 = Head+Tail Trunc
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
      uOpacity: { value: 1.0 },
    },
    vertexShader: /* glsl */ `
      attribute float aSeed;
      attribute float aPhase;
      attribute float aTrack; // 0 = flood/diverted, 1 = bounded digest, 2 = quiet needle

      uniform float uTime;
      uniform float uMode;
      uniform float uPixelRatio;

      varying vec3 vColor;
      varying float vAlpha;

      void main() {
        float speed = mix(0.20, 0.34, fract(aSeed * 7.31));
        float t = fract(aPhase + uTime * speed); // 0..1 lifecycle along X

        // Base X journey: -4.2 (raw log intake) -> 0.0 (containment chamber) -> +4.2 (agent context)
        float x = mix(-4.4, 4.4, t);
        float angle = aSeed * 6.2831853 + uTime * (1.2 + fract(aSeed * 3.1) * 1.4);

        // Intake turbulence (x < -0.9)
        float intakeMix = 1.0 - smoothstep(-1.15, -0.55, x);
        float pastGate = smoothstep(-0.65, 0.55, x);
        float exitMix = smoothstep(0.55, 1.35, x);

        float spreadR = (0.28 + fract(aSeed * 13.7) * 0.72) * intakeMix;
        float y = 0.68 + sin(angle) * spreadR + sin(x * 2.2 + aSeed * 19.0 + uTime * 2.0) * 0.14 * intakeMix;
        float z = cos(angle) * spreadR + cos(x * 1.9 + aSeed * 11.0 - uTime * 1.8) * 0.14 * intakeMix;

        vec3 redFlood = vec3(0.96, 0.25, 0.25);
        vec3 amberNeedle = vec3(1.0, 0.74, 0.18);
        vec3 cobaltDigest = vec3(0.24, 0.62, 1.0);
        vec3 slateStore = vec3(0.42, 0.54, 0.72);

        vec3 col = redFlood;
        float alpha = smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.88, 1.0, t));
        float size = mix(10.0, 18.0, fract(aSeed * 5.9));

        if (uMode < 0.5) {
          // MODE 0: STRAITJACKET CONTAINMENT
          if (aTrack < 0.5) {
            // Diverted into durable local artifact store underneath chamber
            float divert = smoothstep(-0.92, 0.15, x);
            float dropX = mix(x, -0.75 + (fract(aSeed * 17.3) - 0.5) * 1.1, divert);
            float dropY = mix(y, 0.08 + fract(aSeed * 9.1) * 0.16, divert);
            float dropZ = mix(z, (fract(aSeed * 23.7) - 0.5) * 0.95, divert);
            x = dropX;
            y = dropY;
            z = dropZ;
            col = mix(redFlood, slateStore, divert);
            alpha *= mix(0.75, 0.26, divert);
          } else if (aTrack < 1.5) {
            // Bounded 531-token cobalt laminar stream
            float tightR = 0.055 + fract(aSeed * 11.1) * 0.095;
            y = mix(y, 0.68 + sin(angle * 0.6) * tightR, pastGate);
            z = mix(z, cos(angle * 0.6) * tightR, pastGate);
            col = mix(redFlood, cobaltDigest, pastGate);
            alpha *= mix(0.7, 0.95, exitMix);
            size = mix(size, 15.0, exitMix);
          } else {
            // The quiet needle at line 14,238 — bright amber pulse preserved cleanly
            y = mix(y, 0.68 + sin(x * 3.0 + uTime * 3.0) * 0.03, pastGate);
            z = mix(z, cos(x * 3.0 + uTime * 3.0) * 0.03, pastGate);
            col = amberNeedle;
            alpha *= 1.0;
            size = 28.0;
          }
        } else if (uMode < 1.5) {
          // MODE 1: NAIVE RAW FLOOD — overwhelms the chamber and explodes into context
          float explode = smoothstep(-0.6, 2.2, x);
          float burstR = (0.35 + fract(aSeed * 13.7) * 1.85) * explode;
          y = 0.68 + sin(angle) * (spreadR + burstR) + sin(x * 3.0 + aSeed * 21.0 + uTime * 2.5) * 0.35 * explode;
          z = cos(angle) * (spreadR + burstR) + cos(x * 2.7 + aSeed * 15.0 - uTime * 2.2) * 0.35 * explode;
          col = (aTrack > 1.5) ? amberNeedle : mix(redFlood, vec3(1.0, 0.38, 0.16), fract(aSeed * 4.3));
          alpha *= 0.88;
          size = (aTrack > 1.5) ? 24.0 : 18.0;
        } else {
          // MODE 2: HEAD+TAIL TRUNCATION — middle sheared off, quiet needle LOST
          bool isMiddle = (aTrack > 1.5) || (fract(aSeed * 19.7) > 0.24 && fract(aSeed * 19.7) < 0.76);
          if (isMiddle) {
            float shear = smoothstep(-0.4, 0.45, x);
            y += sign(sin(aSeed * 31.0)) * shear * 2.1;
            z += sign(cos(aSeed * 27.0)) * shear * 1.8;
            col = mix(amberNeedle, redFlood, shear);
            alpha *= (1.0 - smoothstep(0.15, 0.55, shear));
          } else {
            float tightR = 0.14;
            y = mix(y, 0.68 + sin(angle) * tightR, pastGate);
            z = mix(z, cos(angle) * tightR, pastGate);
            col = mix(redFlood, vec3(0.65, 0.7, 0.78), pastGate);
            alpha *= 0.58;
          }
        }

        vColor = col;
        vAlpha = alpha;

        vec4 mvPos = modelViewMatrix * vec4(x, y, z, 1.0);
        gl_PointSize = size * uPixelRatio * (3.6 / max(-mvPos.z, 1.0));
        gl_Position = projectionMatrix * mvPos;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uOpacity;
      varying vec3 vColor;
      varying float vAlpha;

      void main() {
        vec2 uv = gl_PointCoord - 0.5;
        float d = length(uv);
        if (d > 0.5) discard;
        float core = exp(-d * d * 18.0);
        float halo = smoothstep(0.5, 0.08, d) * 0.55;
        vec3 finalCol = vColor * (halo + core * 1.6);
        gl_FragColor = vec4(finalCol, (halo + core) * vAlpha * uOpacity);
      }
    `,
  });
}

/* --------------------------------------------------------------------------
 * 4. GATE APERTURE OPTICAL SCAN RING SHADER
 * -------------------------------------------------------------------------- */
export function createGateApertureMaterial(index) {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uActive: { value: 0.35 },
      uColor: { value: new THREE.Color('#3b82f6') },
      uIndex: { value: index },
    },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform float uActive;
      uniform vec3 uColor;
      uniform float uIndex;
      varying vec2 vUv;

      void main() {
        vec2 p = vUv * 2.0 - 1.0;
        float r = length(p);
        if (r > 1.0 || r < 0.42) discard;

        float angle = atan(p.y, p.x);
        float ticks = smoothstep(0.55, 0.95, cos(angle * 24.0 + uTime * (0.7 + uIndex * 0.18)));
        float innerRing = exp(-abs(r - 0.66) * 28.0);
        float outerRing = exp(-abs(r - 0.92) * 32.0);
        float scanArc = smoothstep(0.6, 1.0, sin(angle * 2.0 - uTime * 2.2 + uIndex * 1.4));

        float intensity = (innerRing * (0.6 + 0.4 * ticks) + outerRing * 0.7 + scanArc * 0.28 * smoothstep(0.45, 0.9, r));
        float alpha = intensity * uActive;
        if (alpha < 0.01) discard;

        gl_FragColor = vec4(uColor * (1.1 + uActive * 0.6), clamp(alpha, 0.0, 0.95));
      }
    `,
  });
}

/* --------------------------------------------------------------------------
 * 5. ANCHOR OPTICAL SCAN PLANE & MULTI-HARNESS RELAY CONDUIT SHADERS
 * -------------------------------------------------------------------------- */
export function createScanBeamMaterial() {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uColor: { value: new THREE.Color('#22c55e') },
      uIntensity: { value: 0.75 },
    },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uColor;
      uniform float uIntensity;
      varying vec2 vUv;

      void main() {
        float edge = sin(vUv.x * 3.14159265) * sin(vUv.y * 3.14159265);
        float scan = smoothstep(0.82, 1.0, sin(vUv.y * 44.0 - uTime * 7.0));
        float sweep = exp(-abs(vUv.y - fract(uTime * 0.42)) * 18.0);
        float alpha = (0.14 + scan * 0.25 + sweep * 0.72) * pow(edge, 0.65) * uIntensity;
        gl_FragColor = vec4(uColor * 1.35, clamp(alpha, 0.0, 0.9));
      }
    `,
  });
}

export function createRelayArcMaterial() {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uColor: { value: new THREE.Color('#3b82f6') },
      uActive: { value: 0.85 },
      uPhase: { value: 0.0 },
    },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uColor;
      uniform float uActive;
      uniform float uPhase;
      varying vec2 vUv;

      void main() {
        float pulsePos = fract(uTime * 0.48 + uPhase);
        float d = min(abs(vUv.x - pulsePos), 1.0 - abs(vUv.x - pulsePos));
        float packet = exp(-d * 18.0);
        float packet2 = exp(-min(abs(vUv.x - fract(pulsePos + 0.5)), 1.0 - abs(vUv.x - fract(pulsePos + 0.5))) * 22.0) * 0.6;
        float baseTrack = 0.14;
        float alpha = (baseTrack + (packet + packet2) * 0.95) * uActive;
        vec3 col = mix(uColor, vec3(1.0, 0.85, 0.45), packet * 0.45);
        gl_FragColor = vec4(col * 1.25, clamp(alpha, 0.0, 0.95));
      }
    `,
  });
}

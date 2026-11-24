import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { Stage3D } from './scene/Stage3D.js';
import {
  EVIDENCE_FATES,
  FOUR_GATES,
  ANCHOR_SCENARIOS,
  HARNESSES,
  ACP_ROUTES,
  MEASURED_RECEIPTS,
} from './data/straitjacketData.js';

gsap.registerPlugin(ScrollTrigger);

/* --------------------------------------------------------------------------
 * 1. THREE.JS 3D STAGE BOOTSTRAP
 * -------------------------------------------------------------------------- */
const canvas = document.getElementById('stage');
const fallbackEl = document.getElementById('webgl-fallback');
const hudModelsEl = document.getElementById('hud-models');
const hudModelDotEl = document.getElementById('hud-model-dot');
const hudTokensEl = document.getElementById('hud-tokens');
const railFillEl = document.getElementById('rail-fill');

const stage = new Stage3D(canvas, {
  onWebGLFallback() {
    canvas.hidden = true;
    fallbackEl.hidden = false;
    if (hudModelsEl) hudModelsEl.textContent = '2D FALLBACK';
  },
  onModelLoaded(_key, count, total) {
    if (hudModelsEl) {
      hudModelsEl.textContent = count === total ? '3/3 GLB READY' : `LOADING ${count}/${total}`;
    }
    if (count === total && hudModelDotEl) {
      hudModelDotEl.classList.add('is-ready');
    }
  },
});

window.__straitjacketStage = stage;

/* --------------------------------------------------------------------------
 * 2. LENIS SMOOTH SCROLL + GSAP SCROLLTRIGGER CHOREOGRAPHY
 * -------------------------------------------------------------------------- */
const lenis = new Lenis({
  lerp: 0.085,
  smoothWheel: true,
  wheelMultiplier: 0.95,
});

lenis.on('scroll', ScrollTrigger.update);

gsap.ticker.add((time) => {
  lenis.raf(time * 1000);
});
gsap.ticker.lagSmoothing(0);

// Active chapter tracking for top nav & right rail
const chapters = Array.from(document.querySelectorAll('.chapter'));
const navLinks = Array.from(document.querySelectorAll('.hud-nav a'));
const railBtns = Array.from(document.querySelectorAll('.rail-btn'));

function getScrollProgress() {
  const maxScroll = Math.max(document.documentElement.scrollHeight - window.innerHeight, 1);
  return Math.min(Math.max(window.scrollY / maxScroll, 0), 1);
}

function syncChapterStops() {
  if (chapters.length !== 6) return;
  const maxScroll = Math.max(document.documentElement.scrollHeight - window.innerHeight, 1);
  const stops = chapters.map((sec, idx) => {
    if (idx === 0) return 0;
    if (idx === chapters.length - 1) return 1;
    return Math.min(Math.max(sec.offsetTop / maxScroll, 0.04), 0.96);
  });
  for (let i = 1; i < stops.length - 1; i++) {
    stops[i] = Math.max(stops[i], stops[i - 1] + 0.02);
  }
  stage.setChapterStops(stops);
}

function syncScrollState() {
  const p = getScrollProgress();
  stage.setScrollProgress(p);
  if (railFillEl) {
    railFillEl.style.height = `${(p * 100).toFixed(2)}%`;
  }
}

ScrollTrigger.create({
  trigger: '#scroll-root',
  start: 'top top',
  end: 'bottom bottom',
  scrub: true,
  onUpdate: (self) => {
    stage.setScrollProgress(self.progress);
    if (railFillEl) {
      railFillEl.style.height = `${(self.progress * 100).toFixed(2)}%`;
    }
  },
});

// Fallback direct scroll listener so programmatic window.scrollTo always updates immediately
window.addEventListener('scroll', syncScrollState, { passive: true });
window.addEventListener('resize', () => {
  ScrollTrigger.refresh();
  syncChapterStops();
  syncScrollState();
}, { passive: true });
window.addEventListener('load', () => {
  ScrollTrigger.refresh();
  syncChapterStops();
  syncScrollState();
});

function setActiveChapterUI(idx) {
  navLinks.forEach((a) => {
    a.classList.toggle('is-active', Number(a.dataset.chapter) === idx);
  });
  railBtns.forEach((b) => {
    b.classList.toggle('is-active', Number(b.dataset.chapter) === idx);
  });
}

chapters.forEach((sec) => {
  const idx = Number(sec.dataset.chapter);
  ScrollTrigger.create({
    trigger: sec,
    start: 'top 55%',
    end: 'bottom 45%',
    onEnter: () => setActiveChapterUI(idx),
    onEnterBack: () => setActiveChapterUI(idx),
  });

  // Subtle vertical settle animation on scroll entrance (keeps high legibility at all times)
  const target = sec.querySelector('.panel, .wide-container');
  if (target && idx > 0) {
    gsap.fromTo(
      target,
      { y: 22 },
      {
        y: 0,
        duration: 0.65,
        ease: 'power2.out',
        scrollTrigger: {
          trigger: sec,
          start: 'top 82%',
          once: true,
        },
      }
    );
  }
});

// Smooth jump navigation
document.querySelectorAll('[data-jump]').forEach((el) => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    const sel = el.getAttribute('data-jump');
    const dest = sel ? document.querySelector(sel) : null;
    if (dest) {
      lenis.scrollTo(dest, { offset: 0, duration: 1.15 });
    }
  });
});

/* --------------------------------------------------------------------------
 * 3. POPULATE HERO HARNESS ICONS & RECEIPTS GRID
 * -------------------------------------------------------------------------- */
const heroHarnessIcons = document.getElementById('hero-harness-icons');
if (heroHarnessIcons) {
  HARNESSES.forEach((h) => {
    const chip = document.createElement('span');
    chip.className = 'harness-chip';
    chip.innerHTML = `<img src="${h.icon}" alt="" width="18" height="18" class="${h.mono ? 'icon-mono' : ''}" /><span>${h.name}</span>`;
    heroHarnessIcons.appendChild(chip);
  });
}

const receiptsGrid = document.getElementById('receipts-grid');
if (receiptsGrid) {
  MEASURED_RECEIPTS.forEach((r) => {
    const card = document.createElement('article');
    card.className = 'receipt-card';
    card.innerHTML = `
      <div class="rc-workload">${r.workload}</div>
      <div class="rc-metric" data-tone="${r.tone}">${r.metric}</div>
      <p class="rc-detail">${r.detail}</p>
      <div class="rc-source">${r.receipt}</div>
    `;
    receiptsGrid.appendChild(card);
  });
}

/* --------------------------------------------------------------------------
 * 4. CHAPTER 1 INTERACTIVE: EVIDENCE FATES (302,628 vs 1,219 vs 531)
 * -------------------------------------------------------------------------- */
const fatesSwitcher = document.getElementById('fates-switcher');
const fateTokensEl = document.getElementById('fate-tokens');
const fateBadgeEl = document.getElementById('fate-badge');
const fateReductionEl = document.getElementById('fate-reduction');
const fateMeterEl = document.getElementById('fate-meter');
const fateNeedleEl = document.getElementById('fate-needle');
const fateAddressEl = document.getElementById('fate-address');
const fateSummaryEl = document.getElementById('fate-summary');

let activeFateId = 'straitjacket';

function renderFate(fateId) {
  const fate = EVIDENCE_FATES.find((f) => f.id === fateId) || EVIDENCE_FATES[0];
  activeFateId = fate.id;

  stage.setEvidenceMode(fate.modeIndex);

  fatesSwitcher.querySelectorAll('.seg-btn').forEach((btn) => {
    const active = btn.dataset.id === fate.id;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  });

  const tokStr = fate.tokens.toLocaleString('en-US');
  fateTokensEl.textContent = tokStr;
  fateTokensEl.style.color =
    fate.tone === 'red' ? 'var(--red-hi)' : 'var(--blue-hi)';

  if (hudTokensEl) {
    hudTokensEl.textContent = `${tokStr} tok`;
    hudTokensEl.style.color =
      fate.tone === 'red' ? 'var(--red-hi)' : 'var(--blue-hi)';
  }

  fateBadgeEl.textContent = fate.tag;
  fateBadgeEl.dataset.tone = fate.tone;
  fateReductionEl.textContent = fate.reduction;
  fateMeterEl.style.width = `${Math.max(fate.barPct, 1.2)}%`;
  fateMeterEl.style.background =
    fate.tone === 'red' ? 'var(--red-hi)' : 'var(--blue-hi)';
  fateNeedleEl.textContent = fate.quietNeedle;
  fateNeedleEl.style.color = fate.quietNeedle.includes('LOST')
    ? 'var(--red-hi)'
    : 'var(--green-hi)';
  fateAddressEl.textContent = fate.address;
  fateSummaryEl.textContent = fate.summary;
}

if (fatesSwitcher) {
  EVIDENCE_FATES.forEach((f) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'seg-btn';
    btn.dataset.id = f.id;
    btn.dataset.tone = f.tone;
    btn.setAttribute('role', 'tab');
    btn.textContent = f.label;
    btn.addEventListener('click', () => renderFate(f.id));
    fatesSwitcher.appendChild(btn);
  });
  renderFate(activeFateId);
}

/* --------------------------------------------------------------------------
 * 5. CHAPTER 2 INTERACTIVE: FOUR GATES SELECTOR
 * -------------------------------------------------------------------------- */
const gateSelector = document.getElementById('gate-selector');
const gateQuestionEl = document.getElementById('gate-question');
const gateDescEl = document.getElementById('gate-desc');
const gateCmdEl = document.getElementById('gate-cmd');
const gateCmdBtn = document.getElementById('gate-cmd-btn');

function renderGate(idx) {
  const g = FOUR_GATES[idx] || FOUR_GATES[0];
  stage.setActiveGate(idx);

  gateSelector.querySelectorAll('.gate-btn').forEach((btn, i) => {
    btn.classList.toggle('is-active', i === idx);
    btn.setAttribute('aria-selected', String(i === idx));
  });

  gateQuestionEl.textContent = `Gate ${g.num} (${g.name}) — ${g.question}`;
  gateDescEl.textContent = g.detail;
  gateCmdEl.textContent = g.command;
  gateCmdBtn.dataset.copy = g.command;
}

if (gateSelector) {
  FOUR_GATES.forEach((g, idx) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'gate-btn';
    btn.setAttribute('role', 'tab');
    btn.innerHTML = `<span class="g-num">GATE ${g.num}</span><span class="g-name">${g.name}</span>`;
    btn.addEventListener('click', () => renderGate(idx));
    gateSelector.appendChild(btn);
  });
  renderGate(0);
}

/* --------------------------------------------------------------------------
 * 6. CHAPTER 3 INTERACTIVE: CONTENT ANCHOR DRIFT SIMULATOR
 * -------------------------------------------------------------------------- */
const anchorSwitcher = document.getElementById('anchor-switcher');
const anchorStatusEl = document.getElementById('anchor-status');
const anchorDiffEl = document.getElementById('anchor-diff');
const anchorVerdictEl = document.getElementById('anchor-verdict');
const amAnchoredWrong = document.getElementById('am-anchored-wrong');
const amRelocated = document.getElementById('am-relocated');
const amRefused = document.getElementById('am-refused');
const amUnanchored = document.getElementById('am-unanchored');

function renderAnchorScenario(id) {
  const sc = ANCHOR_SCENARIOS.find((s) => s.id === id) || ANCHOR_SCENARIOS[0];
  stage.setAnchorScenario(sc);

  anchorSwitcher.querySelectorAll('.seg-btn').forEach((btn) => {
    const active = btn.dataset.id === sc.id;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  });

  anchorStatusEl.textContent = sc.status;
  anchorStatusEl.dataset.tone = sc.tone;
  anchorVerdictEl.textContent = sc.verdict;
  amAnchoredWrong.textContent = String(sc.anchoredWrong);
  amRelocated.textContent = String(sc.relocated);
  amRefused.textContent = String(sc.refused);
  amUnanchored.textContent = String(sc.unanchoredWrong);

  anchorDiffEl.innerHTML = sc.afterLines
    .map((line) => {
      const cls = line.highlight
        ? 'is-highlight'
        : line.inserted
        ? 'is-inserted'
        : line.rewritten
        ? 'is-rewritten'
        : '';
      return `<div class="diff-line ${cls}"><span class="diff-ln">L${line.n}</span><span>${line.code}</span></div>`;
    })
    .join('');
}

if (anchorSwitcher) {
  ANCHOR_SCENARIOS.forEach((sc) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'seg-btn';
    btn.dataset.id = sc.id;
    btn.dataset.tone = sc.tone;
    btn.setAttribute('role', 'tab');
    btn.textContent = sc.label;
    btn.addEventListener('click', () => renderAnchorScenario(sc.id));
    anchorSwitcher.appendChild(btn);
  });
  renderAnchorScenario(ANCHOR_SCENARIOS[0].id);
}

/* --------------------------------------------------------------------------
 * 7. CHAPTER 4 INTERACTIVE: ACP CROSS-HARNESS ROUTING
 * -------------------------------------------------------------------------- */
const routeSwitcher = document.getElementById('route-switcher');
const routeCmdEl = document.getElementById('route-cmd');
const routeStepsEl = document.getElementById('route-steps');
const harnessBayList = document.getElementById('harness-bay-list');

function renderRoute(id) {
  const route = ACP_ROUTES.find((r) => r.id === id) || ACP_ROUTES[0];
  stage.setActiveRoute(route.activeBays);

  routeSwitcher.querySelectorAll('.seg-btn').forEach((btn) => {
    const active = btn.dataset.id === route.id;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  });

  routeCmdEl.textContent = `$ ${route.cmd}`;
  routeStepsEl.innerHTML = route.steps
    .map(
      (s) => `
      <li class="route-step">
        <span class="rs-stage">${s.stage}</span>
        <div class="rs-meta">
          <div class="rs-top">
            <strong>${s.harness}</strong>
            <span class="rs-tier">${s.tier}</span>
          </div>
          <span class="rs-handoff">checkpoint → ${s.handoff}</span>
        </div>
      </li>
    `
    )
    .join('');

  harnessBayList.querySelectorAll('.bay-pill').forEach((pill) => {
    const bay = Number(pill.dataset.bay);
    pill.classList.toggle('is-active', route.activeBays.includes(bay));
  });
}

if (harnessBayList) {
  HARNESSES.forEach((h) => {
    const pill = document.createElement('span');
    pill.className = 'bay-pill';
    pill.dataset.bay = String(h.bay);
    pill.innerHTML = `<img src="${h.icon}" alt="" width="15" height="15" class="${h.mono ? 'icon-mono' : ''}" /><span>${h.name}</span>`;
    harnessBayList.appendChild(pill);
  });
}

if (routeSwitcher) {
  ACP_ROUTES.forEach((r) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'seg-btn';
    btn.dataset.id = r.id;
    btn.setAttribute('role', 'tab');
    btn.textContent = r.label;
    btn.addEventListener('click', () => renderRoute(r.id));
    routeSwitcher.appendChild(btn);
  });
  renderRoute(ACP_ROUTES[0].id);
}

/* --------------------------------------------------------------------------
 * 8. ONE-CLICK COPY FOR CLI COMMANDS
 * -------------------------------------------------------------------------- */
document.querySelectorAll('[data-copy]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const text = btn.dataset.copy;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for insecure/offline contexts
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    const tag = btn.querySelector('.copy-tag');
    btn.classList.add('is-copied');
    if (tag) tag.textContent = 'COPIED';
    setTimeout(() => {
      btn.classList.remove('is-copied');
      if (tag) tag.textContent = 'COPY';
    }, 1500);
  });
});

// Final layout sync once all dynamic grids & panels are rendered
syncChapterStops();
syncScrollState();
ScrollTrigger.refresh();

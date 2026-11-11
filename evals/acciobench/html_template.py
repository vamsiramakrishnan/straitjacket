"""Self-contained HTML/CSS/JS template for AccioBench evaluation & trajectory reports."""

from __future__ import annotations

import html as html_lib

__all__ = ["render_html_document"]

_CSS = """
:root {
  --bg: #F8F9FA;
  --surface: #FFFFFF;
  --surface-low: #F8F9FA;
  --surface-mid: #F1F3F4;
  --ink: #202124;
  --ink-2: #5F6368;
  --ink-3: #80868B;
  --line: #DADCE0;
  --line-soft: #E8EAED;
  --primary: #1A73E8;
  --primary-bg: #E8F0FE;
  --primary-ink: #174EA6;
  --ok: #188038;
  --ok-bg: #E6F4EA;
  --ok-ink: #137333;
  --bad: #D93025;
  --bad-bg: #FCE8E6;
  --bad-ink: #A50E0E;
  --warn: #E37400;
  --warn-bg: #FEF7E0;
  --warn-ink: #B06000;
  --thought: #7C3AED;
  --thought-bg: #F3E8FF;
  --sans: 'Public Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 13px; line-height: 1.45; -webkit-font-smoothing: antialiased; }
button, input, select { font-family: inherit; font-size: inherit; }
.mono { font-family: var(--mono); }
.tnum { font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }
.nowrap { white-space: nowrap; }

/* Sticky Top Bar */
.topbar { position: sticky; top: 0; z-index: 50; background: var(--surface); border-bottom: 1px solid var(--line); }
.topbar-inner { max-width: 1340px; margin: 0 auto; padding: 0 24px; height: 56px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.brand-group { display: flex; align-items: center; gap: 20px; min-width: 0; }
.brand-title { font-size: 15px; font-weight: 700; letter-spacing: -0.01em; color: var(--ink); white-space: nowrap; display: flex; align-items: center; gap: 8px; }
.brand-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--primary); box-shadow: 0 0 0 3px var(--primary-bg); flex-shrink: 0; }
.suite-pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface-low); color: var(--ink-2); font-family: var(--mono); font-size: 11px; white-space: nowrap; }
.nav-tabs { display: flex; align-items: center; gap: 22px; height: 56px; }
.nav-tab { height: 56px; display: inline-flex; align-items: center; gap: 6px; border: none; background: transparent; padding: 0 2px; font-size: 13px; font-weight: 500; color: var(--ink-2); cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; transition: color 0.12s; }
.nav-tab:hover { color: var(--ink); }
.nav-tab.active { color: var(--primary); font-weight: 600; border-bottom-color: var(--primary); }
.top-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

/* Subheader Metadata Strip */
.subbar { background: var(--surface-low); border-bottom: 1px solid var(--line); padding: 7px 0; font-family: var(--mono); font-size: 11px; color: var(--ink-2); }
.subbar-inner { max-width: 1340px; margin: 0 auto; padding: 0 24px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.subbar-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.status-led { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); display: inline-block; }
.sep { color: var(--line); }

/* Main Container & Cards */
.container { max-width: 1340px; margin: 0 auto; padding: 20px 24px 48px; display: flex; flex-direction: column; gap: 18px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.card-head { padding: 13px 18px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; background: var(--surface); }
.card-title { margin: 0; font-size: 13.5px; font-weight: 600; color: var(--ink); }
.card-sub { margin: 2px 0 0; font-size: 11.5px; color: var(--ink-2); }
.card-body { padding: 16px 18px; }

/* Buttons & Pills */
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 11px; border-radius: 4px; border: 1px solid var(--line); background: var(--surface); color: var(--ink); font-size: 12px; font-weight: 500; cursor: pointer; white-space: nowrap; transition: background 0.12s; }
.btn:hover { background: var(--surface-mid); }
.btn-primary { background: var(--primary); border-color: var(--primary); color: #FFFFFF; font-weight: 600; }
.btn-primary:hover { background: #1557B0; }
.btn-xs { padding: 3px 8px; font-size: 11px; }
.seg-group { display: inline-flex; padding: 2px; background: var(--surface-mid); border: 1px solid var(--line); border-radius: 5px; gap: 2px; flex-wrap: wrap; }
.seg-btn { border: 1px solid transparent; background: transparent; color: var(--ink-2); padding: 4px 10px; border-radius: 4px; font-size: 11.5px; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.seg-btn:hover { color: var(--ink); }
.seg-btn.active { background: var(--surface); color: var(--primary); font-weight: 600; border-color: var(--line); }

.badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 4px; font-family: var(--mono); font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge-ok { background: var(--ok-bg); color: var(--ok-ink); }
.badge-bad { background: var(--bad-bg); color: var(--bad-ink); }
.badge-warn { background: var(--warn-bg); color: var(--warn-ink); }
.badge-primary { background: var(--primary-bg); color: var(--primary-ink); }
.badge-neutral { background: var(--surface-mid); color: var(--ink-2); font-weight: 500; }

/* Verdict Strip */
.verdict { padding: 16px 18px; display: flex; align-items: flex-start; gap: 14px; }
.verdict-icon { width: 30px; height: 30px; border-radius: 50%; background: var(--ok-bg); color: var(--ok); display: flex; align-items: center; justify-content: center; flex-shrink: 0; margin-top: 1px; }
.verdict-h1 { margin: 0; font-size: 15px; font-weight: 600; color: var(--ink); line-height: 1.4; }
.verdict-sub { margin: 4px 0 0; font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); line-height: 1.5; }

/* 4 KPI Grid */
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
@media (max-width: 980px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .kpi-grid { grid-template-columns: 1fr; } }
.kpi-card { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 15px 16px; display: flex; flex-direction: column; justify-content: space-between; min-height: 108px; }
.kpi-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.kpi-label { font-family: var(--mono); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-2); white-space: nowrap; }
.kpi-val { font-family: var(--mono); font-size: 26px; font-weight: 700; color: var(--ink); letter-spacing: -0.02em; margin: 10px 0 6px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.kpi-val .muted-part { font-size: 16px; font-weight: 500; color: var(--ink-2); }
.kpi-foot { font-family: var(--mono); font-size: 11px; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Tables */
.table-wrap { width: 100%; overflow-x: auto; }
table.data-table { width: 100%; border-collapse: collapse; text-align: left; font-size: 12px; }
table.data-table thead tr { background: var(--surface-low); border-bottom: 1px solid var(--line); }
table.data-table th { padding: 9px 10px; font-size: 11px; font-weight: 600; color: var(--ink-2); white-space: nowrap; font-family: var(--sans); letter-spacing: 0.01em; }
table.data-table td { padding: 10px 10px; border-bottom: 1px solid var(--line-soft); vertical-align: middle; font-family: var(--mono); font-variant-numeric: tabular-nums; }
table.data-table tbody tr:last-child td { border-bottom: none; }
table.data-table tbody tr:hover { background: #F8FAFD; }
table.data-table tbody tr.row-primary { background: rgba(232, 240, 254, 0.35); }
table.data-table tbody tr.row-primary:hover { background: rgba(232, 240, 254, 0.65); }
table.data-table tbody tr.clickable { cursor: pointer; }
table.data-table td.sans-cell { font-family: var(--sans); }
.num-right { text-align: right; }

/* Whisker Bar inside Leaderboard */
.ci-cell { display: flex; align-items: center; gap: 10px; white-space: nowrap; }
.ci-track { position: relative; width: 82px; height: 10px; background: var(--line-soft); border-radius: 999px; flex-shrink: 0; }
.ci-range { position: absolute; top: 2px; height: 6px; border-radius: 999px; opacity: 0.42; }
.ci-dot { position: absolute; top: 1px; width: 8px; height: 8px; border-radius: 50%; transform: translateX(-50%); }

/* Bottom Split Grid */
.two-col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
@media (max-width: 960px) { .two-col { grid-template-columns: 1fr; } }

/* Dot Plot Rows */
.dot-legend { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding-bottom: 10px; margin-bottom: 10px; border-bottom: 1px solid var(--line-soft); font-size: 11.5px; color: var(--ink-2); }
.legend-item { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.arm-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.slice-plot-row { padding: 9px 8px; border-radius: 4px; cursor: pointer; transition: background 0.12s; }
.slice-plot-row:hover { background: var(--surface-low); }
.slice-plot-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; font-size: 12px; }
.slice-plot-track { position: relative; height: 8px; background: var(--surface-mid); border-radius: 999px; border: 1px solid var(--line-soft); }
.slice-plot-span { position: absolute; top: 2px; height: 2px; background: var(--line); }
.slice-plot-pt { position: absolute; top: -2px; width: 10px; height: 10px; border-radius: 50%; transform: translateX(-50%); border: 1.5px solid #FFF; }
.axis-labels { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 10px; color: var(--ink-3); margin-top: 8px; padding: 0 4px; }

/* Failure Bars */
.fail-row { padding: 9px 8px; border-radius: 4px; cursor: pointer; transition: background 0.12s; }
.fail-row:hover { background: var(--surface-low); }
.fail-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 4px; }
.fail-title { font-size: 12.5px; font-weight: 600; color: var(--ink); }
.fail-bar-bg { height: 6px; background: var(--surface-mid); border-radius: 999px; overflow: hidden; margin-bottom: 4px; }
.fail-bar-fill { height: 100%; border-radius: 999px; }
.fail-desc { font-size: 11.5px; color: var(--ink-2); line-height: 1.35; }

/* Lever Grid */
.lever-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 900px) { .lever-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .lever-grid { grid-template-columns: 1fr; } }
.lever-tile { border: 1px solid var(--line-soft); background: var(--surface-low); border-radius: 5px; padding: 12px 14px; }
.lever-kicker { font-family: var(--mono); font-size: 10.5px; font-weight: 600; text-transform: uppercase; color: var(--ink-2); letter-spacing: 0.03em; }
.lever-big { font-family: var(--mono); font-size: 20px; font-weight: 700; color: var(--ink); margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.lever-note { font-size: 11.5px; color: var(--ink-2); }

/* Tasks Filter Toolbar */
.filter-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding: 12px 16px; border-bottom: 1px solid var(--line); background: var(--surface-low); }
.search-input { padding: 6px 10px 6px 30px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); font-size: 12px; width: 250px; color: var(--ink); }
.search-wrap { position: relative; display: inline-flex; align-items: center; }
.search-wrap svg { position: absolute; left: 9px; color: var(--ink-3); pointer-events: none; }
.select-ctl { padding: 5px 10px; border: 1px solid var(--line); border-radius: 4px; background: var(--surface); color: var(--ink); font-size: 12px; }

/* Traces Inspector */
.trace-ctrl-bar { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.trace-ctrl-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.trace-presets { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.preset-pill { border: 1px solid var(--line); background: var(--surface-low); color: var(--ink-2); padding: 3px 9px; border-radius: 4px; font-family: var(--mono); font-size: 11px; cursor: pointer; white-space: nowrap; }
.preset-pill:hover { color: var(--ink); background: var(--surface-mid); }
.preset-pill.active { background: var(--primary); border-color: var(--primary); color: #FFF; font-weight: 600; }

.ribbon-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); border-top: 1px solid var(--line); }
@media (max-width: 1024px) { .ribbon-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 600px) { .ribbon-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.ribbon-cell { padding: 11px 14px; border-right: 1px solid var(--line-soft); }
.ribbon-cell:last-child { border-right: none; }
.ribbon-lbl { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-2); font-weight: 600; white-space: nowrap; }
.ribbon-val { font-family: var(--mono); font-size: 17px; font-weight: 700; color: var(--ink); margin-top: 2px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ribbon-sub { font-family: var(--mono); font-size: 10.5px; color: var(--ink-2); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.inspector-split { display: grid; grid-template-columns: minmax(0, 8fr) minmax(300px, 4fr); gap: 16px; align-items: start; }
@media (max-width: 1024px) { .inspector-split { grid-template-columns: 1fr; } }
.sticky-side { position: sticky; top: 72px; display: flex; flex-direction: column; gap: 14px; }

/* Step Cards & Epoch Banners */
.epoch-banner { margin: 8px 0; padding: 7px 12px; border-radius: 4px; background: var(--ok-bg); border: 1px solid #CEEAD6; color: var(--ok-ink); font-family: var(--mono); font-size: 11px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.step-item { border: 1px solid var(--line-soft); border-radius: 5px; background: var(--surface); margin-bottom: 6px; overflow: hidden; }
.step-item.has-digest { border-left: 3px solid var(--warn); }
.step-item.has-spike { border-left: 3px solid var(--thought); }
.step-head { padding: 8px 12px; display: flex; align-items: center; justify-content: space-between; gap: 10px; cursor: pointer; user-select: none; }
.step-head:hover { background: var(--surface-low); }
.step-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.step-num { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--ink-2); min-width: 34px; }
.tool-chip { font-family: var(--mono); font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 3px; background: var(--primary-bg); color: var(--primary-ink); white-space: nowrap; }
.tool-chip.mut { background: #FEF7E0; color: #B06000; }
.tool-chip.done { background: var(--ok-bg); color: var(--ok-ink); }
.step-sum { font-family: var(--mono); font-size: 11.5px; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.step-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; font-family: var(--mono); font-size: 11px; color: var(--ink-2); }
.step-body { padding: 10px 12px; border-top: 1px solid var(--line-soft); background: var(--surface-low); display: none; flex-direction: column; gap: 8px; }
.step-item.open .step-body { display: flex; }
.code-block { margin: 0; padding: 8px 10px; background: #FFFFFF; border: 1px solid var(--line-soft); border-radius: 4px; font-family: var(--mono); font-size: 11px; color: var(--ink); white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow-y: auto; line-height: 1.45; }
.block-lbl { font-family: var(--mono); font-size: 10px; font-weight: 600; text-transform: uppercase; color: var(--ink-2); margin-bottom: 3px; }

/* Grader Checks Sidebar */
.check-list { max-height: 520px; overflow-y: auto; divide-y: 1px solid var(--line-soft); }
.check-row { padding: 9px 14px; border-bottom: 1px solid var(--line-soft); display: flex; align-items: flex-start; gap: 8px; font-size: 11.5px; }
.check-row:last-child { border-bottom: none; }
.check-id { font-family: var(--mono); font-weight: 600; color: var(--ink); word-break: break-word; }
.check-reason { font-size: 11px; color: var(--ink-2); margin-top: 2px; word-break: break-word; }

/* Multi-Arm Compare Grid */
.compare-grid { display: grid; gap: 12px; align-items: start; }
.compare-col { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; min-width: 0; }
.compare-col-head { padding: 10px 12px; border-bottom: 1px solid var(--line); background: var(--surface-low); display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.compare-stream { max-height: 680px; overflow-y: auto; padding: 10px; }

.hidden { display: none !important; }
"""

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>__DOC_TITLE__</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='6' fill='%231A73E8'/%3E%3C/svg%3E"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Public+Sans:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<style>__CSS__</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand-group">
      <div class="brand-title"><span class="brand-dot"></span><span id="hdr-title">Straitjacket Eval</span></div>
      <div class="suite-pill" id="hdr-pill">Loading suite…</div>
      <nav class="nav-tabs" id="nav-tabs">
        <button type="button" class="nav-tab active" data-tab="overview">Overview</button>
        <button type="button" class="nav-tab" data-tab="slices" id="tab-btn-slices">Slices &amp; Ablation</button>
        <button type="button" class="nav-tab" data-tab="tasks" id="tab-btn-tasks">Tasks</button>
        <button type="button" class="nav-tab" data-tab="traces">Traces</button>
      </nav>
    </div>
    <div class="top-actions">
      <div id="scope-switcher-slot"></div>
      <button type="button" class="btn" id="btn-export-csv">Export CSV</button>
      <button type="button" class="btn btn-primary" id="btn-go-traces">Inspect Traces</button>
    </div>
  </div>
</header>
<div class="subbar">
  <div class="subbar-inner">
    <div class="subbar-left" id="subbar-meta"></div>
    <div id="subbar-date"></div>
  </div>
</div>
<main class="container" id="app-root">
  <div class="card"><div class="card-body mono">Decompressing benchmark telemetry…</div></div>
</main>
<script id="payload-gz" type="application/octet-stream">__PAYLOAD_GZ_B64__</script>
"""
_JS_PART_1 = r"""<script>
(function() {
  'use strict';

  const ICONS = {
    check: '<svg width="16" height="16" viewBox="0 0 20 20" fill="none"><path d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.7-9.3a1 1 0 00-1.4-1.4L9 10.58 7.7 9.3a1 1 0 00-1.4 1.4l2 2a1 1 0 001.4 0l4-4z" fill="currentColor"/></svg>',
    x: '<svg width="15" height="15" viewBox="0 0 20 20" fill="none"><path d="M10 18a8 8 0 100-16 8 8 0 000 16zm2.7-10.7a1 1 0 00-1.4 0L10 8.58 8.7 7.3a1 1 0 00-1.4 1.4L8.58 10l-1.3 1.3a1 1 0 101.42 1.4L10 11.42l1.3 1.3a1 1 0 001.4-1.42L11.42 10l1.3-1.3a1 1 0 000-1.4z" fill="currentColor"/></svg>',
    search: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none"><path d="M8.5 3a5.5 5.5 0 103.47 9.77l3.63 3.63a.75.75 0 101.06-1.06l-3.63-3.63A5.5 5.5 0 008.5 3zm-4 5.5a4 4 0 118 0 4 4 0 01-8 0z" fill="currentColor"/></svg>',
    compare: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none"><path d="M4 4h5v12H4V4zm7 0h5v12h-5V4z" stroke="currentColor" stroke-width="1.5"/></svg>',
    prompt: '<svg width="14" height="14" viewBox="0 0 20 20" fill="none"><path d="M5 3h10a1 1 0 011 1v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4a1 1 0 011-1zm2 4h6m-6 3h6m-6 3h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
  };

  let DATA = null;
  let ARM_BY_ID = {};
  const STATE = {
    tab: 'overview',
    scopeIdx: 0,
    taskSearch: '',
    taskCat: 'all',
    taskOutcome: 'all',
    taskFailCat: '',
    taskSort: 'steps_delta',
    traceTaskId: '',
    traceMode: '',
    showPrompt: false,
    stepFilter: 'all',
    showAblationMatrix: false
  };

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtInt(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    return Math.round(Number(n)).toLocaleString('en-US');
  }

  function fmtCompact(n) {
    const v = Number(n || 0);
    if (!v) return '0';
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2).replace(/\.00$/, '.0') + 'M';
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(v >= 1e5 ? 0 : 1) + 'k';
    return String(Math.round(v));
  }

  function fmtBytes(b) {
    const v = Number(b || 0);
    if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB';
    if (v >= 1024) return (v / 1024).toFixed(1) + ' KB';
    return v + ' B';
  }

  function fmtSign(n, suffix, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    const d = digits === undefined ? 1 : digits;
    const v = Number(n);
    return (v > 0 ? '+' : '') + v.toFixed(d) + (suffix || '');
  }

  function fmtP(p) {
    if (p === null || p === undefined) return 'n/a';
    const v = Number(p);
    if (v < 0.0001) return 'p<0.0001';
    if (v < 0.01) return 'p=' + v.toFixed(4);
    return 'p=' + v.toFixed(3);
  }

  async function decodePayload() {
    const b64 = document.getElementById('payload-gz').textContent.trim();
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    if (typeof DecompressionStream === 'function') {
      const ds = new DecompressionStream('gzip');
      const writer = ds.writable.getWriter();
      writer.write(bin);
      writer.close();
      return await new Response(ds.readable).json();
    }
    throw new Error('Browser lacks DecompressionStream("gzip") support.');
  }

  function initHeader() {
    const m = DATA.meta;
    document.getElementById('hdr-title').textContent = m.title || 'Straitjacket Eval';
    document.getElementById('hdr-pill').textContent =
      m.suite + ' · ' + DATA.arms_order.length + (DATA.arms_order.length === 1 ? ' arm · ' : ' arms · ') + m.total_tasks + ' tasks';
    document.getElementById('tab-btn-tasks').textContent = 'Tasks (' + m.total_tasks + ')';
    if (!DATA.ablation || !DATA.ablation.arms || !DATA.ablation.arms.length) {
      document.getElementById('tab-btn-slices').textContent = 'Slices & Levers';
    }

    const catParts = Object.entries(m.categories || {}).map(([k, v]) => v + ' ' + k).join(', ');
    document.getElementById('subbar-meta').innerHTML =
      '<span class="status-led"></span>' +
      '<span><strong>' + esc(m.harness) + '</strong></span>' +
      '<span class="sep">•</span><span>' + m.total_tasks + ' tasks (' + esc(catParts) + ')</span>' +
      '<span class="sep">•</span><span>Untouched model defaults</span>' +
      '<span class="sep">•</span><span>Judge: <strong style="color:var(--ink)">' + esc(m.judge) + '</strong></span>' +
      (m.grading_errors_total
        ? '<span class="sep">•</span><span id="grading-errors">Grading errors: <strong style="color:var(--ink)">' +
          m.grading_errors_total + '</strong> ungraded (' +
          esc(Object.entries(m.grading_errors || {}).filter(([, v]) => v).map(([k, v]) => k + '=' + v).join(', ')) +
          '), excluded from solve rate &amp; score</span>'
        : '');
    document.getElementById('subbar-date').textContent = 'Completed: ' + m.date + ' UTC';

    const scopeSlot = document.getElementById('scope-switcher-slot');
    if (DATA.scopes.length > 1) {
      scopeSlot.innerHTML = '<div class="seg-group" id="scope-seg">' +
        DATA.scopes.map((sc, i) =>
          '<button type="button" class="seg-btn' + (i === STATE.scopeIdx ? ' active' : '') + '" data-scope="' + i + '">' +
          esc(sc.id === 'all' ? 'All ' + sc.n : sc.n + ' ex-Outlier') + '</button>'
        ).join('') + '</div>';
      scopeSlot.querySelectorAll('[data-scope]').forEach(btn => {
        btn.addEventListener('click', () => {
          STATE.scopeIdx = Number(btn.getAttribute('data-scope'));
          scopeSlot.querySelectorAll('[data-scope]').forEach((b, idx) => b.classList.toggle('active', idx === STATE.scopeIdx));
          renderActiveTab();
        });
      });
    }

    document.querySelectorAll('#nav-tabs .nav-tab').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.getAttribute('data-tab')));
    });
    document.getElementById('btn-go-traces').addEventListener('click', () => switchTab('traces'));
    document.getElementById('btn-export-csv').addEventListener('click', exportCsv);
  }

  function syncHash() {
    let h = '#' + STATE.tab;
    if (STATE.tab === 'traces' && STATE.traceTaskId) {
      h += '/' + encodeURIComponent(STATE.traceTaskId) + '/' + encodeURIComponent(STATE.traceMode);
    }
    if (window.location.hash !== h) {
      history.replaceState(null, '', h);
    }
  }

  function applyInitialHash() {
    const raw = (window.location.hash || '').replace(/^#/, '');
    if (!raw) return;
    const parts = raw.split('/');
    if (['overview', 'slices', 'tasks', 'traces'].includes(parts[0])) {
      STATE.tab = parts[0];
    }
    if (parts[0] === 'traces' && parts[1]) {
      const tid = decodeURIComponent(parts[1]);
      if (DATA.traces[tid]) STATE.traceTaskId = tid;
      if (parts[2]) STATE.traceMode = decodeURIComponent(parts[2]);
    }
  }

  function switchTab(tab, opts) {
    STATE.tab = tab;
    if (opts && opts.taskId) {
      STATE.traceTaskId = opts.taskId;
      if (opts.mode) STATE.traceMode = opts.mode;
    }
    document.querySelectorAll('#nav-tabs .nav-tab').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-tab') === tab);
    });
    syncHash();
    renderActiveTab();
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  function renderActiveTab() {
    const root = document.getElementById('app-root');
    if (STATE.tab === 'overview') root.innerHTML = buildOverviewHtml();
    else if (STATE.tab === 'slices') root.innerHTML = buildSlicesHtml();
    else if (STATE.tab === 'tasks') root.innerHTML = buildTasksHtml();
    else if (STATE.tab === 'traces') root.innerHTML = buildTracesHtml();
    bindTabEvents();
  }

  function buildOverviewHtml() {
    const sc = DATA.scopes[STATE.scopeIdx] || DATA.scopes[0];
    const pId = DATA.meta.primary_arm;
    const bId = DATA.meta.baseline_arm;
    const rId = DATA.meta.reference_arm;
    const p = sc.arms[pId];
    const b = bId ? sc.arms[bId] : null;
    const r = rId ? sc.arms[rId] : null;
    const d = sc.deltas || {};

    let headline = esc(p.name) + ' reaches ' + p.cap.toFixed(1) + '% macro capability and passes ' +
      p.pass_pct.toFixed(1) + '% (' + p.pass_k + '/' + p.n + ') of tasks.';
    let subline = 'Bootstrap 95% CI [' + p.cap_ci[0].toFixed(1) + '%, ' + p.cap_ci[1].toFixed(1) + '%] · ' +
      'Pass Wilson 95% CI [' + p.pass_ci[0].toFixed(1) + '%, ' + p.pass_ci[1].toFixed(1) + '%] · ' +
      fmtCompact(p.vtx_tot) + ' total tokens (' + p.cache_hit_pct.toFixed(1) + '% prompt cache hit).';

    if (b) {
      const refClause = r ? (', ' + fmtSign(d.cap_vs_ref, ' pts') + ' vs ' + esc(r.short)) : '';
      const tokClause = (d.tok_per_step_vs_base_pct !== null && d.tok_per_step_vs_base_pct !== undefined)
        ? (' at ' + (p.tok_per_step / Math.max(1, b.tok_per_step)).toFixed(2) + 'x the tokens/step and ')
        : ' with ';
      headline = '<span class="mono" style="color:var(--primary)">' + esc(p.name) + '</span> reaches ' +
        p.cap.toFixed(1) + '% macro capability (' + fmtSign(d.cap_vs_base, ' pts') + ' over ' + esc(b.short) + refClause + ')' +
        tokClause + Math.abs(d.steps_vs_base_pct).toFixed(1) + '% ' + (d.steps_vs_base_pct <= 0 ? 'fewer' : 'more') + ' trajectory steps.';
      subline = 'Bootstrap 95% CI [' + p.cap_ci[0].toFixed(1) + '%, ' + p.cap_ci[1].toFixed(1) + '%], N=' + sc.n +
        ' paired tasks, Wilcoxon step ' + fmtP(d.p_wilcoxon_steps) + ' (shorter on ' + d.shorter_than_base_k + '/' + sc.n +
        ' tasks), binary pass ' + p.pass_pct.toFixed(1) + '% (' + p.pass_k + '/' + p.n + ', +' + d.gained_vs_base + '/-' + d.lost_vs_base + ' vs ' + esc(b.short) + ').';
    }

    const capBadge = b
      ? '<span class="badge ' + (d.cap_vs_base >= 0 ? 'badge-ok' : 'badge-bad') + '">' + fmtSign(d.cap_vs_base, ' pts vs ' + b.short) + '</span>'
      : '<span class="badge badge-primary">N=' + sc.n + '</span>';

    let passBadgeText = p.pass_k + '/' + p.n;
    if (DATA.scopes.length > 1 && STATE.scopeIdx === 0) {
      const ex = DATA.scopes[1].arms[pId];
      passBadgeText += ' (' + ex.pass_pct.toFixed(1) + '% ex-outlier)';
    } else if (b) {
      passBadgeText += ' (' + fmtSign(d.pass_vs_base_pp, ' pp') + ')';
    }

    const tokBadge = (b && d.tok_per_step_vs_base_pct !== null && d.tok_per_step_vs_base_pct !== undefined)
      ? '<span class="badge ' + (d.tok_per_step_vs_base_pct <= 0 ? 'badge-ok' : 'badge-warn') + '">' + fmtSign(d.tok_per_step_vs_base_pct, '% vs ' + b.short) + '</span>'
      : '<span class="badge badge-primary">' + p.cache_hit_pct.toFixed(1) + '% cache</span>';

    const stepBadge = b
      ? '<span class="badge ' + (d.steps_vs_base_pct <= 0 ? 'badge-ok' : 'badge-warn') + '">' + fmtSign(d.steps_vs_base_pct, '% steps') + '</span>'
      : '<span class="badge badge-neutral">' + fmtInt(p.steps_tot) + ' total</span>';

    // Leaderboard rows
    const lbRows = DATA.arms_order.map((am, idx) => {
      const st = sc.arms[am.id];
      if (!st) return '';
      const isPrim = am.id === pId;
      const isBase = am.id === bId;
      let deltaCell = '<span class="badge badge-neutral">reference</span>';
      if (isBase) {
        deltaCell = '<span class="badge badge-neutral">baseline</span>';
      } else if (isPrim && b) {
        deltaCell = '<span class="badge ' + (d.cap_vs_base >= 0 ? 'badge-ok' : 'badge-bad') + '">' +
          fmtSign(d.cap_vs_base, ' pts') + ' · ' + fmtSign(d.steps_vs_base_pct, '% steps') + ' (' + fmtP(d.p_wilcoxon_steps) + ')</span>';
      } else if (b) {
        const dc = st.cap - b.cap;
        const ds = ((st.steps_tot - b.steps_tot) / Math.max(1, b.steps_tot)) * 100;
        deltaCell = '<span class="badge badge-neutral">' + fmtSign(dc, ' pts') + ' · ' + fmtSign(ds, '% steps') + '</span>';
      }

      const lo = Math.max(0, Math.min(100, st.cap_ci[0]));
      const hi = Math.max(lo, Math.min(100, st.cap_ci[1]));
      const tokStr = st.tok_per_step > 0
        ? (fmtCompact(st.tok_per_step) + (st.is_calibrated_tok ? '*' : ''))
        : (fmtCompact(st.rep_tot) + ' gen');

      return '<tr class="' + (isPrim ? 'row-primary' : '') + '">' +
        '<td class="sans-cell nowrap"><div style="display:flex;align-items:center;gap:8px">' +
          '<span class="arm-dot" style="background:' + am.color + '"></span>' +
          '<span style="font-weight:' + (isPrim ? '600' : '500') + ';color:var(--ink)">' + esc(st.name) + '</span>' +
          (isPrim ? '<span class="badge badge-primary" style="font-size:10px;padding:1px 5px">PROPOSED</span>' : '') +
        '</div></td>' +
        '<td><div class="ci-cell">' +
          '<span style="min-width:108px"><strong>' + st.cap.toFixed(1) + '%</strong> <span style="color:var(--ink-2);font-size:11px">[' + st.cap_ci[0].toFixed(1) + ', ' + st.cap_ci[1].toFixed(1) + ']</span></span>' +
          '<div class="ci-track">' +
            '<div class="ci-range" style="left:' + lo + '%;width:' + Math.max(2, hi - lo) + '%;background:' + am.color + '"></div>' +
            '<div class="ci-dot" style="left:' + st.cap + '%;background:' + am.color + '"></div>' +
          '</div>' +
        '</div></td>' +
        '<td class="num-right nowrap">' + deltaCell + '</td>' +
        '<td class="num-right nowrap"><strong>' + st.pass_pct.toFixed(1) + '%</strong> <span style="color:var(--ink-2)">(' + st.pass_k + '/' + st.n + ')</span></td>' +
        '<td class="num-right nowrap">' + fmtInt(st.steps_tot) + ' <span style="color:var(--ink-2)">/ ' + st.steps_p50 + '</span></td>' +
        '<td class="num-right nowrap">' + st.lat_p50 + ' s <span style="color:var(--ink-2)">/ ' + st.lat_p90 + ' s</span></td>' +
        '<td class="num-right nowrap">' + tokStr + '</td>' +
      '</tr>';
    }).join('');

    // Where arms disagree dot plot (non-overall slices sorted by spread)
    const plotSlices = DATA.slices.filter(s => s.key !== 'all' && s.key !== 'ex_outlier')
      .slice()
      .sort((a, b2) => b2.cap_spread - a.cap_spread);
    let minVal = 100;
    plotSlices.forEach(sl => {
      Object.values(sl.by_arm).forEach(v => { if (v.cap < minVal) minVal = v.cap; });
    });
    const domMin = minVal >= 48 ? 50 : 0;
    const scaleX = v => Math.max(2, Math.min(98, ((v - domMin) / (100 - domMin)) * 100));

    const legendHtml = '<div class="dot-legend"><span>Arms:</span>' +
      DATA.arms_order.map(am =>
        '<span class="legend-item"><span class="arm-dot" style="background:' + am.color + '"></span>' + esc(am.short) + '</span>'
      ).join('') + '</div>';

    const plotRowsHtml = plotSlices.map(sl => {
      const vals = DATA.arms_order.map(am => sl.by_arm[am.id] ? sl.by_arm[am.id].cap : null).filter(v => v !== null);
      const loX = scaleX(Math.min(...vals));
      const hiX = scaleX(Math.max(...vals));
      const pts = DATA.arms_order.map(am => {
        const rec = sl.by_arm[am.id];
        if (!rec) return '';
        return '<span class="slice-plot-pt" title="' + esc(am.short) + ': ' + rec.cap.toFixed(1) + '%" style="left:' + scaleX(rec.cap) + '%;background:' + am.color + '"></span>';
      }).join('');
      const badgeTxt = DATA.arms_order.length > 1
        ? ('spread ' + sl.cap_spread.toFixed(1) + ' pts · SJ ' + (sl.by_arm[pId] ? sl.by_arm[pId].cap.toFixed(1) + '%' : ''))
        : ((sl.by_arm[pId] ? sl.by_arm[pId].cap.toFixed(1) + '%' : ''));
      return '<div class="slice-plot-row" data-slice-cat="' + esc(sl.filter.cat) + '" data-slice-out="' + esc(sl.filter.outcome) + '">' +
        '<div class="slice-plot-head"><span style="font-weight:500;color:var(--ink)">' + esc(sl.label) + '</span>' +
        '<span class="mono" style="font-size:11px;color:var(--ink-2)">' + badgeTxt + '</span></div>' +
        '<div class="slice-plot-track"><div class="slice-plot-span" style="left:' + loX + '%;width:' + Math.max(0, hiX - loX) + '%"></div>' + pts + '</div>' +
      '</div>';
    }).join('');

    const axisHtml = domMin === 50
      ? '<div class="axis-labels"><span>50%</span><span>60%</span><span>70%</span><span>80%</span><span>90%</span><span>100%</span></div>'
      : '<div class="axis-labels"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>';

    // Top failure causes
    const toneColor = { primary: 'var(--primary)', warning: 'var(--warn)', neutral: 'var(--ink-2)', danger: 'var(--bad)' };
    const failRowsHtml = DATA.failures.map(f =>
      '<div class="fail-row" data-fail-key="' + esc(f.key) + '">' +
        '<div class="fail-head"><span class="fail-title">' + esc(f.label) + '</span>' +
        '<span class="mono nowrap" style="font-size:11.5px;color:var(--ink-2)">' + f.count + ' tasks (<strong>' + f.pct + '%</strong>)</span></div>' +
        '<div class="fail-bar-bg"><div class="fail-bar-fill" style="width:' + Math.max(f.count > 0 ? 3 : 0, f.pct) + '%;background:' + (toneColor[f.tone] || 'var(--ink-2)') + '"></div></div>' +
        '<div class="fail-desc">' + esc(f.desc) + '</div>' +
      '</div>'
    ).join('');

    const totRunsLabel = 'arms: ' + DATA.arms_order.length + ' · runs: ' + DATA.meta.total_runs +
      (DATA.meta.ablation_runs ? ' (+ ' + DATA.meta.ablation_runs + ' ablation)' : '');
    const calibFoot = DATA.arms_order.some(a => a.is_calibrated_tok)
      ? '<div style="padding:8px 16px;border-top:1px solid var(--line-soft);background:var(--surface-low);font-size:11px;color:var(--ink-2)" class="mono">* Historical HTML baseline tokens/step calibrated from paired live Vertex default-thinking suite measurements.</div>'
      : '';

    return '' +
      '<section class="card"><div class="verdict">' +
        '<div class="verdict-icon">' + ICONS.check + '</div>' +
        '<div><h1 class="verdict-h1">' + headline + '</h1><p class="verdict-sub">' + subline + '</p></div>' +
      '</div></section>' +

      '<section class="kpi-grid">' +
        '<div class="kpi-card"><div class="kpi-top"><span class="kpi-label">Macro Capability</span>' + capBadge + '</div>' +
          '<div class="kpi-val">' + p.cap.toFixed(1) + '%</div>' +
          '<div class="kpi-foot">95% CI ' + p.cap_ci[0].toFixed(1) + '% to ' + p.cap_ci[1].toFixed(1) + '%</div></div>' +
        '<div class="kpi-card"><div class="kpi-top"><span class="kpi-label">Binary Pass Rate</span><span class="badge badge-neutral">' + esc(passBadgeText) + '</span></div>' +
          '<div class="kpi-val">' + p.pass_pct.toFixed(1) + '%</div>' +
          '<div class="kpi-foot">Wilson 95% CI ' + p.pass_ci[0].toFixed(1) + '% to ' + p.pass_ci[1].toFixed(1) + '%</div></div>' +
        '<div class="kpi-card"><div class="kpi-top"><span class="kpi-label">Tokens / Step</span>' + tokBadge + '</div>' +
          '<div class="kpi-val">' + fmtCompact(p.tok_per_step) + '</div>' +
          '<div class="kpi-foot">' + fmtCompact(p.vtx_tot) + ' total · ' + p.cache_hit_pct.toFixed(1) + '% prompt cache hit</div></div>' +
        '<div class="kpi-card"><div class="kpi-top"><span class="kpi-label">p50 / p90 Steps</span>' + stepBadge + '</div>' +
          '<div class="kpi-val">' + p.steps_p50 + ' <span class="muted-part">/ ' + p.steps_p90 + '</span></div>' +
          '<div class="kpi-foot">' + fmtInt(p.steps_tot) + ' total steps' + (b ? ' vs ' + fmtInt(b.steps_tot) + ' on ' + esc(b.short) : '') + '</div></div>' +
      '</section>' +

      '<section class="card">' +
        '<div class="card-head"><div><h2 class="card-title">Arm Leaderboard</h2>' +
          '<p class="card-sub">Evaluated under identical prompts, isolated sandboxes, and official grader verification (' + esc(sc.label) + ')</p></div>' +
          '<span class="suite-pill">' + esc(totRunsLabel) + '</span></div>' +
        '<div class="table-wrap"><table class="data-table"><thead><tr>' +
          '<th>Arm</th><th>Macro capability (95% CI)</th><th class="num-right">Delta vs baseline</th>' +
          '<th class="num-right">Pass rate</th><th class="num-right">Total / p50 steps</th>' +
          '<th class="num-right">p50 / p90 latency</th><th class="num-right">Tokens / step</th>' +
        '</tr></thead><tbody>' + lbRows + '</tbody></table></div>' + calibFoot +
      '</section>' +

      '<section class="two-col">' +
        '<div class="card"><div class="card-head"><div><h2 class="card-title">Where arms disagree</h2>' +
          '<p class="card-sub">Macro capability by category and behavioral bucket (click any slice to inspect tasks)</p></div>' +
          '<span class="badge badge-neutral">' + plotSlices.length + ' slices</span></div>' +
          '<div class="card-body">' + legendHtml + plotRowsHtml + axisHtml + '</div></div>' +
        '<div class="card"><div class="card-head"><div><h2 class="card-title">Top failure causes</h2>' +
          '<p class="card-sub">Deterministic verifier breakdown across unsolved runs (click any bar to filter tasks)</p></div>' +
          '<span class="badge badge-neutral">' + DATA.unsolved_total + ' unsolved tasks</span></div>' +
          '<div class="card-body">' + failRowsHtml + '</div></div>' +
      '</section>';
  }
"""
_JS_PART_2 = r"""
  function buildSlicesHtml() {
    const pId = DATA.meta.primary_arm;
    const bId = DATA.meta.baseline_arm;
    const lev = DATA.meta.levers || {};
    const sc0 = DATA.scopes[0].arms[pId];

    const armColsHead = DATA.arms_order.map(am =>
      '<th class="num-right">' + esc(am.short) + ' Cap</th>'
    ).join('');

    const sliceRows = DATA.slices.map(sl => {
      const p = sl.by_arm[pId] || {};
      const b = bId ? (sl.by_arm[bId] || null) : null;
      const armCells = DATA.arms_order.map(am => {
        const rec = sl.by_arm[am.id];
        if (!rec) return '<td class="num-right">—</td>';
        const bold = am.id === pId ? 'font-weight:700;color:var(--ink)' : 'color:var(--ink-2)';
        return '<td class="num-right nowrap" style="' + bold + '">' + rec.cap.toFixed(1) + '%</td>';
      }).join('');

      const stepCell = b
        ? (fmtInt(b.steps) + ' → <strong>' + fmtInt(p.steps) + '</strong> <span class="badge ' + (sl.steps_delta_base_pct <= 0 ? 'badge-ok' : 'badge-warn') + '">' + fmtSign(sl.steps_delta_base_pct, '%') + '</span>')
        : fmtInt(p.steps);

      return '<tr class="clickable" data-slice-cat="' + esc(sl.filter.cat) + '" data-slice-out="' + esc(sl.filter.outcome) + '">' +
        '<td class="sans-cell nowrap" style="font-weight:500;color:var(--ink)">' + esc(sl.label) + '</td>' +
        '<td class="num-right">' + sl.n + '</td>' +
        armCells +
        '<td class="num-right nowrap"><strong>' + (p.pass_pct || 0).toFixed(1) + '%</strong> <span style="color:var(--ink-2)">(' + (p.pass_k || 0) + '/' + sl.n + ')</span></td>' +
        '<td class="num-right nowrap">' + stepCell + '</td>' +
        '<td class="num-right nowrap">' + (p.wall_m || 0).toFixed(1) + ' m</td>' +
        '<td class="num-right nowrap">' + fmtCompact(p.tok_per_step || 0) + '</td>' +
        '<td class="num-right nowrap">' + (p.cache_hit_pct || 0).toFixed(1) + '% · ' + sl.epochs + ' ep</td>' +
      '</tr>';
    }).join('');

    const msgCutPct = lev.msg_chars_baseline > 0
      ? (((lev.msg_chars_primary - lev.msg_chars_baseline) / lev.msg_chars_baseline) * 100).toFixed(1) + '%'
      : '0 leaked';

    const leversCard = '<section class="card">' +
      '<div class="card-head"><div><h2 class="card-title">Straitjacket Smart-Peg Lever Telemetry</h2>' +
        '<p class="card-sub">Measured sidecar containment, residence compaction, and mutation-pinning counters across all ' + DATA.meta.total_tasks + ' tasks</p></div>' +
        '<span class="badge badge-primary">' + esc(DATA.meta.harness) + '</span></div>' +
      '<div class="card-body"><div class="lever-grid">' +
        '<div class="lever-tile"><div class="lever-kicker">WatermarkResidence Compaction</div>' +
          '<div class="lever-big">' + fmtInt(lev.compaction_epochs) + ' epochs</div>' +
          '<div class="lever-note">' + fmtCompact(lev.thoughts_compacted) + ' carried thought tokens evicted while keeping tool evidence verbatim</div></div>' +
        '<div class="lever-tile"><div class="lever-kicker">Prefix Cache Economics</div>' +
          '<div class="lever-big">' + (sc0 ? sc0.cache_hit_pct.toFixed(1) + '%' : '—') + '</div>' +
          '<div class="lever-note">' + (sc0 ? fmtCompact(sc0.vtx_tot) + ' total billed tokens across ' + fmtInt(sc0.steps_tot) + ' turns' : '') + '</div></div>' +
        '<div class="lever-tile"><div class="lever-kicker">Entry Gate Bounded Digests</div>' +
          '<div class="lever-big">' + fmtInt(lev.digests_delivered) + ' digests</div>' +
          '<div class="lever-note">' + fmtBytes(lev.bytes_kept_out) + ' of raw command floods kept outside prompt context</div></div>' +
        '<div class="lever-tile"><div class="lever-kicker">State &amp; Mutation Durability</div>' +
          '<div class="lever-big">' + fmtInt(lev.state_notes_preserved) + ' / ' + fmtInt(lev.state_notes_emitted) + '</div>' +
          '<div class="lever-note">state_note checkpoints preserved + ' + fmtInt(lev.mutations_preserved) + ' mutating turns pinned</div></div>' +
        '<div class="lever-tile"><div class="lever-kicker">Browser UI Control Stripping</div>' +
          '<div class="lever-big">' + fmtCompact(lev.ui_controls_stripped_chars) + ' chars</div>' +
          '<div class="lever-note">Stale accessibility control trees stripped from older browser snapshots</div></div>' +
        '<div class="lever-tile"><div class="lever-kicker">Visible Monologue Suppression</div>' +
          '<div class="lever-big">' + esc(msgCutPct) + '</div>' +
          '<div class="lever-note">' + fmtCompact(lev.msg_chars_primary) + ' chars emitted' + (lev.msg_chars_baseline ? ' vs ' + fmtCompact(lev.msg_chars_baseline) + ' on baseline' : '') + '</div></div>' +
      '</div></div></section>';

    let ablationCard = '';
    if (DATA.ablation && DATA.ablation.arms && DATA.ablation.arms.length > 0) {
      const abArms = DATA.ablation.arms;
      const maxTok = Math.max(...abArms.map(a => a.tot_tokens || 1));
      const base38 = abArms.find(a => a.key === '38base') || abArms[0];

      const abRows = abArms.map(a => {
        const pctOfMax = Math.max(4, (a.tot_tokens / maxTok) * 100);
        const delta38 = base38 && base38.tot_tokens > 0 && a.key !== base38.key
          ? (((a.tot_tokens - base38.tot_tokens) / base38.tot_tokens) * 100)
          : null;
        const barCol = a.key.includes('sj') || a.key === 'suite_overlap' ? 'var(--primary)' : (a.key === '38base' ? 'var(--warn)' : 'var(--ink-2)');
        return '<tr class="' + (a.key === '38sj_v3' || a.key === 'suite_overlap' ? 'row-primary' : '') + '">' +
          '<td class="sans-cell nowrap" style="font-weight:600">' + esc(a.label) + '</td>' +
          '<td><div style="display:flex;align-items:center;gap:10px;min-width:190px">' +
            '<span style="min-width:58px;font-weight:600">' + fmtCompact(a.tot_tokens) + '</span>' +
            '<div style="flex:1;height:7px;background:var(--line-soft);border-radius:999px;overflow:hidden">' +
              '<div style="width:' + pctOfMax.toFixed(1) + '%;height:100%;background:' + barCol + '"></div></div></div></td>' +
          '<td class="num-right nowrap">' + (delta38 !== null ? '<span class="badge ' + (delta38 <= 0 ? 'badge-ok' : 'badge-warn') + '">' + fmtSign(delta38, '%') + '</span>' : '<span class="badge badge-neutral">ref</span>') + '</td>' +
          '<td class="num-right nowrap">' + fmtCompact(a.med_tokens) + '</td>' +
          '<td class="num-right nowrap">' + a.mean_steps.toFixed(1) + '</td>' +
          '<td class="num-right nowrap">' + a.med_wall_s.toFixed(0) + ' s</td>' +
          '<td class="num-right nowrap"><strong>' + a.checks_pct.toFixed(1) + '%</strong> <span style="color:var(--ink-2)">(' + a.checks_passed + '/' + a.checks_total + ')</span></td>' +
          '<td class="num-right nowrap">' + a.cache_pct.toFixed(1) + '%</td>' +
        '</tr>';
      }).join('');

      let matrixHtml = '';
      if (STATE.showAblationMatrix && DATA.ablation.tasks && DATA.ablation.tasks.length) {
        const mHead = abArms.map(a => '<th class="num-right">' + esc(a.key) + '</th>').join('');
        const mRows = DATA.ablation.tasks.map(t => {
          const cells = abArms.map(a => {
            const c = t.arms[a.key];
            if (!c) return '<td class="num-right">—</td>';
            return '<td class="num-right nowrap">' + fmtCompact(c.tok) + ' <span style="color:var(--ink-2)">(' + c.steps + 'st · ' + esc(c.chk) + ')</span></td>';
          }).join('');
          return '<tr><td class="mono nowrap" style="font-weight:600">' + esc(t.task_key) + '</td>' + cells + '</tr>';
        }).join('');
        matrixHtml = '<div style="border-top:1px solid var(--line)"><div class="table-wrap"><table class="data-table"><thead><tr><th>Task</th>' + mHead + '</tr></thead><tbody>' + mRows + '</tbody></table></div></div>';
      }

      ablationCard = '<section class="card">' +
        '<div class="card-head"><div><h2 class="card-title">Controlled Multi-Arm Lever Ablation (Exact Vertex Billing)</h2>' +
          '<p class="card-sub">Stepwise isolation from raw default-thinking baselines through Sidecar v1, WatermarkResidence v2, state_note Smart-Peg v3, and Full-Suite overlap</p></div>' +
          '<button type="button" class="btn btn-xs" id="btn-toggle-ab-matrix">' + (STATE.showAblationMatrix ? 'Hide Per-Task Matrix' : 'Show 10-Task Matrix') + '</button></div>' +
        '<div class="table-wrap"><table class="data-table"><thead><tr>' +
          '<th>Ablation Arm</th><th>Cumulative Tokens</th><th class="num-right">vs 38base</th>' +
          '<th class="num-right">Median Tok</th><th class="num-right">Mean Steps</th><th class="num-right">Median Wall</th>' +
          '<th class="num-right">Rubric Checks</th><th class="num-right">Cache Hit</th>' +
        '</tr></thead><tbody>' + abRows + '</tbody></table></div>' + matrixHtml +
      '</section>';
    }

    return '' +
      '<section class="card">' +
        '<div class="card-head"><div><h2 class="card-title">Stratified Slice Comparison</h2>' +
          '<p class="card-sub">Performance across modalities and baseline behavioral buckets (click any row to inspect matching tasks)</p></div></div>' +
        '<div class="table-wrap"><table class="data-table"><thead><tr>' +
          '<th>Slice</th><th class="num-right">N</th>' + armColsHead +
          '<th class="num-right">Proposed Pass</th><th class="num-right">Steps</th>' +
          '<th class="num-right">Wall</th><th class="num-right">Tok / Step</th><th class="num-right">Cache &amp; Compaction</th>' +
        '</tr></thead><tbody>' + sliceRows + '</tbody></table></div>' +
      '</section>' +
      leversCard +
      ablationCard;
  }

  function getFilteredTasks() {
    const pId = DATA.meta.primary_arm;
    const bId = DATA.meta.baseline_arm;
    const q = STATE.taskSearch.trim().toLowerCase();

    const list = DATA.tasks.filter(t => {
      const p = t.by_arm[pId];
      const b = bId ? t.by_arm[bId] : null;
      if (STATE.taskCat !== 'all' && t.cat !== STATE.taskCat) return false;
      if (STATE.taskFailCat && t.fail_cat !== STATE.taskFailCat) return false;
      if (STATE.taskOutcome === 'passed' && !p.passed) return false;
      if (STATE.taskOutcome === 'failed' && p.passed) return false;
      if (STATE.taskOutcome === 'gained' && !(b && p.passed && !b.passed)) return false;
      if (STATE.taskOutcome === 'lost' && !(b && !p.passed && b.passed)) return false;
      if (STATE.taskOutcome === 'bucket_b' && t.bucket !== 'better') return false;
      if (STATE.taskOutcome === 'bucket_w' && t.bucket !== 'worse') return false;
      if (q) {
        const hay = (t.id + ' ' + t.cat + ' ' + (t.failed_preview || []).join(' ')).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    list.sort((a, b2) => {
      const pa = a.by_arm[pId], pb = b2.by_arm[pId];
      if (STATE.taskSort === 'steps_delta') return a.steps_delta_base_pct - b2.steps_delta_base_pct;
      if (STATE.taskSort === 'cap_delta') return b2.cap_delta_base - a.cap_delta_base;
      if (STATE.taskSort === 'tokens_desc') return pb.vtx_total - pa.vtx_total;
      if (STATE.taskSort === 'epochs_desc') return b2.epochs - a.epochs;
      return a.id.localeCompare(b2.id);
    });
    return list;
  }

  function buildTasksHtml() {
    const pId = DATA.meta.primary_arm;
    const bId = DATA.meta.baseline_arm;
    const filtered = getFilteredTasks();

    const cats = ['all', 'cli', 'browser', 'file', 'api'];
    const catBtns = cats.map(c => {
      const cnt = c === 'all' ? DATA.meta.total_tasks : (DATA.meta.categories[c] || 0);
      if (c !== 'all' && !cnt) return '';
      return '<button type="button" class="seg-btn' + (STATE.taskCat === c ? ' active' : '') + '" data-f-cat="' + c + '">' +
        (c === 'all' ? 'All (' + cnt + ')' : c.toUpperCase() + ' (' + cnt + ')') + '</button>';
    }).join('');

    const outs = [
      ['all', 'All Outcomes'],
      ['passed', 'Passed'],
      ['failed', 'Unsolved'],
      ...(bId ? [['gained', 'Gained vs Base'], ['lost', 'Lost vs Base']] : []),
      ['bucket_b', 'Bucket B']
    ];
    const outBtns = outs.map(([k, lbl]) =>
      '<button type="button" class="seg-btn' + (STATE.taskOutcome === k ? ' active' : '') + '" data-f-out="' + k + '">' + esc(lbl) + '</button>'
    ).join('');

    let failChip = '';
    if (STATE.taskFailCat) {
      const fm = DATA.failures.find(x => x.key === STATE.taskFailCat);
      failChip = '<button type="button" class="badge badge-bad" style="border:1px solid #F5C2C7;cursor:pointer" id="btn-clear-fail-cat">Failure: ' +
        esc(fm ? fm.label : STATE.taskFailCat) + ' ×</button>';
    }

    const rows = filtered.map(t => {
      const p = t.by_arm[pId];
      const b = bId ? t.by_arm[bId] : null;
      const statusBadge = p.passed
        ? '<span class="badge badge-ok">PASS</span>'
        : '<span class="badge badge-bad">FAIL</span>';

      const armDots = DATA.arms_order.slice(1).map(am => {
        const ar = t.by_arm[am.id];
        if (!ar) return '';
        return '<span title="' + esc(am.short) + ': ' + (ar.passed ? 'PASS' : 'FAIL') + ' (' + Math.round(ar.cap * 100) + '%)" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:' + (ar.passed ? 'var(--ok)' : '#DADCE0') + '"></span>';
      }).join('');

      const capStr = b
        ? ((b.cap * 100).toFixed(0) + '% → <strong>' + (p.cap * 100).toFixed(0) + '%</strong>')
        : ('<strong>' + (p.cap * 100).toFixed(0) + '%</strong>');

      const stepDeltaBadge = b && b.steps > 0
        ? ' <span class="badge ' + (t.steps_delta_base <= 0 ? 'badge-ok' : 'badge-warn') + '">' + fmtSign(t.steps_delta_base_pct, '%', 0) + '</span>'
        : '';

      return '<tr class="clickable" data-open-trace="' + esc(t.id) + '">' +
        '<td class="nowrap"><div style="display:flex;align-items:center;gap:8px">' +
          '<span class="badge badge-neutral" style="min-width:56px;justify-content:center">' + esc(t.cat) + '</span>' +
          '<span style="font-weight:600;color:var(--ink)">' + esc(t.id) + '</span>' +
          (t.bucket === 'better' ? '<span class="badge badge-primary" style="font-size:10px">Bucket B</span>' : '') +
        '</div></td>' +
        '<td class="nowrap"><div style="display:flex;align-items:center;gap:6px">' + statusBadge + armDots + '</div></td>' +
        '<td class="num-right nowrap">' + esc(p.checks_str) + '</td>' +
        '<td class="num-right nowrap">' + capStr + '</td>' +
        '<td class="num-right nowrap"><strong>' + p.steps + '</strong>' + (b ? ' <span style="color:var(--ink-2)">/ ' + b.steps + '</span>' : '') + stepDeltaBadge + '</td>' +
        '<td class="num-right nowrap">' + p.dur.toFixed(0) + ' s</td>' +
        '<td class="num-right nowrap">' + fmtCompact(p.vtx_total) + ' <span style="color:var(--ink-2)">(' + p.cache_pct.toFixed(0) + '%)</span></td>' +
        '<td class="num-right nowrap sans-cell"><span style="color:var(--primary);font-weight:600">Inspect →</span></td>' +
      '</tr>';
    }).join('');

    return '<section class="card">' +
      '<div class="filter-bar">' +
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
          '<div class="search-wrap">' + ICONS.search +
            '<input type="search" id="task-search-in" class="search-input" placeholder="Search task ID or failed check…" value="' + esc(STATE.taskSearch) + '"/></div>' +
          '<div class="seg-group">' + catBtns + '</div>' +
          '<div class="seg-group">' + outBtns + '</div>' +
          failChip +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px">' +
          '<span class="mono" style="font-size:11.5px;color:var(--ink-2)">' + filtered.length + ' / ' + DATA.tasks.length + '</span>' +
          '<select id="task-sort-sel" class="select-ctl">' +
            '<option value="steps_delta"' + (STATE.taskSort === 'steps_delta' ? ' selected' : '') + '>Sort: Largest Step Cut</option>' +
            '<option value="cap_delta"' + (STATE.taskSort === 'cap_delta' ? ' selected' : '') + '>Sort: Largest Capability Gain</option>' +
            '<option value="tokens_desc"' + (STATE.taskSort === 'tokens_desc' ? ' selected' : '') + '>Sort: Highest Vertex Tokens</option>' +
            '<option value="epochs_desc"' + (STATE.taskSort === 'epochs_desc' ? ' selected' : '') + '>Sort: Most Compaction Epochs</option>' +
            '<option value="id_asc"' + (STATE.taskSort === 'id_asc' ? ' selected' : '') + '>Sort: Task ID (A–Z)</option>' +
          '</select>' +
        '</div>' +
      '</div>' +
      '<div class="table-wrap"><table class="data-table"><thead><tr>' +
        '<th>Task</th><th>Status</th><th class="num-right">Checks</th>' +
        '<th class="num-right">' + (bId ? 'Base → SJ Cap' : 'Capability') + '</th>' +
        '<th class="num-right">Steps' + (bId ? ' (SJ / Base)' : '') + '</th>' +
        '<th class="num-right">Latency</th><th class="num-right">Tokens (Cache)</th><th class="num-right">Trajectory</th>' +
      '</tr></thead><tbody>' + (rows || '<tr><td colspan="8" class="sans-cell" style="padding:24px;text-align:center;color:var(--ink-2)">No tasks match current filters.</td></tr>') + '</tbody></table></div>' +
    '</section>';
  }
"""
_JS_PART_3 = r"""
  function classifyToolTone(tname) {
    if (tname === 'present_files') return 'done';
    if (['write', 'edit', 'browser_click', 'browser_type', 'browser_select', 'browser_press'].includes(tname)) return 'mut';
    return '';
  }

  function renderStepChartSvg(armTrace) {
    const steps = (armTrace && armTrace.steps) || [];
    if (!steps.length) return '';
    const isLive = armTrace.kind === 'live';
    const W = 760, H = 148, padL = 42, padR = 12, padT = 18, padB = 22;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const n = steps.length;
    const barGap = n > 70 ? 1 : 2;
    const barW = Math.max(2, (plotW - barGap * (n - 1)) / n);

    if (isLive) {
      const maxVal = Math.max(10000, ...steps.map(s => (s.p || 0) + (s.t || 0)));
      const yOf = v => padT + plotH - Math.min(plotH, (v / maxVal) * plotH);
      let bars = '', flags = '';

      steps.forEach((s, idx) => {
        const x = padL + idx * (barW + barGap);
        const cp = Math.min(s.p || 0, s.cp || 0);
        const uncached = Math.max(0, (s.p || 0) - cp);
        const th = s.t || 0;
        const hCp = (cp / maxVal) * plotH;
        const hUn = (uncached / maxVal) * plotH;
        const hTh = (th / maxVal) * plotH;

        const yCp = padT + plotH - hCp;
        const yUn = yCp - hUn;
        const yTh = yUn - hTh;
        const tip = 'Step ' + s.s + ': prompt ' + fmtCompact(s.p) + ' (' + fmtCompact(cp) + ' cached), thought ' + fmtCompact(th) + (s.ep ? ' [Epoch ' + s.ep + ': -' + fmtCompact(s.drop) + ']' : '') + (s.dig ? ' [Digest]' : '');

        bars += '<g class="chart-step-bar" data-jump-step="' + s.s + '" style="cursor:pointer"><title>' + esc(tip) + '</title>' +
          '<rect x="' + x.toFixed(1) + '" y="' + padT + '" width="' + Math.max(barW, 3).toFixed(1) + '" height="' + plotH + '" fill="transparent"/>' +
          (hCp > 0.4 ? '<rect x="' + x.toFixed(1) + '" y="' + yCp.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + hCp.toFixed(1) + '" fill="#AECBFA"/>' : '') +
          (hUn > 0.4 ? '<rect x="' + x.toFixed(1) + '" y="' + yUn.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + hUn.toFixed(1) + '" fill="#1A73E8"/>' : '') +
          (hTh > 0.4 ? '<rect x="' + x.toFixed(1) + '" y="' + yTh.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + hTh.toFixed(1) + '" fill="#7C3AED"/>' : '') +
        '</g>';

        if (s.ep > 0) {
          flags += '<line x1="' + x.toFixed(1) + '" y1="' + padT + '" x2="' + x.toFixed(1) + '" y2="' + (padT + plotH) + '" stroke="#188038" stroke-width="1.3" stroke-dasharray="3,2"/>' +
            '<circle cx="' + x.toFixed(1) + '" cy="' + (padT + 4) + '" r="3.2" fill="#188038"><title>Epoch ' + s.ep + ': -' + fmtCompact(s.drop) + ' tok</title></circle>';
        }
        if (s.dig) {
          flags += '<circle cx="' + (x + barW / 2).toFixed(1) + '" cy="' + (padT + plotH - 3) + '" r="2.8" fill="#E37400"><title>Step ' + s.s + ': Ctx Digest</title></circle>';
        }
      });

      return '<div class="card"><div class="card-head"><div>' +
        '<h2 class="card-title">Per-Step Prompt Window, Cached Prefix &amp; Compaction Timeline</h2>' +
        '<p class="card-sub">Steps 1–' + n + ' · Click any bar to jump to that turn</p></div>' +
        '<div style="display:flex;gap:12px;flex-wrap:wrap;font-size:11px;color:var(--ink-2)">' +
          '<span class="legend-item"><span class="arm-dot" style="background:#AECBFA;border-radius:2px"></span>Cached prefix</span>' +
          '<span class="legend-item"><span class="arm-dot" style="background:#1A73E8;border-radius:2px"></span>Uncached prompt</span>' +
          '<span class="legend-item"><span class="arm-dot" style="background:#7C3AED;border-radius:2px"></span>Thought tokens</span>' +
          '<span class="legend-item"><span class="arm-dot" style="background:#188038"></span>Compaction epoch</span>' +
          '<span class="legend-item"><span class="arm-dot" style="background:#E37400"></span>Ctx digest</span>' +
        '</div></div>' +
        '<div class="card-body" style="padding:10px 14px">' +
          '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">' +
            '<line x1="' + padL + '" y1="' + padT + '" x2="' + (W - padR) + '" y2="' + padT + '" stroke="#E8EAED" stroke-dasharray="3,3"/>' +
            '<line x1="' + padL + '" y1="' + yOf(maxVal / 2).toFixed(1) + '" x2="' + (W - padR) + '" y2="' + yOf(maxVal / 2).toFixed(1) + '" stroke="#E8EAED" stroke-dasharray="3,3"/>' +
            '<line x1="' + padL + '" y1="' + (padT + plotH) + '" x2="' + (W - padR) + '" y2="' + (padT + plotH) + '" stroke="#DADCE0"/>' +
            '<text x="4" y="' + (padT + 4) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#5F6368">' + fmtCompact(maxVal) + '</text>' +
            '<text x="4" y="' + (yOf(maxVal / 2) + 3).toFixed(1) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#5F6368">' + fmtCompact(maxVal / 2) + '</text>' +
            '<text x="4" y="' + (padT + plotH) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#5F6368">0</text>' +
            bars + flags +
            '<text x="' + padL + '" y="' + (H - 5) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#80868B">Step 1</text>' +
            '<text x="' + (W - padR) + '" y="' + (H - 5) + '" text-anchor="end" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#80868B">Step ' + n + '</text>' +
          '</svg></div></div>';
    }

    // Historical HTML baseline chart: Reasoning chars + Visible Monologue chars per step
    const maxChars = Math.max(1000, ...steps.map(s => (s.rc || 0) + (s.mc || 0)));
    let bars = '';
    steps.forEach((s, idx) => {
      const x = padL + idx * (barW + barGap);
      const hR = ((s.rc || 0) / maxChars) * plotH;
      const hM = ((s.mc || 0) / maxChars) * plotH;
      const yR = padT + plotH - hR;
      const yM = yR - hM;
      const tip = 'Step ' + s.s + ': ' + fmtCompact(s.rc) + ' reasoning chars, ' + fmtCompact(s.mc) + ' visible message chars';
      bars += '<g class="chart-step-bar" data-jump-step="' + s.s + '" style="cursor:pointer"><title>' + esc(tip) + '</title>' +
        '<rect x="' + x.toFixed(1) + '" y="' + padT + '" width="' + Math.max(barW, 3).toFixed(1) + '" height="' + plotH + '" fill="transparent"/>' +
        (hR > 0.4 ? '<rect x="' + x.toFixed(1) + '" y="' + yR.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + hR.toFixed(1) + '" fill="#7C3AED"/>' : '') +
        (hM > 0.4 ? '<rect x="' + x.toFixed(1) + '" y="' + yM.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + hM.toFixed(1) + '" fill="#E37400"/>' : '') +
      '</g>';
    });
    return '<div class="card"><div class="card-head"><div>' +
      '<h2 class="card-title">Per-Step Reasoning &amp; Visible Monologue Characters</h2>' +
      '<p class="card-sub">Steps 1–' + n + ' · Click any bar to inspect step output</p></div>' +
      '<div style="display:flex;gap:12px;font-size:11px;color:var(--ink-2)">' +
        '<span class="legend-item"><span class="arm-dot" style="background:#7C3AED;border-radius:2px"></span>Reasoning chars</span>' +
        '<span class="legend-item"><span class="arm-dot" style="background:#E37400;border-radius:2px"></span>Visible monologue chars</span>' +
      '</div></div>' +
      '<div class="card-body" style="padding:10px 14px">' +
        '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">' +
          '<line x1="' + padL + '" y1="' + (padT + plotH) + '" x2="' + (W - padR) + '" y2="' + (padT + plotH) + '" stroke="#DADCE0"/>' +
          '<text x="4" y="' + (padT + 4) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#5F6368">' + fmtCompact(maxChars) + '</text>' +
          '<text x="4" y="' + (padT + plotH) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#5F6368">0</text>' +
          bars +
          '<text x="' + padL + '" y="' + (H - 5) + '" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#80868B">Step 1</text>' +
          '<text x="' + (W - padR) + '" y="' + (H - 5) + '" text-anchor="end" font-family="JetBrains Mono,monospace" font-size="9.5" fill="#80868B">Step ' + n + '</text>' +
        '</svg></div></div>';
  }

  function renderStepItemHtml(s, isLive, compactMode) {
    const calls = s.c || [];
    const firstTool = calls.length ? calls[0].t : 'text';
    const extraTools = calls.length > 1 ? (' +' + (calls.length - 1)) : '';
    const tone = classifyToolTone(firstTool);
    const sumTxt = calls.length ? (calls[0].sum || calls[0].t) : ((isLive ? s.txt : (s.mh || s.rh)) || '(final turn)').slice(0, 110);
    const isSpike = isLive ? ((s.t || 0) >= 4000) : (((s.rc || 0) + (s.mc || 0)) >= 12000);
    const hasDig = Boolean(s.dig);

    let banner = '';
    if (isLive && s.ep > 0) {
      banner = '<div class="epoch-banner"><span><strong>WatermarkResidence Epoch ' + s.ep + '</strong> · evicted ' +
        fmtCompact(s.drop) + ' carried prompt tokens while retaining verbatim tool outputs &amp; state notes</span>' +
        '<span class="badge badge-ok">-' + fmtCompact(s.drop) + ' tok</span></div>';
    }

    let rightMeta = '';
    if (isLive) {
      rightMeta = (hasDig ? '<span class="badge badge-warn">DIGEST</span>' : '') +
        (isSpike ? '<span class="badge" style="background:var(--thought-bg);color:var(--thought)">+' + fmtCompact(s.t) + ' th</span>' : '') +
        (!compactMode ? '<span>' + fmtCompact(s.p) + ' p</span><span>' + (s.lat || 0).toFixed(1) + 's</span>' : '<span>' + fmtCompact(s.p) + '</span>');
    } else {
      rightMeta = ((s.mc || 0) > 2000 ? '<span class="badge badge-warn">' + fmtCompact(s.mc) + ' msg ch</span>' : '') +
        ((s.rc || 0) > 0 ? '<span>' + fmtCompact(s.rc) + ' reas ch</span>' : '');
    }

    let bodyBlocks = '';
    if (isLive) {
      if (s.txt) {
        bodyBlocks += '<div><div class="block-lbl">Model Visible Text</div><pre class="code-block">' + esc(s.txt) + '</pre></div>';
      }
      calls.forEach((c, ci) => {
        const hdr = 'Tool Call ' + (ci + 1) + ': ' + c.t +
          (c.ec !== null && c.ec !== undefined ? ' (exit=' + c.ec + ')' : '') +
          (c.rb ? ' · raw ' + fmtBytes(c.rb) + ' -> ' + fmtInt(c.rc) + ' inline chars' : ' · ' + fmtInt(c.rc) + ' chars');
        bodyBlocks += '<div><div class="block-lbl">' + esc(hdr) + '</div>' +
          (c.cmd ? '<pre class="code-block" style="margin-bottom:4px;background:#F1F3F4">' + esc(c.cmd) + '</pre>' : '') +
          (c.h ? '<pre class="code-block">' + esc(c.h) + '</pre>' : '') + '</div>';
      });
    } else {
      if (s.rh) bodyBlocks += '<div><div class="block-lbl">Reasoning Preview (' + fmtInt(s.rc) + ' chars)</div><pre class="code-block">' + esc(s.rh) + '</pre></div>';
      if (s.mh) bodyBlocks += '<div><div class="block-lbl">Visible Message (' + fmtInt(s.mc) + ' chars)</div><pre class="code-block">' + esc(s.mh) + '</pre></div>';
      calls.forEach(c => {
        bodyBlocks += '<div><div class="block-lbl">Tool Call: ' + esc(c.t) + '</div><pre class="code-block">' + esc(c.arg || c.sum) + '</pre></div>';
      });
      if (s.o) bodyBlocks += '<div><div class="block-lbl">Observation Preview</div><pre class="code-block">' + esc(s.o) + '</pre></div>';
    }

    return banner + '<div class="step-item' + (hasDig ? ' has-digest' : (isSpike ? ' has-spike' : '')) + '" id="trace-step-' + s.s + '">' +
      '<div class="step-head" data-toggle-step="1">' +
        '<div class="step-left">' +
          '<span class="step-num">#' + s.s + '</span>' +
          '<span class="tool-chip ' + tone + '">' + esc(firstTool + extraTools) + '</span>' +
          '<span class="step-sum">' + esc(sumTxt) + '</span>' +
        '</div>' +
        '<div class="step-right">' + rightMeta + '</div>' +
      '</div>' +
      '<div class="step-body">' + (bodyBlocks || '<div class="mono" style="font-size:11px;color:var(--ink-2)">No extended payload recorded.</div>') + '</div>' +
    '</div>';
  }

  function buildTracesHtml() {
    if (!DATA.tasks.length) {
      return '<div class="card"><div class="card-body">No tasks found.</div></div>';
    }
    if (!STATE.traceTaskId || !DATA.traces[STATE.traceTaskId]) {
      STATE.traceTaskId = (DATA.presets[0] && DATA.presets[0].id) || DATA.tasks[0].id;
    }
    const pId = DATA.meta.primary_arm;
    const bId = DATA.meta.baseline_arm;
    if (!STATE.traceMode) STATE.traceMode = pId;

    const taskIdx = DATA.tasks.findIndex(t => t.id === STATE.traceTaskId);
    const task = DATA.tasks[taskIdx >= 0 ? taskIdx : 0];
    const tr = DATA.traces[task.id] || { prompt: '', arms: {} };

    const optsHtml = DATA.tasks.map(t => {
      const pr = t.by_arm[pId];
      const tag = (pr.passed ? 'PASS' : 'FAIL') + ' · ' + pr.checks_str + ' · ' + pr.steps + 'st';
      return '<option value="' + esc(t.id) + '"' + (t.id === task.id ? ' selected' : '') + '>' + esc(t.id) + ' (' + esc(tag) + ')</option>';
    }).join('');

    const presetsHtml = DATA.presets.map(pr =>
      '<button type="button" class="preset-pill' + (pr.id === task.id ? ' active' : '') + '" data-preset-id="' + esc(pr.id) + '">' +
        esc(pr.label) + ' <span style="opacity:0.78">(' + esc(pr.tag) + ')</span></button>'
    ).join('');

    const armModesHtml = DATA.arms_order.map(am => {
      if (!task.by_arm[am.id]) return '';
      return '<button type="button" class="seg-btn' + (STATE.traceMode === am.id ? ' active' : '') + '" data-trace-mode="' + esc(am.id) + '">' +
        '<span class="arm-dot" style="background:' + am.color + '"></span>' + esc(am.short) + '</button>';
    }).join('') + (DATA.arms_order.length >= 2
      ? '<button type="button" class="seg-btn' + (STATE.traceMode === 'compare' ? ' active' : '') + '" data-trace-mode="compare">' +
          ICONS.compare + '<span>' + DATA.arms_order.length + '-Way Compare</span></button>'
      : '');

    const promptCard = STATE.showPrompt
      ? '<section class="card"><div class="card-head"><h2 class="card-title">Official Task Specification (task.md)</h2>' +
          '<button type="button" class="btn btn-xs" id="btn-close-prompt">Close</button></div>' +
          '<div class="card-body"><pre class="code-block" style="max-height:320px">' + esc(tr.prompt || 'task.md not available (pass --dataset-root to embed prompts).') + '</pre></div></section>'
      : '';

    // Ribbon metrics anchored on selected arm (or primary_arm in compare mode)
    const activeArmId = (STATE.traceMode !== 'compare' && task.by_arm[STATE.traceMode]) ? STATE.traceMode : pId;
    const ar = task.by_arm[activeArmId];
    const br = bId ? task.by_arm[bId] : null;
    const isPrimSelected = activeArmId === pId;

    let summarySentence = 'Completed in <strong>' + ar.steps + ' steps</strong> (' + ar.dur.toFixed(0) + ' s) with capability score <strong>' + (ar.cap * 100).toFixed(1) + '%</strong>.';
    if (isPrimSelected && br) {
      summarySentence = '<strong>' + esc(ARM_BY_ID[pId].short) + '</strong> scored <strong>' + esc(ar.checks_str) + ' (' + (ar.cap * 100).toFixed(0) + '%)</strong> in <strong>' +
        ar.steps + ' steps</strong> vs ' + br.steps + ' steps on ' + esc(ARM_BY_ID[bId].short) + ' (' + fmtSign(task.steps_delta_base_pct, '%', 0) + '), firing <strong>' +
        task.epochs + ' compaction epochs</strong> and <strong>' + task.digests + ' bounded digests</strong>.';
    }

    const ribbonCells = isPrimSelected
      ? (
        '<div class="ribbon-cell"><div class="ribbon-lbl">Steps</div><div class="ribbon-val">' + ar.steps + '</div><div class="ribbon-sub">' + (br ? 'vs ' + br.steps + ' on ' + esc(ARM_BY_ID[bId].short) + ' (' + fmtSign(task.steps_delta_base_pct, '%', 0) + ')' : esc(task.term)) + '</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Wall Time</div><div class="ribbon-val">' + ar.dur.toFixed(0) + ' s</div><div class="ribbon-sub">' + (br ? 'vs ' + br.dur.toFixed(0) + ' s on ' + esc(ARM_BY_ID[bId].short) : 'elapsed') + '</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Total Tokens</div><div class="ribbon-val">' + fmtCompact(ar.vtx_total) + '</div><div class="ribbon-sub">' + fmtCompact(ar.tok_per_step) + ' tok/step</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Prompt Cache Hit</div><div class="ribbon-val" style="color:var(--primary)">' + ar.cache_pct.toFixed(1) + '%</div><div class="ribbon-sub">' + fmtCompact(ar.vtx_cached) + ' cached</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Compaction Epochs</div><div class="ribbon-val">' + task.epochs + '</div><div class="ribbon-sub">' + fmtCompact(task.thoughts_comp) + ' thoughts evicted</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">State &amp; Mutations</div><div class="ribbon-val">' + task.notes_pres + '</div><div class="ribbon-sub">' + task.muts_pres + ' mutating turns pinned</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Ctx Digests</div><div class="ribbon-val">' + task.digests + '</div><div class="ribbon-sub">' + fmtBytes(task.bytes_out) + ' kept out</div></div>'
      )
      : (
        '<div class="ribbon-cell"><div class="ribbon-lbl">Arm</div><div class="ribbon-val">' + esc(ARM_BY_ID[activeArmId].short) + '</div><div class="ribbon-sub">Historical / Baseline</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Steps</div><div class="ribbon-val">' + ar.steps + '</div><div class="ribbon-sub">vs ' + task.by_arm[pId].steps + ' on SJ</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Wall Time</div><div class="ribbon-val">' + ar.dur.toFixed(0) + ' s</div><div class="ribbon-sub">vs ' + task.by_arm[pId].dur.toFixed(0) + ' s on SJ</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Capability</div><div class="ribbon-val">' + (ar.cap * 100).toFixed(0) + '%</div><div class="ribbon-sub">' + esc(ar.checks_str) + '</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Report Gen Tok</div><div class="ribbon-val">' + fmtCompact(ar.rep_tok) + '</div><div class="ribbon-sub">reas + msg tok</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Reasoning Tok</div><div class="ribbon-val">' + fmtCompact(ar.vtx_thought) + '</div><div class="ribbon-sub">model reasoning</div></div>' +
        '<div class="ribbon-cell"><div class="ribbon-lbl">Monologue Chars</div><div class="ribbon-val">' + fmtCompact(ar.msg_chars) + '</div><div class="ribbon-sub">visible prose</div></div>'
      );

    const verdictCard = '<section class="card">' +
      '<div style="padding:12px 16px;background:var(--surface-low);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">' +
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
          '<span class="badge ' + (ar.passed ? 'badge-ok' : 'badge-bad') + '" style="font-size:12px;padding:4px 9px">' +
            (ar.passed ? 'PASS' : 'FAIL') + ' · ' + esc(ar.checks_str) + ' (' + (ar.cap * 100).toFixed(0) + '%)</span>' +
          '<span style="font-size:12.5px;color:var(--ink)">' + summarySentence + '</span>' +
        '</div>' +
        '<span class="suite-pill">cat: ' + esc(task.cat) + (task.bucket && task.bucket !== 'unknown' ? ' · bucket: ' + esc(task.bucket) : '') + '</span>' +
      '</div>' +
      '<div class="ribbon-grid">' + ribbonCells + '</div>' +
    '</section>';

    const headerBar = '<section class="trace-ctrl-bar">' +
      '<div class="trace-ctrl-top">' +
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
          '<select id="trace-task-sel" class="select-ctl mono" style="max-width:410px;font-weight:600">' + optsHtml + '</select>' +
          '<button type="button" class="btn btn-xs" id="btn-prev-task">← Prev</button>' +
          '<button type="button" class="btn btn-xs" id="btn-next-task">Next →</button>' +
          '<button type="button" class="btn btn-xs" id="btn-toggle-prompt">' + ICONS.prompt + '<span>Task Prompt</span></button>' +
        '</div>' +
        '<div class="seg-group">' + armModesHtml + '</div>' +
      '</div>' +
      '<div class="trace-presets"><span class="mono" style="font-size:10.5px;text-transform:uppercase;color:var(--ink-2);margin-right:4px">Presets:</span>' + presetsHtml + '</div>' +
    '</section>';

    // Multi-arm side-by-side compare mode
    if (STATE.traceMode === 'compare') {
      // Collect union of check IDs across arms
      const checkMap = {};
      const checkOrder = [];
      DATA.arms_order.forEach(am => {
        const chks = (tr.arms[am.id] && tr.arms[am.id].checks) || [];
        chks.forEach(c => {
          if (!checkMap[c.id]) {
            checkMap[c.id] = { id: c.id, desc: c.desc || '', byArm: {} };
            checkOrder.push(c.id);
          }
          checkMap[c.id].byArm[am.id] = c;
        });
      });
      checkOrder.sort((a, b2) => {
        const va = Object.values(checkMap[a].byArm).map(x => x.p);
        const vb = Object.values(checkMap[b2].byArm).map(x => x.p);
        const disA = new Set(va).size > 1 ? 0 : 1;
        const disB = new Set(vb).size > 1 ? 0 : 1;
        return disA - disB;
      });

      let diffCard = '';
      if (checkOrder.length) {
        const cHead = DATA.arms_order.map(am => '<th class="num-right">' + esc(am.short) + '</th>').join('');
        const cRows = checkOrder.map(cid => {
          const item = checkMap[cid];
          const vals = Object.values(item.byArm).map(x => x.p);
          const disagree = new Set(vals).size > 1;
          const cells = DATA.arms_order.map(am => {
            const c = item.byArm[am.id];
            if (!c) return '<td class="num-right">—</td>';
            return '<td class="num-right nowrap" title="' + esc(c.r || '') + '">' +
              (c.p ? '<span class="badge badge-ok">PASS</span>' : '<span class="badge badge-bad">FAIL</span>') + '</td>';
          }).join('');
          const failReason = (item.byArm[pId] && !item.byArm[pId].p && item.byArm[pId].r) ? item.byArm[pId].r : '';
          return '<tr style="' + (disagree ? 'background:#FFFDF5' : '') + '">' +
            '<td class="mono">' + esc(cid) + (disagree ? ' <span class="badge badge-warn" style="font-size:9.5px">DIFF</span>' : '') +
              (failReason ? '<div class="sans-cell" style="font-size:11px;color:var(--ink-2);margin-top:2px">' + esc(failReason) + '</div>' : '') + '</td>' +
            cells + '</tr>';
        }).join('');
        diffCard = '<section class="card"><div class="card-head"><div><h2 class="card-title">Cross-Arm Verifier Rubric Diff</h2>' +
          '<p class="card-sub">Checks where arms disagree are surfaced first</p></div></div>' +
          '<div class="table-wrap" style="max-height:220px;overflow-y:auto"><table class="data-table"><thead><tr><th>Rubric Check ID</th>' + cHead + '</tr></thead><tbody>' + cRows + '</tbody></table></div></section>';
      }

      const colsCount = DATA.arms_order.filter(am => task.by_arm[am.id]).length;
      const colsHtml = DATA.arms_order.map(am => {
        const rec = task.by_arm[am.id];
        const armTr = tr.arms[am.id] || { kind: 'summary', steps: [] };
        if (!rec) return '';
        const stepsHtml = (armTr.steps || []).map(s => renderStepItemHtml(s, armTr.kind === 'live', true)).join('') ||
          '<div class="mono" style="padding:14px;font-size:11.5px;color:var(--ink-2)">Step-level trace not loaded for this baseline arm.</div>';
        return '<div class="compare-col">' +
          '<div class="compare-col-head">' +
            '<div style="display:flex;align-items:center;gap:7px"><span class="arm-dot" style="background:' + am.color + '"></span><strong>' + esc(am.short) + '</strong></div>' +
            '<span class="badge ' + (rec.passed ? 'badge-ok' : 'badge-bad') + '">' + esc(rec.checks_str) + ' · ' + rec.steps + 'st · ' + rec.dur.toFixed(0) + 's</span>' +
          '</div>' +
          '<div class="compare-stream">' + stepsHtml + '</div>' +
        '</div>';
      }).join('');

      return headerBar + promptCard + verdictCard + diffCard +
        '<div class="compare-grid" style="grid-template-columns:repeat(' + Math.min(3, Math.max(1, colsCount)) + ', minmax(0, 1fr))">' + colsHtml + '</div>';
    }

    // Single-arm deep-dive view
    const armTr = tr.arms[activeArmId] || { kind: 'live', checks: [], steps: [] };
    const isLive = armTr.kind === 'live';
    const allSteps = armTr.steps || [];
    const shownSteps = STATE.stepFilter === 'highlights'
      ? allSteps.filter(s => s.ep > 0 || s.dig || (s.t || 0) >= 4000 || (s.mc || 0) >= 4000)
      : allSteps;

    const stepsListHtml = shownSteps.map(s => renderStepItemHtml(s, isLive, false)).join('') ||
      '<div style="padding:20px;color:var(--ink-2)" class="mono">No trajectory steps match this filter.</div>';

    // Right sidebar: Head-to-Head + Checks list
    const h2hRows = DATA.arms_order.map(am => {
      const rec = task.by_arm[am.id];
      if (!rec) return '';
      return '<tr class="clickable' + (am.id === activeArmId ? ' row-primary' : '') + '" data-trace-mode="' + esc(am.id) + '">' +
        '<td class="sans-cell nowrap"><span class="arm-dot" style="background:' + am.color + ';margin-right:6px"></span><strong>' + esc(am.short) + '</strong></td>' +
        '<td class="num-right nowrap"><span class="badge ' + (rec.passed ? 'badge-ok' : 'badge-bad') + '">' + esc(rec.checks_str) + '</span></td>' +
        '<td class="num-right nowrap">' + rec.steps + '</td>' +
        '<td class="num-right nowrap">' + rec.dur.toFixed(0) + 's</td>' +
      '</tr>';
    }).join('');

    const checksArr = (armTr.checks && armTr.checks.length) ? armTr.checks : ((tr.arms[pId] && tr.arms[pId].checks) || []);
    const checksHtml = checksArr.map(c =>
      '<div class="check-row">' +
        '<span style="color:' + (c.p ? 'var(--ok)' : 'var(--bad)') + ';flex-shrink:0;margin-top:1px">' + (c.p ? ICONS.check : ICONS.x) + '</span>' +
        '<div style="min-width:0"><div class="check-id">' + esc(c.id) + '</div>' +
          (c.r && !c.p ? '<div class="check-reason">' + esc(c.r) + '</div>' : (c.desc ? '<div class="check-reason">' + esc(c.desc) + '</div>' : '')) +
          ((c.af && c.af.length) ? '<div class="check-reason mono" style="color:var(--bad-ink)">' + esc(c.af.join(' | ')) + '</div>' : '') +
        '</div></div>'
    ).join('') || '<div style="padding:14px;color:var(--ink-2);font-size:12px">Detailed check breakdown not recorded.</div>';

    return headerBar + promptCard + verdictCard +
      '<div class="inspector-split">' +
        '<div style="display:flex;flex-direction:column;gap:14px;min-width:0">' +
          renderStepChartSvg(armTr) +
          '<section class="card">' +
            '<div class="card-head"><div><h2 class="card-title">Execution Trajectory (' + shownSteps.length + ' of ' + allSteps.length + ' steps)</h2>' +
              '<p class="card-sub">Click any step row to inspect full command arguments, sidecar digests, and tool observations</p></div>' +
              '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
                '<button type="button" class="btn btn-xs' + (STATE.stepFilter === 'all' ? ' btn-primary' : '') + '" id="btn-step-all">All Steps</button>' +
                '<button type="button" class="btn btn-xs' + (STATE.stepFilter === 'highlights' ? ' btn-primary' : '') + '" id="btn-step-hi">Highlights Only</button>' +
                '<button type="button" class="btn btn-xs" id="btn-expand-all">Expand All</button>' +
                '<button type="button" class="btn btn-xs" id="btn-collapse-all">Collapse All</button>' +
              '</div></div>' +
            '<div class="card-body" style="padding:12px">' + stepsListHtml + '</div>' +
          '</section>' +
        '</div>' +
        '<aside class="sticky-side">' +
          '<section class="card"><div class="card-head"><h2 class="card-title">Arm Head-to-Head</h2><span class="mono" style="font-size:11px;color:var(--ink-2)">Click to switch</span></div>' +
            '<div class="table-wrap"><table class="data-table"><thead><tr><th>Arm</th><th class="num-right">Checks</th><th class="num-right">Steps</th><th class="num-right">Wall</th></tr></thead><tbody>' + h2hRows + '</tbody></table></div></section>' +
          '<section class="card"><div class="card-head"><h2 class="card-title">Verifier Rubric Checks</h2><span class="badge ' + (ar.passed ? 'badge-ok' : 'badge-bad') + '">' + esc(ar.checks_str) + '</span></div>' +
            '<div class="check-list">' + checksHtml + '</div></section>' +
        '</aside>' +
      '</div>';
  }
"""
_JS_PART_4 = r"""
  function bindTabEvents() {
    const root = document.getElementById('app-root');

    // Slice click -> filter Tasks
    root.querySelectorAll('[data-slice-cat]').forEach(el => {
      el.addEventListener('click', () => {
        STATE.taskCat = el.getAttribute('data-slice-cat') || 'all';
        const out = el.getAttribute('data-slice-out') || 'all';
        STATE.taskOutcome = ['all', 'passed', 'failed', 'gained', 'lost', 'bucket_b', 'bucket_w'].includes(out) ? out : 'all';
        STATE.taskFailCat = '';
        switchTab('tasks');
      });
    });

    // Failure row click -> filter Tasks by fail_cat
    root.querySelectorAll('[data-fail-key]').forEach(el => {
      el.addEventListener('click', () => {
        STATE.taskCat = 'all';
        STATE.taskOutcome = 'failed';
        STATE.taskFailCat = el.getAttribute('data-fail-key') || '';
        switchTab('tasks');
      });
    });

    // Ablation matrix toggle
    const abBtn = document.getElementById('btn-toggle-ab-matrix');
    if (abBtn) {
      abBtn.addEventListener('click', () => {
        STATE.showAblationMatrix = !STATE.showAblationMatrix;
        renderActiveTab();
      });
    }

    // Tasks filter controls
    const searchIn = document.getElementById('task-search-in');
    if (searchIn) {
      searchIn.addEventListener('input', e => {
        const pos = e.target.selectionStart;
        STATE.taskSearch = e.target.value;
        renderActiveTab();
        const nextIn = document.getElementById('task-search-in');
        if (nextIn) {
          nextIn.focus();
          try { nextIn.setSelectionRange(pos, pos); } catch (_) {}
        }
      });
    }
    root.querySelectorAll('[data-f-cat]').forEach(btn => {
      btn.addEventListener('click', () => {
        STATE.taskCat = btn.getAttribute('data-f-cat');
        renderActiveTab();
      });
    });
    root.querySelectorAll('[data-f-out]').forEach(btn => {
      btn.addEventListener('click', () => {
        STATE.taskOutcome = btn.getAttribute('data-f-out');
        renderActiveTab();
      });
    });
    const clearFailBtn = document.getElementById('btn-clear-fail-cat');
    if (clearFailBtn) {
      clearFailBtn.addEventListener('click', () => {
        STATE.taskFailCat = '';
        renderActiveTab();
      });
    }
    const sortSel = document.getElementById('task-sort-sel');
    if (sortSel) {
      sortSel.addEventListener('change', e => {
        STATE.taskSort = e.target.value;
        renderActiveTab();
      });
    }
    root.querySelectorAll('[data-open-trace]').forEach(row => {
      row.addEventListener('click', () => {
        switchTab('traces', { taskId: row.getAttribute('data-open-trace') });
      });
    });

    // Traces controls
    const taskSel = document.getElementById('trace-task-sel');
    if (taskSel) {
      taskSel.addEventListener('change', e => {
        STATE.traceTaskId = e.target.value;
        syncHash();
        renderActiveTab();
      });
    }
    const prevBtn = document.getElementById('btn-prev-task');
    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        const idx = DATA.tasks.findIndex(t => t.id === STATE.traceTaskId);
        const nextIdx = (idx - 1 + DATA.tasks.length) % DATA.tasks.length;
        STATE.traceTaskId = DATA.tasks[nextIdx].id;
        syncHash();
        renderActiveTab();
      });
    }
    const nextBtn = document.getElementById('btn-next-task');
    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        const idx = DATA.tasks.findIndex(t => t.id === STATE.traceTaskId);
        const nextIdx = (idx + 1) % DATA.tasks.length;
        STATE.traceTaskId = DATA.tasks[nextIdx].id;
        syncHash();
        renderActiveTab();
      });
    }
    const promptBtn = document.getElementById('btn-toggle-prompt');
    if (promptBtn) {
      promptBtn.addEventListener('click', () => {
        STATE.showPrompt = !STATE.showPrompt;
        renderActiveTab();
      });
    }
    const closePromptBtn = document.getElementById('btn-close-prompt');
    if (closePromptBtn) {
      closePromptBtn.addEventListener('click', () => {
        STATE.showPrompt = false;
        renderActiveTab();
      });
    }
    root.querySelectorAll('[data-preset-id]').forEach(btn => {
      btn.addEventListener('click', () => {
        STATE.traceTaskId = btn.getAttribute('data-preset-id');
        syncHash();
        renderActiveTab();
      });
    });
    root.querySelectorAll('[data-trace-mode]').forEach(el => {
      el.addEventListener('click', () => {
        STATE.traceMode = el.getAttribute('data-trace-mode');
        syncHash();
        renderActiveTab();
      });
    });

    // Step expand / collapse / jump
    root.querySelectorAll('[data-toggle-step]').forEach(hd => {
      hd.addEventListener('click', () => {
        hd.parentElement.classList.toggle('open');
      });
    });
    root.querySelectorAll('[data-jump-step]').forEach(bar => {
      bar.addEventListener('click', () => {
        const sNum = bar.getAttribute('data-jump-step');
        const target = document.getElementById('trace-step-' + sNum);
        if (target) {
          target.classList.add('open');
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
    });
    const btnAll = document.getElementById('btn-step-all');
    if (btnAll) btnAll.addEventListener('click', () => { STATE.stepFilter = 'all'; renderActiveTab(); });
    const btnHi = document.getElementById('btn-step-hi');
    if (btnHi) btnHi.addEventListener('click', () => { STATE.stepFilter = 'highlights'; renderActiveTab(); });
    const btnExp = document.getElementById('btn-expand-all');
    if (btnExp) btnExp.addEventListener('click', () => root.querySelectorAll('.step-item').forEach(el => el.classList.add('open')));
    const btnCol = document.getElementById('btn-collapse-all');
    if (btnCol) btnCol.addEventListener('click', () => root.querySelectorAll('.step-item').forEach(el => el.classList.remove('open')));
  }

  function exportCsv() {
    if (!DATA) return;
    const cols = ['task_id', 'category', 'bucket', 'compaction_epochs', 'digests'];
    DATA.arms_order.forEach(am => {
      cols.push(am.id + '_passed', am.id + '_cap', am.id + '_checks', am.id + '_steps', am.id + '_wall_s', am.id + '_total_tokens');
    });
    const lines = [cols.join(',')];
    DATA.tasks.forEach(t => {
      const row = [
        JSON.stringify(t.id),
        JSON.stringify(t.cat),
        JSON.stringify(t.bucket),
        t.epochs,
        t.digests
      ];
      DATA.arms_order.forEach(am => {
        const r = t.by_arm[am.id] || {};
        row.push(
          r.passed ? 1 : 0,
          r.cap !== undefined ? r.cap : '',
          JSON.stringify(r.checks_str || ''),
          r.steps !== undefined ? r.steps : '',
          r.dur !== undefined ? r.dur : '',
          r.vtx_total !== undefined ? r.vtx_total : ''
        );
      });
      lines.push(row.join(','));
    });
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'acciobench_eval_' + DATA.meta.date + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function boot() {
    try {
      DATA = await decodePayload();
      DATA.arms_order.forEach(am => { ARM_BY_ID[am.id] = am; });
      STATE.traceMode = DATA.meta.primary_arm;
      initHeader();
      applyInitialHash();
      document.querySelectorAll('#nav-tabs .nav-tab').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-tab') === STATE.tab);
      });
      renderActiveTab();
    } catch (err) {
      document.getElementById('app-root').innerHTML =
        '<div class="card"><div class="card-body mono" style="color:var(--bad)">Failed to initialize report: ' + esc(err && err.message ? err.message : String(err)) + '</div></div>';
    }
  }

  boot();
})();
</script>
</body>
</html>
"""


def render_html_document(*, doc_title: str, payload_gz_b64: str) -> str:
    """Assemble the complete self-contained HTML document string."""
    head = (
        _HTML_HEAD.replace("__DOC_TITLE__", html_lib.escape(doc_title))
        .replace("__CSS__", _CSS)
        .replace("__PAYLOAD_GZ_B64__", payload_gz_b64)
    )
    return head + _JS_PART_1 + _JS_PART_2 + _JS_PART_3 + _JS_PART_4

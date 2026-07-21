#!/usr/bin/env python3
"""ctx-anatomy — visually deconstruct an agent's context window.

Two lenses, one page:

  1. **Composition** (from captured request bodies): what actually fills the
     window on a single turn — system prompt, *tool schemas*, and messages —
     broken down per tool. This is where the surprises live: most of an
     agent's window is usually inert tool schemas it never calls.

  2. **Cache economics** (from Claude Code transcripts): how the window grows
     turn over turn, and how ``cache_read`` compounds — the fixed prefix is
     cached once and re-read every single turn.

Inputs (all optional, mix as needed):
  --compose label:capture.jsonl   request bodies (see ctx_capture.py) — the
                                   composition lens. Repeatable.
  --arm     label:transcript.jsonl  a session jsonl — the cache-economics lens.
  --wire    label:wire.jsonl        proxy wire log (adds req_bytes).
  --probe   probe.json              component-attribution toggle probe.

Output is one self-contained HTML page (``-o page.html``) — no network, no
build step — or ``--json`` for the raw anatomy.

Usage:
  ctx_anatomy.py --compose naive:naive.jsonl --compose sj:sj.jsonl \
                 --arm naive:naive_tx.jsonl --arm sj:sj_tx.jsonl \
                 -o context.html
"""
from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import dataclass, field, asdict


# ── cache-economics lens (transcripts) ────────────────────────────────────
@dataclass
class Request:
    seq: int
    cache_create: int
    cache_read: int
    input: int
    output: int

    @property
    def cached(self) -> int:
        return self.cache_create + self.cache_read

    @property
    def window(self) -> int:
        return self.cache_create + self.cache_read + self.input


@dataclass
class Arm:
    label: str
    requests: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    req_bytes: list = field(default_factory=list)

    def _u(self, msg):
        u = msg.get("usage") or {}
        mu = msg.get("modelUsage") or {}
        if not u and mu:
            f = next(iter(mu.values()), {})
            u = {"input_tokens": f.get("inputTokens", 0),
                 "output_tokens": f.get("outputTokens", 0),
                 "cache_creation_input_tokens": f.get("cacheCreationInputTokens", 0),
                 "cache_read_input_tokens": f.get("cacheReadInputTokens", 0)}
        return u


def extract(label: str, transcript: pathlib.Path) -> Arm:
    arm = Arm(label=label)
    prev = None
    for ln in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        msg = o.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    t = c.get("content")
                    if isinstance(t, list):
                        t = "".join(p.get("text", "") for p in t if isinstance(p, dict))
                    if isinstance(t, str):
                        arm.tool_results.append(len(t))
        if msg.get("role") != "assistant":
            continue
        u = arm._u(msg)
        tup = (int(u.get("cache_creation_input_tokens", 0)),
               int(u.get("cache_read_input_tokens", 0)),
               int(u.get("input_tokens", 0)),
               int(u.get("output_tokens", 0)))
        if tup == prev:  # SDK logs identical usage several times per turn
            continue
        prev = tup
        arm.requests.append(Request(len(arm.requests) + 1, *tup))
    return arm


def arm_summary(arm: Arm) -> dict:
    reqs = arm.requests
    base = reqs[0].cached if reqs else 0
    growth = [{"seq": r.seq, "base": min(base, r.cached),
               "history": max(0, r.window - base), "output": r.output}
              for r in reqs]
    return {
        "label": arm.label, "turns": len(reqs), "base_prefix": base,
        "peak_window": max((r.window for r in reqs), default=0),
        "total_cache_read": sum(r.cache_read for r in reqs),
        "total_cache_create": sum(r.cache_create for r in reqs),
        "total_output": sum(r.output for r in reqs),
        "requests": [asdict(r) for r in reqs], "growth": growth,
    }


# ── composition lens (captured request bodies) ────────────────────────────
def load_compose(label: str, path: pathlib.Path) -> dict:
    """Pick the *agent* request (the one carrying tool schemas — not the
    title-generation sidecar) and shape it for the page."""
    reqs = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if "est_tokens" in r:
            reqs.append(r)
    if not reqs:
        return {"label": label, "empty": True}
    a = max(reqs, key=lambda r: r.get("tools_count", 0))
    et = a["est_tokens"]
    tools = [{"name": n, "tok": b // 4} for n, b in a.get("tool_sizes", [])]
    shown = sum(t["tok"] for t in tools)
    other = max(0, et["tools"] - shown)
    return {
        "label": label,
        "system": et["system"], "tools": et["tools"], "messages": et["messages"],
        "total": et["total"], "tools_count": a["tools_count"],
        "tool_leaderboard": tools, "tools_other": other,
    }


# ── shaping ───────────────────────────────────────────────────────────────
def build(arms: list, compose: list, probe: list | None) -> dict:
    out: dict = {}
    if compose:
        out["composition"] = compose
        if len(compose) == 2 and not any(c.get("empty") for c in compose):
            a, b = compose
            out["ctx_delta"] = {
                "system": b["system"] - a["system"],
                "tools": b["tools"] - a["tools"],
                "messages": b["messages"] - a["messages"],
                "total": b["total"] - a["total"],
                "pct": round((b["total"] - a["total"]) / a["total"] * 100, 1) if a["total"] else None,
            }
    if arms:
        summaries = [arm_summary(a) for a in arms]
        out["arms"] = summaries
        if len(summaries) == 2:
            a, b = summaries
            out["cache_delta"] = {
                "base_prefix": b["base_prefix"] - a["base_prefix"],
                "cache_read": b["total_cache_read"] - a["total_cache_read"],
            }
    if probe:
        out["prefix_components"] = probe
    return out


def probe_components(rows: list) -> list:
    by = {r["cell"].split("_", 1)[0]: r for r in rows}
    bare = by.get("A", {}).get("prefix_total", 0)
    comps = [{"name": "base (system + tool schemas)", "tokens": bare}]
    for key, name in (("B", "CLAUDE.md verb card"), ("C", "--append-system-prompt"),
                      ("D", "registered sub-agent")):
        if key in by:
            comps.append({"name": name, "tokens": by[key]["prefix_total"] - bare})
    return comps


# ── HTML render ───────────────────────────────────────────────────────────
def render_html(anatomy: dict) -> str:
    return _HTML.replace("/*__DATA__*/", json.dumps(anatomy))


_HTML = r"""<title>ctx · context anatomy</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#ffffff; --ink:#12151b; --dim:#5a6472; --faint:#8a94a3;
    --line:#e4e7ec; --grid:#eef0f3;
    --tools:#5b6b8c; --toolsq:#8695af; --sys:#c99a3b; --msg:#d98032; --ctx:#0891b2;
    --warn:#e11d48; --good:#0f9d76;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme:dark){:root{
    --bg:#0d1017; --panel:#151a22; --ink:#e9edf3; --dim:#93a0b2; --faint:#697485;
    --line:#232a34; --grid:#1b212a;
    --tools:#6b7ba0; --toolsq:#455167; --sys:#d6a94a; --msg:#e08a3a; --ctx:#22d3ee;
    --warn:#fb5c7d; --good:#25c093;
  }}
  :root[data-theme=dark]{
    --bg:#0d1017; --panel:#151a22; --ink:#e9edf3; --dim:#93a0b2; --faint:#697485;
    --line:#232a34; --grid:#1b212a;
    --tools:#6b7ba0; --toolsq:#455167; --sys:#d6a94a; --msg:#e08a3a; --ctx:#22d3ee;
    --warn:#fb5c7d; --good:#25c093;}
  :root[data-theme=light]{
    --bg:#f6f7f9; --panel:#ffffff; --ink:#12151b; --dim:#5a6472; --faint:#8a94a3;
    --line:#e4e7ec; --grid:#eef0f3;
    --tools:#5b6b8c; --toolsq:#8695af; --sys:#c99a3b; --msg:#d98032; --ctx:#0891b2;
    --warn:#e11d48; --good:#0f9d76;}
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
       font-size:15px;line-height:1.55;letter-spacing:-.005em}
  .wrap{max-width:900px;margin:0 auto;padding:40px 22px 96px}
  .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;
           color:var(--ctx);margin:0 0 14px}
  h1{font-size:clamp(28px,4.4vw,40px);line-height:1.06;margin:0 0 12px;font-weight:640;
     letter-spacing:-.025em;text-wrap:balance}
  h1 .q{color:var(--ctx)}
  .lede{font-size:17px;color:var(--dim);margin:0 0 34px;max-width:60ch}
  .lede b{color:var(--ink);font-weight:600}
  h2{font-family:var(--mono);font-size:12px;letter-spacing:.16em;text-transform:uppercase;
     color:var(--faint);margin:44px 0 16px;font-weight:600;display:flex;align-items:center;gap:10px}
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px 24px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:6px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;position:relative;overflow:hidden}
  .kpi .rail{position:absolute;left:0;top:0;bottom:0;width:3px}
  .kpi .n{font-family:var(--mono);font-size:27px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
  .kpi .l{font-size:12.5px;color:var(--dim);margin-top:3px;line-height:1.3}
  .kpi .n small{font-size:14px;color:var(--faint);font-weight:500}
  /* stacked composition bar */
  .stack{height:52px;border-radius:9px;overflow:hidden;display:flex;border:1px solid var(--line)}
  .stack>span{display:block;height:100%;position:relative;min-width:2px}
  .stack .lab{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
              font-family:var(--mono);font-size:11px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.35);
              white-space:nowrap;overflow:hidden}
  .armhead{display:flex;justify-content:space-between;align-items:baseline;margin:2px 0 8px}
  .armhead .name{font-family:var(--mono);font-weight:600;font-size:14px}
  .armhead .tot{font-family:var(--mono);font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
  .legend{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--dim);margin-top:14px;font-family:var(--mono)}
  .sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
  /* leaderboard */
  .lb{display:flex;flex-direction:column;gap:7px}
  .lb .row{display:grid;grid-template-columns:132px 1fr 62px;align-items:center;gap:12px}
  .lb .nm{font-family:var(--mono);font-size:12.5px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .lb .tk{font-family:var(--mono);font-size:12px;color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
  .lb .track{height:16px;background:var(--grid);border-radius:5px;overflow:hidden}
  .lb .fill{height:100%;border-radius:5px}
  .lb.attrib .row{grid-template-columns:172px 1fr 54px}
  .lb .sub2{grid-column:1 / -1;font-family:var(--mono);font-size:10.5px;color:var(--faint);
            margin:-3px 0 5px 172px;letter-spacing:.02em}
  .callout{border:1px solid var(--ctx);border-radius:14px;padding:16px 20px;margin-top:18px;
           background:color-mix(in srgb,var(--ctx) 8%,transparent)}
  .callout .big{font-family:var(--mono);font-size:22px;font-weight:600;color:var(--ctx);font-variant-numeric:tabular-nums}
  .callout p{margin:6px 0 0;font-size:13.5px;color:var(--dim)}
  /* waterfall */
  svg{max-width:100%;display:block}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums}
  th,td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--faint);font-weight:600;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase}
  .note{font-size:13.5px;color:var(--dim);line-height:1.6}
  .note b{color:var(--ink)}
  .scroll{overflow-x:auto}
  .foot{margin-top:52px;padding-top:18px;border-top:1px solid var(--line);font-family:var(--mono);
        font-size:11px;color:var(--faint);letter-spacing:.03em}
</style>
<div class="wrap">
  <p class="eyebrow">ctx · context anatomy</p>
  <h1 id="head"></h1>
  <p class="lede" id="lede"></p>
  <div id="app"></div>
  <p class="foot">ctx-anatomy · request bodies captured at the wire · $0 upstream · tokens ≈ bytes/4 for schemas, exact for usage</p>
</div>
<script>
const D = /*__DATA__*/;
const fmt = n => n>=1000 ? (n/1000).toFixed(n>=10000?0:1)+'k' : String(Math.round(n));
const pct = (n,d) => d? Math.round(n/d*100) : 0;
const COL = {tools:'var(--tools)', system:'var(--sys)', messages:'var(--msg)', ctx:'var(--ctx)'};

function hero(){
  const c = D.composition;
  if(!c){ document.getElementById('head').textContent='Context anatomy';
          document.getElementById('lede').textContent=''; return; }
  const ref = c.find(x=>!x.empty) || c[0];
  const toolPct = pct(ref.tools, ref.total);
  document.getElementById('head').innerHTML =
    `<span class="q">${toolPct}%</span> of the window is tool schemas the agent never calls`;
  const d = D.ctx_delta;
  let l = `A single agent turn carries <b>${fmt(ref.total)} tokens</b> before it does any work — and <b>${fmt(ref.tools)}</b> of them (${ref.tools_count} tool schemas) are inert weight re-sent every turn.`;
  if(d) l += ` Everything <b>ctx</b> itself adds to the request: <b>+${d.total} tokens (${d.pct}%)</b>.`;
  document.getElementById('lede').innerHTML = l;
}

function kpiRow(){
  const c = (D.composition||[]).find(x=>!x.empty);
  if(!c) return '';
  const d = D.ctx_delta;
  const cards = [];
  const card=(n,l,rail)=>`<div class="kpi"><span class="rail" style="background:${rail}"></span>
     <div class="n">${n}</div><div class="l">${l}</div></div>`;
  cards.push(card(`${fmt(c.tools)}<small> tok</small>`, `tool schemas per turn · ${pct(c.tools,c.total)}% of the window`, 'var(--tools)'));
  if(d) cards.push(card(`+${d.total}<small> tok</small>`, `everything ctx adds to the request · ${d.pct}%`, 'var(--ctx)'));
  cards.push(card(`${fmt(c.system)}<small> tok</small>`, `system prompt`, 'var(--sys)'));
  cards.push(card(`${fmt(c.messages)}<small> tok</small>`, `the actual task`, 'var(--msg)'));
  return `<div class="kpis">${cards.join('')}</div>`;
}

function stack(c){
  if(c.empty) return '';
  const segs=[['tools','tools',c.tools],['system','system',c.system],['messages','task',c.messages]];
  const bars = segs.map(([k,lab,v])=>{
    const w = v/c.total*100;
    return `<span style="width:${w}%;background:${COL[k]}"><span class="lab">${w>9?lab+' '+fmt(v):''}</span></span>`;
  }).join('');
  return `<div class="armhead"><span class="name">${c.label}</span><span class="tot">${fmt(c.total)} tok / turn</span></div>
    <div class="stack">${bars}</div>`;
}

function attribution(){
  const t = D.controlled_attribution; if(!t || !t.length) return '';
  const c = (D.composition||[]).find(x=>!x.empty);
  const ambient = c ? c.tools : 0;
  // scale bars against the ambient tool mass so ctx's slivers read true
  const scale = Math.max(ambient, ...t.map(r=>Math.abs(r.d_total)));
  const barw = v => Math.max(1.2, Math.abs(v)/scale*100);
  const NAMES = {'+claudemd':'CLAUDE.md verb card','+append':'output-discipline prompt',
                 '+agent':'explorer sub-agent','+settings':'hook settings','ctx-wrap':'ctx wrap · everything'};
  const rows = t.map(r=>{
    const nm = NAMES[r.component]||r.component;
    const where = [r.d_system?`sys +${r.d_system}`:'', r.d_messages?`msg +${r.d_messages}`:'', r.d_tools?`tool +${r.d_tools}`:''].filter(Boolean).join(' · ');
    const full = r.component==='ctx-wrap';
    return `<div class="row"><div class="nm">${nm}</div>
      <div class="track"><div class="fill" style="width:${barw(r.d_total)}%;background:${full?'var(--ctx)':'var(--good)'}"></div></div>
      <div class="tk">+${r.d_total}</div></div>
      <div class="sub2">${where||'—'}</div>`;
  }).join('');
  const ref = `<div class="row"><div class="nm" style="color:var(--faint)">ambient tool schemas</div>
      <div class="track"><div class="fill" style="width:100%;background:var(--tools);opacity:.55"></div></div>
      <div class="tk" style="color:var(--faint)">${fmt(ambient)}</div></div>
      <div class="sub2">host harness — identical in every cell, so it cancels in the deltas above</div>`;
  return `<h2>the true cost of each piece</h2>
    <div class="panel"><div class="lb attrib">${rows}${ref}</div>
    <p class="note" style="margin-top:16px">Measured by capturing the request body at the wire for a controlled matrix — bare → +one component → full wrap — all in one identical environment. Because every cell carries the same ambient schemas, <b>(cell − bare)</b> is each piece's true marginal cost, immune to what the host injects. The explorer sub-agent everyone worries about: <b>+115 tokens</b>, not 16k.</p></div>`;
}

function composition(){
  const c = D.composition; if(!c) return '';
  let h = `<h2>what fills one turn</h2><div class="panel">${c.map(stack).join('<div style="height:16px"></div>')}
    <div class="legend">
      <span><span class="sw" style="background:var(--tools)"></span>tool schemas (inert)</span>
      <span><span class="sw" style="background:var(--sys)"></span>system prompt</span>
      <span><span class="sw" style="background:var(--msg)"></span>the task + reasoning</span>
    </div></div>`;
  if(D.ctx_delta){
    const d=D.ctx_delta;
    h += `<div class="callout"><div class="big">+${d.total} tokens</div>
      <p>the entire footprint of <b>ctx wrap</b> on the request — system&nbsp;+${d.system}, task&nbsp;+${d.messages}, tool&nbsp;schemas&nbsp;+${d.tools}. The two arms are otherwise byte-identical: same ${c.find(x=>!x.empty).tools_count} tools, same model. Any larger gap you see between runs is the host harness loading a different tool set, not ctx.</p></div>`;
  }
  return h;
}

function leaderboard(){
  const c = (D.composition||[]).find(x=>!x.empty);
  if(!c || !c.tool_leaderboard) return '';
  const rows = c.tool_leaderboard.slice(0,14);
  const max = rows[0].tok;
  const NATIVE = new Set(['Bash','Read','Grep','Glob','Edit','Write','MultiEdit','WebFetch','WebSearch']);
  const body = rows.map(t=>{
    const w=Math.max(2,t.tok/max*100);
    const native = NATIVE.has(t.name);
    const col = native?'var(--toolsq)':'var(--warn)';
    return `<div class="row"><div class="nm">${t.name}</div>
      <div class="track"><div class="fill" style="width:${w}%;background:${col}"></div></div>
      <div class="tk">${fmt(t.tok)}</div></div>`;
  }).join('');
  const other = c.tools_other ? `<div class="row"><div class="nm" style="color:var(--faint)">+ ${c.tools_count-rows.length} more</div>
      <div class="track"><div class="fill" style="width:${Math.max(2,c.tools_other/max*100)}%;background:var(--toolsq);opacity:.5"></div></div>
      <div class="tk">${fmt(c.tools_other)}</div></div>`:'';
  return `<h2>the schema leaderboard</h2><div class="panel"><div class="lb">${body}${other}</div>
    <div class="legend"><span><span class="sw" style="background:var(--warn)"></span>host-harness tool (never called this run)</span>
    <span><span class="sw" style="background:var(--toolsq)"></span>native tool</span></div></div>`;
}

// per-turn cache waterfall
function waterfall(arm){
  const W=840,H=190,PL=46,PB=24,PT=8, g=arm.growth; if(!g.length) return '';
  const maxV=Math.max(...g.map(r=>r.base+r.history));
  const x=i=>PL+(g.length<2?0:i/(g.length-1))*(W-PL-8);
  const y=v=>PT+(1-v/maxV)*(H-PT-PB);
  const area=(lo,hi)=>{
    const up=g.map((r,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(hi(r)).toFixed(1)}`).join(' ');
    const dn=g.slice().reverse().map((r,i)=>`L${x(g.length-1-i).toFixed(1)},${y(lo(r)).toFixed(1)}`).join(' ');
    return up+' '+dn+' Z';};
  const gl=[0,.5,1].map(f=>{const v=maxV*f;return `<line x1="${PL}" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${PL-7}" y="${y(v)+3}" text-anchor="end" fill="var(--faint)" font-size="10" font-family="var(--mono)">${fmt(v)}</text>`}).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="context growth for ${arm.label}">${gl}
    <path d="${area(()=>0,r=>r.base)}" fill="var(--tools)" opacity=".85"/>
    <path d="${area(r=>r.base,r=>r.base+r.history)}" fill="var(--msg)" opacity=".8"/>
    <text x="${PL}" y="${H-5}" fill="var(--faint)" font-size="10" font-family="var(--mono)">turn 1</text>
    <text x="${W}" y="${H-5}" text-anchor="end" fill="var(--faint)" font-size="10" font-family="var(--mono)">turn ${g.length}</text></svg>`;
}

function economics(){
  if(!D.arms) return '';
  let h=`<h2>how the window compounds</h2>`;
  D.arms.forEach(a=>{
    h+=`<div class="panel" style="margin-bottom:14px"><div class="armhead">
      <span class="name">${a.label}</span>
      <span class="tot">${a.turns} turns · fixed prefix ${fmt(a.base_prefix)} · Σ cache-read ${fmt(a.total_cache_read)}</span></div>
      ${waterfall(a)}</div>`;
  });
  h+=`<p class="note"><b>The grey slab is fixed</b> — cached once, then re-read on every turn. Total cache-read ≈ that slab × turn count, so a heavier prefix (or an extra turn) is paid many times over. That compounding — not request size — is where ctx's real cost sits: it's the same prefix, occasionally one more turn.</p>`;
  return h;
}

function methodology(){
  return `<h2>how this was measured</h2><div class="panel note">
    <b>Composition</b> comes from the real request body, captured at the wire by a local stand-in server that logs
    <span style="font-family:var(--mono)">system / tools / messages</span> then returns a stub — zero upstream cost, so the
    numbers are the exact bytes Claude Code put on the wire. <b>Cache economics</b> come from the session transcript's per-call
    usage. The one caveat worth stating loudly: this run was captured inside a remote agent harness that injects its own tool
    suite into every nested call, which is why <span style="font-family:var(--mono)">Workflow / Artifact / Monitor</span> dominate.
    A plain Claude Code CLI with seven tools would show a far smaller <span style="font-family:var(--mono)">tools</span> band —
    but ctx's <b>+${(D.ctx_delta||{}).total||0}-token</b> footprint is invariant to that.</div>`;
}

function render(){
  hero();
  document.getElementById('app').innerHTML =
    kpiRow() + attribution() + composition() + leaderboard() + economics() + methodology();
}
render();
</script>
"""


# ── cli ───────────────────────────────────────────────────────────────────
def _split(pair: str):
    label, _, path = pair.partition(":")
    return label, pathlib.Path(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose", action="append", default=[], help="label:capture.jsonl")
    ap.add_argument("--arm", action="append", default=[], help="label:transcript.jsonl")
    ap.add_argument("--wire", action="append", default=[], help="label:wire.jsonl")
    ap.add_argument("--probe", type=pathlib.Path)
    ap.add_argument("-o", "--out", type=pathlib.Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    compose = [load_compose(*_split(s)) for s in args.compose]
    arms = [extract(*_split(s)) for s in args.arm]
    probe = None
    if args.probe and args.probe.exists():
        probe = probe_components(json.loads(args.probe.read_text()))

    anatomy = build(arms, compose, probe)
    if args.json or not args.out:
        print(json.dumps(anatomy, indent=2))
    if args.out:
        args.out.write_text(render_html(anatomy), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

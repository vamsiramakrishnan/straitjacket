// Grounded receipts and data sourced directly from ./straitjacket/evals/ and ./straitjacket/docs/

export const EVIDENCE_FATES = [
  {
    id: 'straitjacket',
    modeIndex: 0,
    label: 'Straitjacket ctx run',
    tag: 'CONTAINED · ADDRESSABLE',
    tone: 'blue',
    tokens: 531,
    reduction: '−99.82%',
    barPct: 0.4,
    quietNeedle: 'Preserved (line 14,238)',
    address: 'run:8956c843e556#stdout',
    summary:
      'Captures all 20,001 lines (979.1 KiB) once on disk, clusters 11 log templates covering 100% of lines, surfaces the quiet anomaly at line 14,238, and emits a deterministic retrieval handle.',
  },
  {
    id: 'naive',
    modeIndex: 1,
    label: 'Naive raw flood',
    tag: 'RESIDENT FLOOD',
    tone: 'red',
    tokens: 302628,
    reduction: '0%',
    barPct: 100,
    quietNeedle: 'Survived (buried in flood)',
    address: 'None',
    summary:
      'Dumps all 302,628 o200k tokens straight into the active transcript. The entire log stays resident and is re-read on every subsequent turn.',
  },
  {
    id: 'trunc',
    modeIndex: 2,
    label: 'Head + Tail truncation',
    tag: 'SILENT LOSS',
    tone: 'red',
    tokens: 1219,
    reduction: '−99.60%',
    barPct: 1.8,
    quietNeedle: 'LOST (middle discarded)',
    address: 'None',
    summary:
      'Slices out the middle of the 20,001-line log without a handle or coverage receipt. The quiet needle at line 14,238 vanishes silently.',
  },
];

export const FOUR_GATES = [
  {
    id: 'birth',
    num: '01',
    name: 'BIRTH',
    question: 'Can this operation flood at the source?',
    detail:
      'Intercepts commands, shell pipelines, and test runners via ctx run before raw megabytes ever touch the conversation transcript.',
    command: 'ctx run -- pytest -q',
    ringX: -0.45,
  },
  {
    id: 'entry',
    num: '02',
    name: 'ENTRY',
    question: 'What actually crossed the host boundary?',
    detail:
      'Host hooks and wire observers measure bytes across commands, MCP tools, and web fetches—replacing oversized results deterministically.',
    command: 'ctx setup && ctx doctor',
    ringX: -0.15,
  },
  {
    id: 'residence',
    num: '03',
    name: 'RESIDENCE',
    question: 'What stays in active context across turns?',
    detail:
      'Keeps only the 531-token digest resident while 64 MiB/stream artifacts sit safely on disk; retrieves 21 lines only when needed.',
    command: 'ctx get run:8956c843e556#stdout --lines 14228:14248',
    ringX: 0.15,
  },
  {
    id: 'emission',
    num: '04',
    name: 'EMISSION',
    question: 'What should the worker hand back out?',
    detail:
      'Workers cite immutable handles and anchored repo ranges instead of pasting raw payloads or forwarding conversation transcripts.',
    command: 'ctx search run:8956c843e556#stdout "AssertionError"',
    ringX: 0.45,
  },
];

export const ANCHOR_SCENARIOS = [
  {
    id: 'insert-above',
    label: 'Insert Above',
    cases: 480,
    relocated: 478,
    verified: 2,
    refused: 0,
    anchoredWrong: 0,
    unanchoredWrong: 478,
    status: 'RELOCATED',
    tone: 'green',
    shiftOffset: 0.34,
    beforeLines: [
      { n: 40, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 41, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 42, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    afterLines: [
      { n: 40, code: 'import hashlib  # inserted', inserted: true },
      { n: 41, code: 'from dataclasses import dataclass', inserted: true },
      { n: 42, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 43, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 44, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    verdict:
      'Content digest @07407f1c shifted from L40:42 to L42:44. Straitjacket verified the hash and relocated the span automatically.',
  },
  {
    id: 'delete-above',
    label: 'Delete Above',
    cases: 480,
    relocated: 480,
    verified: 0,
    refused: 0,
    anchoredWrong: 0,
    unanchoredWrong: 480,
    status: 'RELOCATED',
    tone: 'green',
    shiftOffset: -0.32,
    beforeLines: [
      { n: 40, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 41, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 42, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    afterLines: [
      { n: 35, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 36, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 37, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    verdict:
      'Five unused lines above were deleted. Unanchored line numbers returned wrong code 480/480 times; anchored lookup relocated 480/480.',
  },
  {
    id: 'move-the-span',
    label: 'Move Span',
    cases: 480,
    relocated: 480,
    verified: 0,
    refused: 0,
    anchoredWrong: 0,
    unanchoredWrong: 480,
    status: 'RELOCATED',
    tone: 'blue',
    shiftOffset: 0.56,
    beforeLines: [
      { n: 40, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 41, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 42, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    afterLines: [
      { n: 118, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 119, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 120, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    verdict:
      'Function moved 78 lines down during a refactor. Anchor hash @07407f1c tracked the exact content block with zero false matches.',
  },
  {
    id: 'rewrite-the-span',
    label: 'Rewrite Span',
    cases: 480,
    relocated: 14,
    verified: 0,
    refused: 466,
    anchoredWrong: 0,
    unanchoredWrong: 480,
    status: 'REFUSED · SAFE',
    tone: 'amber',
    shiftOffset: 0.0,
    beforeLines: [
      { n: 40, code: 'def parse_ref(raw: str) -> Ref:', highlight: true },
      { n: 41, code: '    scheme, rest = split_scheme(raw)', highlight: true },
      { n: 42, code: '    return Ref(scheme, rest)', highlight: true },
    ],
    afterLines: [
      { n: 40, code: 'def parse_ref(raw: str, strict=True):', rewritten: true },
      { n: 41, code: '    return Ref.from_uri(raw, strict)', rewritten: true },
    ],
    verdict:
      'The underlying code was rewritten. Instead of silently returning stale lines, Straitjacket refuses with action="refresh_and_replan" — leaving the file untouched.',
  },
];

export const HARNESSES = [
  { id: 'claude', name: 'Claude Code', icon: './icons/claude.svg', bay: 0, role: 'Plan & Implement', mono: true },
  { id: 'codex', name: 'Codex', icon: './icons/codex.svg', bay: 1, role: 'Verify & Patch', mono: true },
  { id: 'antigravity', name: 'Antigravity', icon: './icons/antigravity.png', bay: 2, role: 'Explore & Gemini 4' },
  { id: 'hermes', name: 'Hermes', icon: './icons/hermes.svg', bay: 3, role: 'Rewrite & Filter', mono: true },
  { id: 'omp', name: 'Oh My Pi', icon: './icons/omp.svg', bay: 4, role: 'Bounded Search' },
  { id: 'opencode', name: 'OpenCode', icon: './icons/opencode.svg', bay: 5, role: 'ACP Worker' },
  { id: 'dsh', name: 'DeepSeek Harness', icon: './icons/dsh.svg', bay: 6, role: 'Gate & Edit' },
];

export const ACP_ROUTES = [
  {
    id: 'general',
    label: 'General Feature Task',
    cmd: 'ctx orchestrate "Add anchor recovery receipt" --dry-run',
    activeBays: [2, 0, 5, 1],
    steps: [
      { stage: 'Explore', harness: 'Antigravity', tier: 'economy', handoff: 'repo:src/ctx/refs.py@07407f1c' },
      { stage: 'Plan', harness: 'Claude Code', tier: 'frontier', handoff: 'checkpoint:plan-01 (420 tok)' },
      { stage: 'Implement', harness: 'OpenCode', tier: 'standard', handoff: 'patch:verified-diff' },
      { stage: 'Verify', harness: 'Codex', tier: 'economy', handoff: 'run:ba3d1020ee8f (exit 0)' },
    ],
  },
  {
    id: 'small-edit',
    label: 'Targeted Small Edit',
    cmd: 'ctx orchestrate "Fix off-by-one in span slicer" --dry-run',
    activeBays: [0, 1],
    steps: [
      { stage: 'Implement', harness: 'Claude Code', tier: 'standard', handoff: 'sealed-edit-plan@a4536d5d' },
      { stage: 'Verify', harness: 'Codex', tier: 'economy', handoff: 'run:9c4e71a20b1f (exit 0)' },
    ],
  },
  {
    id: 'review',
    label: 'Diff Review',
    cmd: 'ctx orchestrate "Review the current diff" --dry-run',
    activeBays: [6, 3],
    steps: [
      { stage: 'Review', harness: 'DeepSeek Harness', tier: 'standard', handoff: 'diff:HEAD~1 (bounded)' },
      { stage: 'Relay Check', harness: 'Hermes', tier: 'economy', handoff: 'verdict:approved' },
    ],
  },
  {
    id: 'verify',
    label: 'Named Test Verification',
    cmd: 'ctx orchestrate "Run tests/test_acp.py" --dry-run',
    activeBays: [4, 1],
    steps: [
      { stage: 'Search', harness: 'Oh My Pi', tier: 'economy', handoff: 'sym:test_acp_stdio' },
      { stage: 'Verify', harness: 'Codex', tier: 'economy', handoff: 'run:8d8335db6848#stdout' },
    ],
  },
];

export const MEASURED_RECEIPTS = [
  {
    workload: '20,001-line log fixture',
    receipt: 'evals/field-needle-2026-07-20.md',
    metric: '531 vs 302,628 tok',
    tone: 'blue',
    detail:
      'Retained the quiet event at line 14,238 and emitted a deterministic retrieval address for full inspection.',
  },
  {
    workload: 'Source ranges after edits',
    receipt: 'evals/anchor-drift-2026-08-20.md',
    metric: '0 / 1,920 wrong',
    tone: 'green',
    detail:
      '1,452 relocated, 2 verified in place, 466 explicitly refused, 0 wrong content (vs 1,918 silently wrong unanchored).',
  },
  {
    workload: 'DeepSWE · 8 Python tasks · Haiku',
    receipt: 'evals/agentbench/deepswe-2026-09-13.md',
    metric: '177 vs 54 tests',
    tone: 'green',
    detail:
      'Wrapped arm passed 177 held-out fail-to-pass test IDs vs 54 for plain Claude ($5.74 vs $5.42). Neither arm fully resolved a task.',
  },
  {
    workload: 'File discovery · 14 DeepSWE tasks',
    receipt: 'evals/agentbench/results/pack_recall.json',
    metric: '54.0% vs 42.9%',
    tone: 'blue',
    detail:
      'Top-8 ctx pack suggestions found 54.0% of reference-fix files on average vs 42.9% for grep-count ranking (grep hit >=1 file on 14/14, pack on 12/14).',
  },
  {
    workload: 'Three live canary tasks',
    receipt: 'evals/agentbench/RESULTS.md',
    metric: '97.5% cache · $0.92',
    tone: 'amber',
    detail:
      'Both arms solved 3/3. Median cache hit rose from 96.5% to 97.5%, while total cost rose from $0.67 to $0.92 — reported transparently.',
  },
];

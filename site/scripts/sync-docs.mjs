// Sync ../docs/*.md into src/content/docs/ as Starlight pages.
//
// The repo's markdown under docs/ stays the single source of truth. This script:
//   1. strips the GitHub-only chrome (SVG banner + breadcrumb + H1);
//   2. adds Starlight frontmatter (title + description);
//   3. rewrites inter-doc links so they resolve on the published site:
//        - a link to another synced doc  (FOO.md)      -> its site slug
//        - a link to the docs index      (README.md)   -> the site home
//        - a link that escapes docs/      (../evals/…)  -> a GitHub URL
//      (absolute URLs, in-page anchors, and asset links are left untouched;
//       SVG embeds already use absolute raw.githubusercontent URLs.)
//
// PAGES is the set published by Starlight. The sidebar in ../astro.config.mjs
// intentionally exposes only the product path; specialist pages may still be
// published so existing links remain stable.
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const docsDir = join(here, '..', '..', 'docs');
const outDir = join(here, '..', 'src', 'content', 'docs');

const BASE = '/straitjacket'; // must match `base` in astro.config.mjs
const OWNER_REPO = 'vamsiramakrishnan/straitjacket';
const GH_BLOB = `https://github.com/${OWNER_REPO}/blob/main/`;
const GH_TREE = `https://github.com/${OWNER_REPO}/tree/main/`;

// [ source filename, site slug, title, description ]
const PAGES = [
  // Start here
  ['HOW-IT-WORKS.md', 'start/how-it-works', 'How it works', 'A ten-minute, plain-language walkthrough of one command through the whole system.'],
  ['GETTING-STARTED.md', 'start/getting-started', 'Getting started', 'From a checkout to one harnessed session, then capture, inspect, and retrieve.'],
  ['CONCEPTS.md', 'start/concepts', 'Core concepts', 'The vocabulary: artifact, handle, span, digest, profile, contract, plan, and the four gates.'],
  // Guides
  ['USE-CASES.md', 'guides/use-cases', 'Use cases', 'Choose the shortest path through the harness by task and failure mode.'],
  ['HOST-CAPABILITIES.md', 'guides/host-capabilities', 'Host capabilities', 'What Claude Code, Codex, and Antigravity can enforce before and after tool execution.'],
  ['ANCHORS.md', 'guides/anchors', 'Anchored addresses', 'How repository addresses verify, relocate, or refuse after content moves.'],
  ['CLI.md', 'guides/cli', 'CLI guide', 'Choose a verb, retrieve evidence, and interpret the session scorecard.'],
  ['CONFIGURATION.md', 'guides/configuration', 'Configuration', 'Every ctx.toml setting: budgets, the guard, scopes, redaction, and the store.'],
  ['TROUBLESHOOTING.md', 'guides/troubleshooting', 'Troubleshooting & FAQ', 'Symptom to cause to fix for setup, steering, permissions, the store, and the proxy.'],
  ['WHY-STRAITJACKET.md', 'guides/why-straitjacket', 'Why straitjacket', 'The failure, design decision, shipped mechanism, limits, and counterexamples.'],
  ['COMPARISONS.md', 'guides/comparisons', 'Comparisons', 'Head-to-head data versus Headroom, rtk, Ponytail, Caveman, Maki, and the field.'],
  ['ALPHAEVOLVE-OPTIMIZATION.md', 'guides/alphaevolve-benefits', 'AlphaEvolve benefits', 'What AlphaEvolve measurably improved, what it made safer, and which results remain modeled or rejected.'],
  // Contributing
  ['ARCHITECTURE.md', 'contributing/architecture', 'Architecture & code map', 'Every src/ctx module mapped to its plane, with a which-file-do-I-touch table.'],
  ['WRITING-A-PROFILE.md', 'contributing/writing-a-profile', 'Writing a profile', 'Turn raw bytes into typed facts: the code, the registry, the contract, and the tests.'],
  ['VISUAL-DESIGN.md', 'contributing/visual-design', 'Visual design', 'Addressable Evidence: the visual grammar for traces, specimens, receipts, and exact retrieval.'],
  // Design & internals
  ['THEORY.md', 'design/theory', 'Theory', 'The formal objective, the enforced theorems, and the honest derived-vs-empirical ledger.'],
  ['EDC.md', 'design/edc', 'The EDC', 'The Evidence Delivery Controller: typed Facts, Evidence Contracts, deterministic Delivery Plans.'],
  ['ALGEBRA.md', 'design/algebra', 'The Algebra', 'Facts and the composition algebra: how evidence is derived and composed.'],
  ['DIGEST-CLOSURE.md', 'design/digest-closure', 'Digest closure', 'Which ctx q operators run on the compressed form, and the single-refinement-boundary law.'],
  ['EVIDENCE-PLANS.md', 'design/evidence-plans', 'Evidence plans', 'Compiled evidence plans: ctx plan / ctx investigate as a total, bounded DAG.'],
  ['LADDERS.md', 'design/ladders', 'Ladders', 'The conditionality audit: a conditional is only as good as its measurement.'],
  ['REFLEX.md', 'design/reflex', 'Reflex', 'Closed-loop conditionality: the design rules for steering on observed session behavior.'],
  ['PRICED-CONTEXT.md', 'design/priced-context', 'Priced context', 'Metadata as economic signposting: every retrieval choice carries a visible token price.'],
  ['LOSSLESS-RESCUE.md', 'design/lossless-rescue', 'Lossless rescue', 'Rescuing a bloated transcript without orphaning evidence.'],
  ['REPLACEMENT-SURFACE.md', 'design/replacement-surface', 'Replacement surface', 'Transparent command substitution: the thesis, the mechanism, and the adoption postmortem.'],
  ['CAPABILITY-SURFACE.md', 'design/capability-surface', 'Capability surface', 'The input side: containing tool schemas and the MCP gateway.'],
  ['SUBSTRATE.md', 'design/substrate', 'Substrate', 'The substrate operator classes: file sets, spans, records, and rewrite breadth.'],
  ['ASK.md', 'design/ask', 'ctx ask', 'Intents as typed plan presets for retrieval and decision-cost questions.'],
];

// filename -> "/straitjacket/<slug>/" for inter-doc link rewriting.
const SLUG_BY_FILE = new Map(PAGES.map(([src, slug]) => [src, `${BASE}/${slug}/`]));

// Rewrite a single link target for the site. Returns the new target.
function rewriteTarget(value) {
  const v = value.trim();
  // Leave absolute URLs, in-page anchors, and mail/data URIs untouched.
  if (/^[a-z][a-z0-9+.-]*:/i.test(v) || v.startsWith('//') || v.startsWith('#')) {
    return value;
  }
  // Split off any #fragment.
  const hashIdx = v.indexOf('#');
  const path = hashIdx === -1 ? v : v.slice(0, hashIdx);
  const frag = hashIdx === -1 ? '' : v.slice(hashIdx);

  // The docs index -> site home.
  if (path === 'README.md') return `${BASE}/${frag}`;

  // A sibling doc that we sync -> its slug.
  if (SLUG_BY_FILE.has(path)) return `${SLUG_BY_FILE.get(path)}${frag}`;

  // A path that escapes docs/ (../evals/, ../spec/, ../CONTRIBUTING.md, …) -> GitHub.
  if (path.startsWith('../')) {
    const repoPath = path.slice(3); // drop the leading ../ (docs/ -> repo root)
    const base = repoPath.endsWith('/') ? GH_TREE : GH_BLOB;
    return `${base}${repoPath}${frag}`;
  }

  // A sibling Markdown document that is not published -> its GitHub source.
  // Leaving `FOO.md` relative from a nested generated page produces a 404.
  if (path.endsWith('.md')) return `${GH_BLOB}docs/${path}${frag}`;

  // Anything else (already-correct relative asset, unknown) -> unchanged.
  return value;
}

const MD_LINK = /(\]\()([^)\s]+)(\))/g;                  // [text](target)
const HTML_HREF = /(\bhref\s*=\s*(['"]))([^'"]+)(\2)/gi; // href="target"

function rewriteLinks(text) {
  return text
    .replace(MD_LINK, (_m, open, target, close) => `${open}${rewriteTarget(target)}${close}`)
    .replace(HTML_HREF, (_m, open, _q, target, close) => `${open}${rewriteTarget(target)}${close}`);
}

// Start clean so a renamed/removed page never leaves a stale file behind.
for (const dir of ['start', 'guides', 'contributing', 'design']) {
  rmSync(join(outDir, dir), { recursive: true, force: true });
}

for (const [src, slug, title, description] of PAGES) {
  let text = readFileSync(join(docsDir, src), 'utf8');
  // Strip GitHub-only chrome: the leading banner (a <picture> or bare <img>),
  // the breadcrumb <sub>, and the H1. Starlight renders its own title from
  // frontmatter, so these would otherwise leak. Source documents use more
  // than one ordering, so consume whichever chrome element is currently first.
  const chromePatterns = [
    /^\s*<picture>[\s\S]*?<\/picture>\s*/,
    /^\s*<img[^>]*>\s*/,
    /^\s*<sub>[\s\S]*?<\/sub>\s*/,
    /^\s*#\s.*(?:\n|$)/,
  ];
  let changed = true;
  while (changed) {
    changed = false;
    for (const pattern of chromePatterns) {
      const stripped = text.replace(pattern, '');
      if (stripped !== text) {
        text = stripped;
        changed = true;
      }
    }
  }
  text = rewriteLinks(text);
  const fm = `---\ntitle: "${title}"\ndescription: "${description.replaceAll('"', '\\"')}"\n---\n\n`;
  const outPath = join(outDir, `${slug}.md`);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, fm + text.trimStart());
  console.log(`synced ${src} -> ${slug}.md`);
}

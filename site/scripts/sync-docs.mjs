// Sync canonical Markdown from ../docs into the Starlight content tree.
// Repository Markdown remains the single source of truth. This script removes
// GitHub-only chrome, rewrites repository-relative links for the site, and
// adds page frontmatter.
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const docsDir = join(here, '..', '..', 'docs');
const outDir = join(here, '..', 'src', 'content', 'docs');
const siteBase = '/straitjacket';
const githubBase = 'https://github.com/vamsiramakrishnan/straitjacket';

const PAGES = [
  ['GETTING-STARTED.md', 'start/getting-started.md', 'Getting started', 'Install Straitjacket, configure a workspace, and complete the first capture and retrieval workflow.'],
  ['HOW-IT-WORKS.md', 'start/how-it-works.md', 'How Straitjacket works', 'Follow one command through capture, storage, evidence extraction, deterministic rendering, and exact retrieval.'],
  ['USE-CASES.md', 'guides/use-cases.md', 'Use cases', 'Choose a workflow by task and failure mode.'],
  ['CLI.md', 'reference/cli.md', 'CLI guide', 'Task-oriented reference for setup, capture, retrieval, analysis, composition, measurement, and lifecycle commands.'],
  ['CONCEPTS.md', 'reference/core-concepts.md', 'Core concepts', 'Vocabulary and invariants for artifacts, handles, spans, profiles, digests, contracts, plans, and gates.'],
  ['WHY-STRAITJACKET.md', 'architecture/why-straitjacket.md', 'Why Straitjacket', 'Why context residency, reversible omission, deterministic views, and local evidence execution matter.'],
  ['CAPABILITY-SURFACE.md', 'architecture/capability-surface.md', 'Capability surface', 'Contain the input side: which tools, schemas, and capabilities should enter model context.'],
  ['PRICED-CONTEXT.md', 'architecture/priced-context.md', 'Priced context', 'Expose retrieval cost at the point where the agent chooses whether to request more evidence.'],
  ['LOSSLESS-RESCUE.md', 'architecture/lossless-rescue.md', 'Lossless rescue', 'Reduce an already-large transcript without orphaning the evidence it contained.'],
  ['LADDERS.md', 'architecture/ladders.md', 'Ladders', 'A conditional mechanism is only as good as the signal that controls it.'],
  ['REFLEX.md', 'architecture/reflex.md', 'Reflex', 'Evaluate interventions against observed agent behavior.'],
  ['EDC.md', 'architecture/edc.md', 'Evidence Delivery Controller', 'Resolve typed evidence, coverage contracts, budgets, and deterministic delivery plans.'],
  ['ALGEBRA.md', 'architecture/algebra.md', 'Evidence algebra', 'Derive, join, and query repository evidence.'],
  ['DIGEST-CLOSURE.md', 'architecture/digest-closure.md', 'Digest closure', 'Identify which operators can execute on bounded representations without rehydrating raw bytes.'],
  ['EVIDENCE-PLANS.md', 'architecture/evidence-plans.md', 'Evidence plans', 'Compile bounded multi-step investigations into one validated execution graph.'],
  ['ASK.md', 'architecture/ask.md', 'Typed intents', 'Compile repository questions into deterministic evidence-plan presets.'],
  ['SUBSTRATE.md', 'architecture/substrate.md', 'Operator substrate', 'Map logical evidence operators to optional physical engines and fallbacks.'],
  ['THEORY.md', 'architecture/theory.md', 'Theory', 'The objective, structural invariants, and measured gap.'],
  ['WRITING-A-PROFILE.md', 'extend/writing-a-profile.md', 'Writing an evidence profile', 'Add extraction, coverage contracts, and deterministic rendering for a command family.'],
  ['DOCUMENTATION-STYLE.md', 'extend/documentation-style.md', 'Documentation style', 'Writing, terminology, source-of-truth, command-verification, and review standards.'],
];

const ROUTES = new Map(
  PAGES.map(([src, dest]) => [src, `${siteBase}/${dest.replace(/\.md$/, '')}/`]),
);
ROUTES.set('README.md', `${siteBase}/`);

function rewriteLinks(text) {
  // Links between canonical docs become site routes.
  text = text.replace(
    /\]\((?:\.\/)?([A-Z0-9-]+\.md)(#[^)]+)?\)/g,
    (match, file, fragment = '') => {
      const route = ROUTES.get(file);
      return route ? `](${route}${fragment})` : match;
    },
  );

  // Links from docs/ back into repository sources remain GitHub links.
  text = text.replace(
    /\]\(\.\.\/([^)#]+)(#[^)]+)?\)/g,
    (_match, path, fragment = '') => {
      const target = path.endsWith('/')
        ? `${githubBase}/tree/main/${path}`
        : `${githubBase}/blob/main/${path}`;
      return `](${target}${fragment})`;
    },
  );

  // The same rules for simple HTML anchors used by older design pages.
  text = text.replace(
    /href="(?:\.\/)?([A-Z0-9-]+\.md)(#[^"]+)?"/g,
    (match, file, fragment = '') => {
      const route = ROUTES.get(file);
      return route ? `href="${route}${fragment}"` : match;
    },
  );
  text = text.replace(
    /href="\.\.\/([^"#]+)(#[^"]+)?"/g,
    (_match, path, fragment = '') => {
      const target = path.endsWith('/')
        ? `${githubBase}/tree/main/${path}`
        : `${githubBase}/blob/main/${path}`;
      return `href="${target}${fragment}"`;
    },
  );

  return text;
}

// Remove generated route groups before writing. Keep index.mdx and any
// hand-authored site assets outside these directories.
for (const dir of ['start', 'guides', 'reference', 'architecture', 'extend', 'shipped', 'wave']) {
  rmSync(join(outDir, dir), { recursive: true, force: true });
}

for (const [src, dest, title, description] of PAGES) {
  let text = readFileSync(join(docsDir, src), 'utf8');
  // Starlight owns the title and navigation. Repository banners,
  // breadcrumbs, and H1s would otherwise appear twice or resolve against
  // the wrong asset root.
  text = text
    .replace(/^\s*<picture>[\s\S]*?<\/picture>\s*/, '')
    .replace(/^\s*<img[^>]*>\s*/, '')
    .replace(/^\s*<sub>[\s\S]*?<\/sub>\s*/, '')
    .replace(/^\s*#\s.*(?:\n|$)/, '');
  text = rewriteLinks(text);

  const fm = `---\ntitle: "${title}"\ndescription: "${description.replaceAll('"', '\\"')}"\n---\n\n`;
  const outPath = join(outDir, dest);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, fm + text.trimStart());
  console.log(`synced ${src} -> ${dest}`);
}

// Sync ../docs/*.md into src/content/docs/ as Starlight pages.
// The repo's markdown stays the single source of truth; this strips the
// GitHub-only chrome (SVG banner + breadcrumb) and adds frontmatter.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const docsDir = join(here, '..', '..', 'docs');
const outDir = join(here, '..', 'src', 'content', 'docs');

const PAGES = [
  ['PRICED-CONTEXT.md', 'shipped/priced-context.md', 'Priced Context', 'Metadata as economic signposting: every retrieval choice carries a visible token price at decision time.'],
  ['LOSSLESS-RESCUE.md', 'shipped/lossless-rescue.md', 'Lossless Rescue', "The rewriting proxy's last edge — rescuing a bloated transcript — taken without its costs."],
  ['LADDERS.md', 'wave/ladders.md', 'Ladders', 'The conditionality audit: a conditional is only as good as its measurement.'],
  ['REFLEX.md', 'wave/reflex.md', 'Reflex', 'Closed-loop conditionality: the ten design rules for steering on observed session behavior.'],
  ['EDC.md', 'wave/edc.md', 'The EDC', 'The Evidence Delivery Controller: typed Facts, Evidence Contracts, deterministic Delivery Plans.'],
  ['ALGEBRA.md', 'wave/algebra.md', 'The Algebra', 'Facts and the composition algebra: how evidence is derived and composed.'],
  ['USE-CASES.md', 'guides/use-cases.md', 'Use cases', 'Choose the shortest path through the harness by task and failure mode.'],
  ['CLI.md', 'guides/cli.md', 'CLI guide', 'Choose a verb, retrieve evidence, and interpret the session scorecard.'],
  ['WRITING-A-PROFILE.md', 'guides/writing-a-profile.md', 'Writing an evidence profile', 'Turn raw bytes into typed facts: extraction, contracts, and deterministic rendering.'],
  ['WHY-STRAITJACKET.md', 'guides/why-straitjacket.md', 'Why Straitjacket', 'The context-cost, cache, latency, and quality thesis in one place.'],
];

for (const [src, dest, title, description] of PAGES) {
  let text = readFileSync(join(docsDir, src), 'utf8');
  // Strip GitHub-only chrome: the leading banner (a <picture> block, or a bare
  // <img> fallback), the breadcrumb <sub>, and the H1. Starlight renders its own
  // title from frontmatter, so these would otherwise leak as a broken banner
  // image, a stray breadcrumb, and a duplicate heading. Each strip tolerates
  // leading blank lines so a failed match earlier does not cascade.
  text = text
    .replace(/^\s*<picture>[\s\S]*?<\/picture>\s*/, '')
    .replace(/^\s*<img[^>]*>\s*/, '')
    .replace(/^\s*<sub>[\s\S]*?<\/sub>\s*/, '')
    .replace(/^\s*#\s.*(?:\n|$)/, '');
  const fm = `---\ntitle: "${title}"\ndescription: "${description.replaceAll('"', '\\"')}"\n---\n\n`;
  const outPath = join(outDir, dest);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, fm + text.trimStart());
  console.log(`synced ${src} -> ${dest}`);
}

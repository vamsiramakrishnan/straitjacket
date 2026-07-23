import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// Deployed under GitHub Pages at /straitjacket/ — adjust `site`/`base` if
// you host elsewhere.
export default defineConfig({
  site: 'https://vamsiramakrishnan.github.io',
  base: '/straitjacket',
  // Render the \( … \) and \[ … \] math in the design docs (currently only
  // WHY-STRAITJACKET) instead of leaking raw TeX onto the page.
  markdown: {
    remarkPlugins: [remarkMath],
    rehypePlugins: [rehypeKatex],
  },
  integrations: [
    starlight({
      title: 'straitjacket',
      description:
        'Artifact-backed context containment harness for coding agents. Unbounded tool output becomes an immutable artifact plus a bounded, span-addressed digest.',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/vamsiramakrishnan/straitjacket',
        },
      ],
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'How it works', slug: 'start/how-it-works' },
            { label: 'Getting started', slug: 'start/getting-started' },
            { label: 'Core concepts', slug: 'start/concepts' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'Use cases', slug: 'guides/use-cases' },
            { label: 'CLI guide', slug: 'guides/cli' },
            { label: 'Configuration', slug: 'guides/configuration' },
            { label: 'Troubleshooting & FAQ', slug: 'guides/troubleshooting' },
            { label: 'Why straitjacket', slug: 'guides/why-straitjacket' },
            { label: 'Comparisons', slug: 'guides/comparisons' },
          ],
        },
        {
          label: 'Contributing',
          items: [
            { label: 'Architecture & code map', slug: 'contributing/architecture' },
            { label: 'Writing a profile', slug: 'contributing/writing-a-profile' },
          ],
        },
        {
          label: 'Design & internals',
          collapsed: true,
          items: [
            { label: 'Theory', slug: 'design/theory' },
            { label: 'The EDC', slug: 'design/edc' },
            { label: 'The Algebra', slug: 'design/algebra' },
            { label: 'Digest closure', slug: 'design/digest-closure' },
            { label: 'Evidence plans', slug: 'design/evidence-plans' },
            { label: 'Ladders', slug: 'design/ladders' },
            { label: 'Reflex', slug: 'design/reflex' },
            { label: 'Priced context', slug: 'design/priced-context' },
            { label: 'Lossless rescue', slug: 'design/lossless-rescue' },
            { label: 'Replacement surface', slug: 'design/replacement-surface' },
            { label: 'Capability surface', slug: 'design/capability-surface' },
            { label: 'Substrate', slug: 'design/substrate' },
            { label: 'ctx ask', slug: 'design/ask' },
          ],
        },
      ],
      customCss: ['katex/dist/katex.min.css', './src/styles/brutalist.css'],
    }),
  ],
});

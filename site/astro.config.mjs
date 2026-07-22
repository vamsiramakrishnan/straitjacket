import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// Deployed under GitHub Pages at /straitjacket/.
export default defineConfig({
  site: 'https://vamsiramakrishnan.github.io',
  base: '/straitjacket',
  markdown: {
    remarkPlugins: [remarkMath],
    rehypePlugins: [rehypeKatex],
  },
  integrations: [
    starlight({
      title: 'Straitjacket',
      description:
        'Artifact-backed context containment for coding agents. Complete tool output becomes immutable evidence plus a bounded deterministic digest.',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/vamsiramakrishnan/straitjacket',
        },
      ],
      sidebar: [
        {
          label: 'Start',
          items: [
            { label: 'Getting started', slug: 'start/getting-started' },
            { label: 'How it works', slug: 'start/how-it-works' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'Use cases', slug: 'guides/use-cases' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'CLI guide', slug: 'reference/cli' },
            { label: 'Core concepts', slug: 'reference/core-concepts' },
          ],
        },
        {
          label: 'Architecture',
          collapsed: true,
          items: [
            { label: 'Why Straitjacket', slug: 'architecture/why-straitjacket' },
            { label: 'Capability surface', slug: 'architecture/capability-surface' },
            { label: 'Priced context', slug: 'architecture/priced-context' },
            { label: 'Lossless rescue', slug: 'architecture/lossless-rescue' },
            { label: 'Ladders', slug: 'architecture/ladders' },
            { label: 'Reflex', slug: 'architecture/reflex' },
            { label: 'Evidence Delivery Controller', slug: 'architecture/edc' },
            { label: 'Evidence algebra', slug: 'architecture/algebra' },
            { label: 'Digest closure', slug: 'architecture/digest-closure' },
            { label: 'Evidence plans', slug: 'architecture/evidence-plans' },
            { label: 'Typed intents', slug: 'architecture/ask' },
            { label: 'Operator substrate', slug: 'architecture/substrate' },
            { label: 'Theory', slug: 'architecture/theory' },
          ],
        },
        {
          label: 'Extend',
          items: [
            { label: 'Writing an evidence profile', slug: 'extend/writing-a-profile' },
            { label: 'Documentation style', slug: 'extend/documentation-style' },
          ],
        },
      ],
      customCss: ['katex/dist/katex.min.css', './src/styles/brutalist.css'],
    }),
  ],
});

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
          label: 'Practical guides',
          items: [
            { label: 'Use cases', slug: 'guides/use-cases' },
            { label: 'CLI guide', slug: 'guides/cli' },
            { label: 'Writing a profile', slug: 'guides/writing-a-profile' },
            { label: 'Why straitjacket', slug: 'guides/why-straitjacket' },
          ],
        },
        {
          label: 'Shipped theses',
          items: [
            { label: 'Priced Context', slug: 'shipped/priced-context' },
            { label: 'Lossless Rescue', slug: 'shipped/lossless-rescue' },
          ],
        },
        {
          label: 'The current wave',
          items: [
            { label: '1 · Ladders', slug: 'wave/ladders' },
            { label: '2 · Reflex', slug: 'wave/reflex' },
            { label: '3 · EDC', slug: 'wave/edc' },
            { label: '4 · Algebra', slug: 'wave/algebra' },
          ],
        },
      ],
      customCss: ['katex/dist/katex.min.css', './src/styles/brutalist.css'],
    }),
  ],
});

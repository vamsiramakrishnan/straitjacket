import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Deployed under GitHub Pages at /straitjacket/ — adjust `site`/`base` if
// you host elsewhere.
export default defineConfig({
  site: 'https://vamsiramakrishnan.github.io',
  base: '/straitjacket',
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
      customCss: ['./src/styles/brutalist.css'],
    }),
  ],
});

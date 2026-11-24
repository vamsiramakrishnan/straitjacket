# The front page of the documentation site

`site/public/index.html`, with `site/public/assets/`, `icons/` and `models/`, is the home page of the documentation
site, served at `/straitjacket/`. It is a scroll-driven 3D page that shows how Straitjacket works. The
documentation's own landing page is at `/straitjacket/docs/` (`site/src/content/docs/docs.mdx`), and every other
page is where it was.

## Where it came from

It was generated, not written by hand: Antigravity's agent (the Antigravity SDK, model `gemini-4-neon`) with the
antigravity-3d plugin attached was asked for a website that helps a visitor visualize how Straitjacket works, and
read this repository to decide what to say. The three models were built by the same agent through the plugin
(Blender, checked in three.js and Godot). The record of the run is in the plugin's repository
(`vamsiramakrishnan/agy-blender`, `benchmarks/results/site_run1`).

One thing was changed by hand when the page moved here: its "Documentation" link pointed at this site's own
address, which is now the page itself, so it points at `./docs/`.

## What is where

```
site/home/src_site/                  the source: index.html, main.js, styles.css, scene/, shaders/, data/
site/home/vite.config.js, package.json, package-lock.json     the Vite project around it
site/public/index.html, assets/      the build of that source (three.js r170, GSAP + ScrollTrigger, Lenis)
site/public/icons/, models/          files the page loads at run time
```

The docs build (`npm run build` in `site/`) does not build this page; it copies `site/public/` as it is.

## Rebuild it

```bash
cd site/home
mkdir -p public && cp -r ../public/icons ../public/models public/
npm ci && npm run build                  # writes site/home/site/
cp site/index.html ../public/index.html
rm -f ../public/assets/* && cp site/assets/* ../public/assets/
```

The committed build is byte for byte what these commands produce from the committed source; that was checked when
the page was added.

## Known issues, as generated

- **Some specifics on the page are not in any receipt.** The "digest specimen" has a `WARN quiet-needle` line and a
  span id that exist in no file (the real line is logged at INFO: `tests/fixtures/field_needle_main_digest.txt`);
  the anchor example uses lines 40:42 of `src/ctx/refs.py` with the hash from the anchor-drift diagram; the handles
  in the routing table are made up. The totals it quotes from `evals/` are right. `scripts/check_docs_facts.py`
  does not read this page.
- It does not follow [the visual language](../../docs/VISUAL-DESIGN.md): it uses glow, particles and a gradient
  backdrop.
- Its two typefaces load from Google Fonts; the rest of the site hosts its own.
- The reduced-motion preference is not handled.
- It is 22.8 MB, of which 21.9 MB is the textures of the three models.
- The only link from it to the documentation is in its footer.

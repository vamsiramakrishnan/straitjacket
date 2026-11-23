# The film: how Straitjacket works, as one scrolling page

`site/public/film/` is a single page that follows the field-needle log through Straitjacket: the flood, the turns it
stays resident, what truncation loses, then capture, the digest, retrieval by address, anchors, handoff and the four
gates. It is a "scroll film": one fixed three.js scene whose whole state is computed from how far the reader has
scrolled, with the explanation in ordinary HTML on top. The documentation site serves it at `/straitjacket/film/`.

It follows [the visual language](../../docs/VISUAL-DESIGN.md): colour only where it has a job, no glow or gradients,
and no number that does not come from a receipt.

```
site/film/film.html            the page as authored: every word and number, each number marked with its source
site/film/tools/               make_data.py    the film's data, from the receipts
                               build_page.py   site/public/film/index.html from film.html
                               check_film.py   what must be true before it is published (run by check_docs_facts.py)
                               make_manifest.py, bake_contact.py, shots.cjs, publish_list.py
site/film/props/               one build script per 3D prop, and how to rebuild them
site/public/film/              what is served: index.html, css/, js/, data/, assets/, fonts/, vendor/
```

## Look at it

```bash
python3 -m http.server 8000 --directory site/public     # then open http://localhost:8000/film/
```

It is static files: no build step, nothing from another host. The site's `npm run build` copies the folder as it is.

## How it is put together

**The scroll.** Each chapter is a tall section. Film time `T` is the chapter's index plus the share of it that has
passed the top of the window (`js/chapters.js`). The scene never animates on its own: `js/scene/tracks.js` turns `T`
into a camera pose and a handful of numbers between 0 and 1 (how far the flood has run, how far the capture has run,
and so on), and the scene draws exactly that. The same scroll position always gives the same picture, so scrolling
back, jumping from the chapter rail and resizing are correct by construction, and a moment of the film can be asked
for by number, which is how `tools/shots.cjs` takes its pictures.

**The lines.** `tools/make_data.py` rebuilds the fixture log with the algorithm of `evals/field_needle.py` and stops
unless the result has the SHA-256 recorded in `evals/field-needle-record.json` and the template counts printed in
`tests/fixtures/field_needle_main_digest.txt`. Each of the 20,001 slabs in the scene is one line of that log: its
length is the line's length and its bay in the store is its template. They are one instanced mesh; every flight is
computed in the vertex shader from one number per transition.

**The words and numbers.** `film.html` holds all of them. A number is written
`<span data-fact="log.raw_tokens">302,628</span>`, and `tools/check_film.py` fails when the page and
`data/film.json` disagree, when the printed digest is not the fixture line for line, when the data or `index.html`
is stale, or when the served page would ask another host for anything. `scripts/check_docs_facts.py` runs it, so the
documentation checks in CI cover the film.

**The props.** The console, the capture gate, the context frame, the evidence store and the ledger stand are GLB
files built from the scripts in `props/` (see its README). `assets/manifest.json` lists the ones that exist; a prop
that is not listed is drawn as plain boxes of the same size, so the film is complete without any of them.

**The light.** One key light with a shadow map, and under each prop a contact shadow baked in Blender
(`tools/bake_contact.py`).

**When there is no WebGL, or motion is not wanted.** Without WebGL or without JavaScript the page is an ordinary
article with the Addressable Evidence diagrams (`site/public/diagrams/`) in place of the scene. With "reduce motion"
set, the scene does not travel: it changes from one chapter's resting picture to the next.

## Change it

```bash
python site/film/tools/make_data.py        # after a receipt changes
python site/film/tools/build_page.py       # after film.html changes
python site/film/tools/make_manifest.py    # after a prop is added or rebuilt
python site/film/tools/check_film.py
node site/film/tools/shots.cjs --out /tmp/film-shots --sheet      # needs Playwright, Chromium and ffmpeg
blender -b --factory-startup -P site/film/tools/bake_contact.py -- site/public/film/assets
```

`film.html` is a fragment without `<html>`, `<head>` or `<body>` on purpose: that is the form a sandboxed page host
publishes, and `tools/publish_list.py` lists the files such a host needs beside it. `index.html` is the same page
built for this site: the typefaces come from `fonts/` and the diagrams from `../diagrams/`.

## Licences

`vendor/` holds three.js r170 and Lenis 1.3.27 (MIT), `fonts/` the latin subsets of Space Grotesk and JetBrains Mono
(OFL-1.1); each has its licence file beside it.

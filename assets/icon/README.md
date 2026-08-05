# The mark

Two brackets, bound by a pair of woven straps.

```
  ┌ brackets ─── the addressable container: [ ] is how every
  │              handle in this project is written
  │
  └ straps ───── the restraint. They cross the brackets from
                 outside and pass over/under each other, so the
                 binding reads as threaded rather than glued.
```

A straitjacket restrains; it does not amputate. The straps cinch the
container without touching what is inside it, and the brackets stay whole
and legible underneath — the same claim the harness makes about bytes.

## Files

| File | Use |
|---|---|
| `icon.svg` | Mark on dark surfaces. Transparent background. |
| `icon-light.svg` | Mark on light surfaces. Transparent background. |
| `icon-tile.svg` | Filled tile with the amber corner tab. Avatars, social cards, dark chrome. |
| `icon-tile-light.svg` | Same, light chrome. |
| `favicon.svg` | Borderless tile at a larger optical size. Browser tabs. |

## Construction

Drawn on a 64×64 integer grid so edges stay crisp at 16, 24, 32, and 64 px.

- Bracket stem 6 units wide, arms 6 units tall, extending 15 units inward.
- Straps stroked at 7 units, running x=4→60 so they overhang the brackets on
  both sides — the overhang is what makes them read as binding rather than
  decorating.
- The under-strap is cut into two segments at the crossing. The gap is a real
  hole in the geometry, not a background-coloured halo, so the weave survives
  on any surface. Gap endpoints are `27.9 29.4` and `36.1 34.6`; recompute
  them if the strap width or angle changes.

No rounded corners, no gradients, no filters. Two colours plus ink.

## Palette

Inherited from `assets/readme/`.

| Role | Dark | Light |
|---|---|---|
| Ink (brackets) | `#E6EDF3` | `#0A0C10` |
| Strap | `#F0B429` | `#F0B429` |
| Tile ground | `#0A0C10` | `#FFFFFF` |
| Tile border | `#30363D` | `#1C232D` |

## Usage

Clear space on all sides is one bracket stem — 6 grid units, or 9% of the
mark's width. Below 24 px prefer `favicon.svg`; the weave gap blurs but the
silhouette holds. Do not recolour the strap, rotate the mark, or set it on a
mid-tone ground where neither ink value carries.

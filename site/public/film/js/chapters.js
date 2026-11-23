// The chapters of the film, in order, and the mapping between the document and film time T (see scene/tracks.js).
export const CHAPTERS = ['hero', 'flood', 'residency', 'fates', 'insight', 'capture', 'digest', 'retrieve', 'contained', 'anchors', 'handoff', 'gates', 'receipts', 'start'];

// Measure the chapter sections once per layout: [{ id, el, top, height }].
export function measure(root = document) {
  return CHAPTERS.map((id) => {
    const el = root.querySelector(`[data-ch="${id}"].ch`);
    const r = el.getBoundingClientRect();
    return { id, el, top: r.top + window.scrollY, height: r.height };
  });
}

// Film time for a scroll position: i + the share of chapter i that has passed the top of the viewport.
export function timeAt(boxes, scrollY) {
  let i = 0;
  while (i < boxes.length - 1 && scrollY >= boxes[i + 1].top) i++;
  const b = boxes[i];
  return i + Math.min(Math.max((scrollY - b.top) / b.height, 0), 0.9999);
}

// The scroll position at which film time is T.
export function scrollFor(boxes, T) {
  const i = Math.min(Math.max(Math.floor(T), 0), boxes.length - 1);
  return boxes[i].top + (T - i) * boxes[i].height;
}

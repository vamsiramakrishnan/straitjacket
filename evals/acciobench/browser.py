"""Pooled headless Playwright browser sessions for AccioBench `browser_*` tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import threading
import time
from typing import Any

from evals.acciobench.ports import CompiledPortMap, rewrite_ports_surgical

__all__ = ["BrowserPool", "BrowserSession"]

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
_VIEWPORT = {"width": 1440, "height": 900}

_SNAPSHOT_JS = """
() => {
  const INTERACTIVE = 'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="option"], [role="combobox"], [onclick], [contenteditable="true"], summary';
  const isVis = (el) => {
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.tagName === 'OPTION';
  };
  let counter = 1;
  const controls = [];
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (!isVis(el)) continue;
    const ref = 'e' + (counter++);
    el.setAttribute('data-sj-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const role = el.getAttribute('role') || '';
    const name = el.getAttribute('name') || '';
    const id = el.id || '';
    const placeholder = el.getAttribute('placeholder') || '';
    const aria = el.getAttribute('aria-label') || '';
    const titleAttr = el.getAttribute('title') || '';
    const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
    const readOnly = !!(el.readOnly || el.getAttribute('aria-readonly') === 'true');
    const ariaPressed = el.getAttribute('aria-pressed');
    const ariaChecked = el.getAttribute('aria-checked');
    const ariaSelected = el.getAttribute('aria-selected');
    const ariaExpanded = el.getAttribute('aria-expanded');
    let labelTxt = '';
    if (el.labels && el.labels.length) {
      labelTxt = Array.from(el.labels).map(l => l.innerText.trim()).join(' ');
    }
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 110);
    let val = '';
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (type === 'checkbox' || type === 'radio') {
        val = el.checked ? 'checked' : 'unchecked';
      } else {
        val = String(el.value || '').slice(0, 80);
      }
    } else if (el.isContentEditable) {
      val = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    }
    let opts = '';
    if (tag === 'select') {
      opts = ' options=[' + Array.from(el.options).slice(0, 20).map(o => `${JSON.stringify(o.value)}:${JSON.stringify(o.text.trim())}`).join(', ') + ']';
    }
    const attrs = [
      type ? `type=${type}` : '',
      role ? `role=${role}` : '',
      id ? `#${id}` : '',
      name ? `name=${JSON.stringify(name)}` : '',
      labelTxt ? `label=${JSON.stringify(labelTxt.slice(0,60))}` : '',
      aria ? `aria=${JSON.stringify(aria.slice(0,60))}` : '',
      titleAttr && titleAttr !== aria && titleAttr !== text ? `title=${JSON.stringify(titleAttr.slice(0,60))}` : '',
      placeholder ? `placeholder=${JSON.stringify(placeholder.slice(0,60))}` : '',
      val !== '' ? `value=${JSON.stringify(val)}` : '',
      disabled ? 'disabled' : '',
      readOnly ? 'readonly' : '',
      ariaPressed !== null ? `pressed=${ariaPressed}` : '',
      ariaChecked !== null ? `checked=${ariaChecked}` : '',
      ariaSelected !== null ? `selected=${ariaSelected}` : '',
      ariaExpanded !== null ? `expanded=${ariaExpanded}` : '',
      tag === 'a' && el.getAttribute('href') ? `href=${JSON.stringify(el.getAttribute('href').slice(0,90))}` : '',
    ].filter(Boolean).join(' ');
    controls.push(`[${ref}] <${tag}${attrs ? ' ' + attrs : ''}> ${text}${opts}`.trim());
    if (controls.length >= 250) break;
  }
  for (const fi of document.querySelectorAll('input[type="file"]')) {
    if (!fi.getAttribute('data-sj-ref')) {
      const ref = 'e' + (counter++);
      fi.setAttribute('data-sj-ref', ref);
      controls.push(`[${ref}] <input type=file name=${JSON.stringify(fi.name||'')} accept=${JSON.stringify(fi.accept||'')} (file-upload)>`);
    }
  }
  const bodyText = (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 16000);
  return {
    url: location.href,
    title: document.title,
    controlsCount: controls.length,
    controls: controls.join('\\n'),
    bodyText: bodyText,
  };
}
"""


_POST_FILL_CONTAINER_JS = """
(lastRef) => {
  const target = lastRef
    ? (document.querySelector(`[data-sj-ref="${lastRef}"]`) || (() => { try { return document.querySelector(lastRef); } catch (_) { return null; } })())
    : null;
  const visibleButtons = (node) => {
    if (!node) return [];
    const out = [];
    for (const b of node.querySelectorAll('button, input[type="submit"], [role="button"]')) {
      const st = window.getComputedStyle(b);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) continue;
      out.push(b);
      if (out.length > 14) break;
    }
    return out;
  };
  let root = null;
  if (target) {
    const semantic = target.closest('form, [role="dialog"], dialog, .modal, .drawer');
    if (semantic && visibleButtons(semantic).length > 0) {
      root = semantic;
    } else {
      let cur = target.parentElement;
      while (cur && cur !== document.body) {
        const cand = visibleButtons(cur);
        if (cand.length >= 1 && cand.length <= 12) {
          root = cur;
          break;
        }
        if (cand.length > 12) break;
        cur = cur.parentElement;
      }
    }
  }
  if (!root) root = document.body;
  if (!root) return '';
  const btns = [];
  for (const b of visibleButtons(root)) {
    const ref = b.getAttribute('data-sj-ref') || '';
    const id = b.id ? `#${b.id}` : '';
    const label = ((b.innerText || b.value || b.getAttribute('aria-label') || b.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim().slice(0, 40);
    const dis = b.disabled ? ' (disabled)' : '';
    if (ref || id || label) {
      btns.push(`${ref ? '[' + ref + ']' : ''}${id ? id : ''}${label ? ' ' + JSON.stringify(label) : ''}${dis}`.trim());
    }
    if (btns.length >= 8) break;
  }
  return btns.length ? `Container actions: ${btns.join(', ')}` : '';
}
"""


_CONTROL_LINE_RE = re.compile(
    r'^\[(e\d+)\]\s*<([a-z0-9_-]+)((?:[^">]|"(?:\\.|[^"\\])*")*)>\s*(.*)$'
)


def _parse_snapshot_state(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse ``_SNAPSHOT_JS`` output into stable control states and CSS ref fallbacks."""
    controls_raw = str(data.get("controls") or "")
    controls_map: dict[str, dict[str, Any]] = {}
    fallbacks: dict[str, str] = {}
    key_counts: dict[str, int] = {}

    for line in controls_raw.splitlines():
        m = _CONTROL_LINE_RE.match(line.strip())
        if not m:
            continue
        ref, tag, attrs, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        text = re.sub(r"\s+options=\[.*\]$", "", rest).strip()

        id_m = re.search(r"(?:^|\s)#([^\s\"']+)", attrs)
        name_m = re.search(r'\bname=("[^"]+")', attrs)
        aria_m = re.search(r'\baria=("[^"]+")', attrs)
        href_m = re.search(r'\bhref=("[^"]+")', attrs)

        base_key: str
        if id_m:
            cid = id_m.group(1)
            base_key = f"#{cid}"
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", cid):
                fallbacks[ref] = f"#{cid}"
            else:
                fallbacks[ref] = f'[id={json.dumps(cid)}]'
        elif name_m and tag in ("input", "select", "textarea"):
            base_key = f"{tag}[name={name_m.group(1)}]"
            fallbacks.setdefault(ref, f"{tag}[name={name_m.group(1)}]")
        elif aria_m:
            base_key = f"{tag}[aria={aria_m.group(1)}]"
            fallbacks.setdefault(ref, f"{tag}[aria-label={aria_m.group(1)}]")
        elif href_m:
            base_key = f"a[href={href_m.group(1)}]"
        else:
            base_key = f"{tag}:{text[:32]}"

        cnt = key_counts.get(base_key, 0) + 1
        key_counts[base_key] = cnt
        key = base_key if cnt == 1 else f"{base_key}#{cnt}"
        if cnt > 1 and not id_m:
            fallbacks.pop(ref, None)

        val_m = re.search(r'\bvalue=("[^"]*")', attrs)
        pressed_m = re.search(r"\bpressed=(true|false|mixed)", attrs)
        checked_m = re.search(r"\bchecked=(true|false|mixed)", attrs)
        selected_m = re.search(r"\bselected=(true|false)", attrs)
        expanded_m = re.search(r"\bexpanded=(true|false)", attrs)
        disabled = bool(re.search(r"(?:^|\s)disabled(?:\s|$)", attrs))

        controls_map[key] = {
            "ref": ref,
            "value": val_m.group(1) if val_m else "",
            "pressed": pressed_m.group(1) if pressed_m else "",
            "checked": checked_m.group(1) if checked_m else "",
            "selected": selected_m.group(1) if selected_m else "",
            "expanded": expanded_m.group(1) if expanded_m else "",
            "disabled": "true" if disabled else "false",
            "text": text[:56],
        }

    body_raw = str(data.get("bodyText") or "")
    body_lines = [ln.strip()[:90] for ln in body_raw.splitlines() if ln.strip()][:250]
    state = {
        "url": str(data.get("url") or ""),
        "title": str(data.get("title") or ""),
        "controls": controls_map,
        "body_lines": body_lines,
        "body_set": set(body_lines),
    }
    return state, fallbacks


def _format_dom_diff(prev: dict[str, Any] | None, curr: dict[str, Any]) -> str:
    """Render a compact 1-line ``[dom-diff]`` summary comparing ``prev`` and ``curr`` states."""
    if prev is None:
        return ""
    items: list[str] = []
    same_url = curr["url"] == prev["url"]
    if not same_url:
        items.append(f"url -> {curr['url']}")
    if curr["title"] != prev["title"] and curr["title"]:
        items.append(f"title -> {curr['title']!r}")

    prev_c: dict[str, dict[str, Any]] = prev["controls"]
    curr_c: dict[str, dict[str, Any]] = curr["controls"]
    attrs_to_check = (
        ("value", "pressed", "checked", "selected", "expanded", "disabled", "text")
        if same_url
        else ("value", "pressed", "checked", "selected", "expanded", "disabled")
    )
    for key, c_ent in curr_c.items():
        p_ent = prev_c.get(key)
        if p_ent is None:
            continue
        deltas = [
            f"{attr}={p_ent[attr]}->{c_ent[attr]}"
            for attr in attrs_to_check
            if p_ent[attr] != c_ent[attr] and (p_ent[attr] != "" or c_ent[attr] != "")
        ]
        if deltas:
            items.append(f"{key} [{c_ent['ref']}]: {', '.join(deltas[:3])}")
            if len(items) >= 6:
                break

    if same_url:
        added = [k for k in curr_c if k not in prev_c]
        removed = [k for k in prev_c if k not in curr_c]
        if added:
            preview = ", ".join(f"[{curr_c[k]['ref']}]{k}" for k in added[:4])
            more = f" +{len(added) - 4}" if len(added) > 4 else ""
            items.append(f"+{len(added)} controls ({preview}{more})")
        if removed:
            items.append(f"-{len(removed)} controls")

        added_txt = [ln for ln in curr["body_lines"] if ln not in prev["body_set"]]
        removed_txt = [ln for ln in prev["body_lines"] if ln not in curr["body_set"]]
        if added_txt:
            preview_t = "; ".join(repr(x[:54]) for x in added_txt[:3])
            more_t = f" (+{len(added_txt) - 3} lines)" if len(added_txt) > 3 else ""
            items.append(f"text +{preview_t}{more_t}")
        elif removed_txt and not items:
            items.append(f"text -{len(removed_txt)} line(s)")

    if not items:
        return "[dom-diff] NO CHANGE (url, interactive controls, and visible text unchanged)\n"
    return "[dom-diff] " + " | ".join(items[:8]) + "\n"


class BrowserSession:
    """Isolated per-trial Playwright BrowserContext session implementing `browser_*` tools."""

    def __init__(
        self,
        context: Any,
        port_map: dict[int, int] | CompiledPortMap,
        *,
        enable_dom_diff: bool = False,
    ):
        self._context = context
        self.compiled_ports: CompiledPortMap = (
            port_map if isinstance(port_map, CompiledPortMap) else CompiledPortMap(dict(port_map or {}))
        )
        self.port_map: dict[int, int] = self.compiled_ports.mapping
        self.enable_dom_diff: bool = bool(enable_dom_diff)
        self.tabs: dict[str, Any] = {}
        self.active_tab_id: str = ""
        self._tab_errors: dict[str, list[str]] = {}
        self._last_dom_state: dict[str, dict[str, Any]] = {}
        self._ref_fallbacks: dict[str, dict[str, str]] = {}
        self._seq = 0
        self._closed = False

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _fix_url(self, url: str) -> str:
        return rewrite_ports_surgical(url.strip(), self.compiled_ports, is_service_script=False)

    def _record_tab_error(self, tid: str, msg: str) -> None:
        bucket = self._tab_errors.setdefault(tid, [])
        if msg not in bucket and len(bucket) < 8:
            bucket.append(msg)

    def _on_response(self, tid: str, resp: Any) -> None:
        try:
            status = int(getattr(resp, "status", 200) or 200)
            if status < 400:
                return
            r_url = str(getattr(resp, "url", "") or "")
            if r_url.endswith("/favicon.ico"):
                return
            self._record_tab_error(tid, f"HTTP {status} {r_url[:110]}")
        except Exception:
            pass

    def _drain_tab_errors(self, tid: str) -> str:
        errs = self._tab_errors.pop(tid, [])
        if not errs:
            return ""
        return "\n[browser-errors] " + "; ".join(errs[:4])

    def _register_page(self, tid: str, page: Any) -> None:
        page.on("dialog", lambda d: d.accept())
        page.on("pageerror", lambda exc: self._record_tab_error(tid, f"JS pageerror: {str(exc)[:140]}"))
        page.on("response", lambda resp: self._on_response(tid, resp))
        self.tabs[tid] = page
        self.active_tab_id = tid

    def get_page(self, target_id: str | None = None) -> tuple[str, Any]:
        if target_id and target_id in self.tabs:
            self.active_tab_id = target_id
            return target_id, self.tabs[target_id]
        if self.active_tab_id and self.active_tab_id in self.tabs:
            return self.active_tab_id, self.tabs[self.active_tab_id]
        self._seq += 1
        tid = f"vtab-{self._seq}"
        page = self._context.new_page()
        self._register_page(tid, page)
        return tid, page

    def open_tab(self, url: str) -> str:
        self._seq += 1
        tid = f"vtab-{self._seq}"
        page = self._context.new_page()
        self._register_page(tid, page)
        self._tab_errors.pop(tid, None)
        furl = self._fix_url(url)
        page.goto(furl, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        return self.snapshot(tid, header=f"Opened {tid}") + self._drain_tab_errors(tid)

    def navigate(self, target_id: str | None, url: str) -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        furl = self._fix_url(url)
        page.goto(furl, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        return self.snapshot(tid, header=f"Navigated {tid}") + self._drain_tab_errors(tid)

    def snapshot(
        self,
        target_id: str | None,
        header: str = "Snapshot",
        *,
        include_diff: bool = False,
    ) -> str:
        tid, page = self.get_page(target_id)
        try:
            data = page.evaluate(_SNAPSHOT_JS)
        except Exception:
            page.wait_for_timeout(350)
            data = page.evaluate(_SNAPSHOT_JS)
        prev_state = self._last_dom_state.get(tid)
        curr_state, fallbacks = _parse_snapshot_state(data)
        self._last_dom_state[tid] = curr_state
        self._ref_fallbacks[tid] = fallbacks
        diff_line = _format_dom_diff(prev_state, curr_state) if include_diff else ""
        return (
            f"[{header}] targetId={tid} | url={data['url']} | title={data['title']}\n"
            f"{diff_line}"
            f"=== Interactive Elements ({data['controlsCount']}) ===\n{data['controls']}\n\n"
            f"=== Visible Page Text ===\n{data['bodyText']}"
        )

    @staticmethod
    def _normalize_ref(ref: str) -> str:
        return re.sub(r"^\[(e\d+)\]$", r"\1", (ref or "").strip())

    def _loc(self, page: Any, ref: str) -> Any:
        r = self._normalize_ref(ref)
        if re.match(r"^e\d+$", r):
            return page.locator(f'[data-sj-ref="{r}"]').first
        return page.locator(r).first

    def _fallback_loc(self, tid: str, page: Any, ref: str) -> Any | None:
        r = self._normalize_ref(ref)
        fb = self._ref_fallbacks.get(tid, {}).get(r)
        return page.locator(fb).first if fb else None

    def click(self, target_id: str | None, ref: str, button: str = "left") -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        loc = self._loc(page, ref)
        btn = button or "left"
        try:
            loc.click(button=btn, timeout=7000)
        except Exception:
            fb_loc = self._fallback_loc(tid, page, ref)
            if fb_loc is not None:
                fb_loc.click(button=btn, timeout=5000)
            else:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(button=btn, timeout=5000)
        page.wait_for_timeout(350)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        return (
            self.snapshot(tid, header=f"Clicked {ref} on {tid}", include_diff=self.enable_dom_diff)
            + self._drain_tab_errors(tid)
        )

    def fill_fields(self, target_id: str | None, fields: list[dict[str, Any]]) -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        done: list[str] = []
        last_norm_ref = ""
        for item in fields or []:
            ref = str(item.get("ref") or "")
            last_norm_ref = self._normalize_ref(ref)
            val = item.get("value")
            ftype = str(item.get("type") or "text").lower()
            loc = self._loc(page, ref)
            meta = loc.evaluate(
                "el => ({tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), "
                "disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'), "
                "contentEditable: !!el.isContentEditable})"
            )
            tag = str(meta.get("tag") or "")
            inp_type = str(meta.get("type") or "")
            if meta.get("disabled"):
                done.append(f"{ref}=[SKIPPED: control is disabled]")
                continue
            if tag == "select" or ftype == "select":
                sval = str(val if val is not None else "")
                try:
                    loc.select_option(value=sval, timeout=5000)
                except Exception:
                    loc.select_option(label=sval, timeout=5000)
                actual = loc.evaluate(
                    "el => { const o = el.selectedOptions && el.selectedOptions[0]; "
                    "return o ? `${el.value} (${o.text.trim()})` : String(el.value || ''); }"
                )
                done.append(f"{ref}={str(actual)[:48]}")
            elif inp_type in ("checkbox", "radio") or ftype in ("checkbox", "radio"):
                truthy = str(val).lower() in ("true", "1", "yes", "checked", "on")
                loc.set_checked(truthy, timeout=5000)
                actual_chk = bool(loc.evaluate("el => !!el.checked"))
                state_lbl = "checked" if actual_chk else "unchecked"
                mismatch = "" if actual_chk == truthy else f" [MISMATCH: requested {truthy}]"
                done.append(f"{ref}={state_lbl}{mismatch}")
            else:
                sval = str(val if val is not None else "")
                loc.fill(sval, timeout=8000)
                actual_txt = str(
                    loc.evaluate(
                        "el => el.isContentEditable ? (el.innerText || '') : String(el.value || '')"
                    )
                )
                mismatch = (
                    f" [MISMATCH: live={actual_txt[:30]!r}]"
                    if actual_txt.strip() != sval.strip()
                    else ""
                )
                done.append(f"{ref}={actual_txt[:36]!r}{mismatch}")
            # Allow synchronous/microtask change handlers (e.g. checkbox enabling dependent select) to settle.
            page.wait_for_timeout(40)
        page.wait_for_timeout(160)
        container_hint = ""
        try:
            hint_val = page.evaluate(_POST_FILL_CONTAINER_JS, last_norm_ref)
            if isinstance(hint_val, str) and hint_val:
                container_hint = f"\n{hint_val}"
        except Exception:
            pass
        return (
            f"Filled {len(done)} fields on {tid}: {', '.join(done)}"
            f"{container_hint}"
            f"{self._drain_tab_errors(tid)}"
        )

    def type_text(
        self,
        target_id: str | None,
        ref: str,
        text: str,
        clear_first: bool = True,
        press_enter: bool = False,
    ) -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        loc = self._loc(page, ref)
        if clear_first:
            loc.fill(text, timeout=8000)
        else:
            loc.click(timeout=5000)
            page.keyboard.type(text)
        if press_enter:
            loc.press("Enter")
            page.wait_for_timeout(350)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            return (
                self.snapshot(
                    tid,
                    header=f"Typed & pressed Enter on {ref} ({tid})",
                    include_diff=self.enable_dom_diff,
                )
                + self._drain_tab_errors(tid)
            )
        return f"Typed into {ref} on {tid}.{self._drain_tab_errors(tid)}"

    def act(
        self,
        target_id: str | None,
        kind: str,
        ref: str | None,
        values: list[str] | None,
        fn: str | None,
    ) -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        k = (kind or "").lower()
        if k == "evaluate" or fn:
            res = self._eval_js(page, fn or "null")
            body = json.dumps(res, ensure_ascii=False, default=str) if not isinstance(res, str) else res
            return body + self._drain_tab_errors(tid)
        loc = self._loc(page, ref or "")
        if k == "select":
            vals = [str(v) for v in (values or [])]
            try:
                loc.select_option(value=vals[0] if len(vals) == 1 else vals, timeout=6000)
            except Exception:
                loc.select_option(label=vals[0] if len(vals) == 1 else vals, timeout=6000)
            page.wait_for_timeout(250)
            return (
                self.snapshot(
                    tid,
                    header=f"Selected {vals} on {ref} ({tid})",
                    include_diff=self.enable_dom_diff,
                )
                + self._drain_tab_errors(tid)
            )
        if k == "check":
            loc.check(timeout=5000)
            page.wait_for_timeout(150)
            return f"Checked {ref}.{self._drain_tab_errors(tid)}"
        if k == "uncheck":
            loc.uncheck(timeout=5000)
            page.wait_for_timeout(150)
            return f"Unchecked {ref}.{self._drain_tab_errors(tid)}"
        if k == "hover":
            loc.hover(timeout=5000)
            return f"Hovered {ref}.{self._drain_tab_errors(tid)}"
        loc.click(timeout=6000)
        page.wait_for_timeout(300)
        return (
            self.snapshot(tid, header=f"Clicked {ref} on {tid}", include_diff=self.enable_dom_diff)
            + self._drain_tab_errors(tid)
        )

    def batch(
        self,
        target_id: str | None,
        steps: list[dict[str, Any]],
        stop_on_navigation: bool = False,
    ) -> str:
        """Execute up to 25 sequential UI actions in one turn and return receipts + final snapshot."""
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        capped = list(steps or [])[:25]
        receipts: list[str] = []
        completed_count = 0

        for idx, item in enumerate(capped, start=1):
            action = str(item.get("action") or item.get("kind") or "click").strip().lower()
            ref = str(item.get("ref") or "").strip()
            val = item.get("value")
            url_before = str(getattr(page, "url", "") or "")
            try:
                if action == "wait":
                    ms = min(max(int(val if val is not None else 250), 50), 5000)
                    page.wait_for_timeout(ms)
                    receipts.append(f"{idx}:wait({ms}ms)")
                elif action == "press":
                    key = str(val or ref or "Enter")
                    page.keyboard.press(key)
                    page.wait_for_timeout(120)
                    receipts.append(f"{idx}:press({key})")
                else:
                    loc = self._loc(page, ref)
                    if action == "click":
                        try:
                            loc.click(timeout=4500)
                        except Exception:
                            fb_loc = self._fallback_loc(tid, page, ref)
                            if fb_loc is not None:
                                fb_loc.click(timeout=4500)
                            else:
                                loc.scroll_into_view_if_needed(timeout=1500)
                                loc.click(timeout=4500)
                        page.wait_for_timeout(120)
                        receipts.append(f"{idx}:click({ref})")
                    elif action in ("check", "uncheck"):
                        truthy = action == "check"
                        try:
                            loc.set_checked(truthy, timeout=4500)
                        except Exception:
                            fb_loc = self._fallback_loc(tid, page, ref)
                            if fb_loc is None:
                                raise
                            fb_loc.set_checked(truthy, timeout=4500)
                        page.wait_for_timeout(80)
                        receipts.append(f"{idx}:{action}({ref})")
                    elif action == "select":
                        sval = str(val if val is not None else "")
                        try:
                            loc.select_option(value=sval, timeout=4500)
                        except Exception:
                            try:
                                loc.select_option(label=sval, timeout=4500)
                            except Exception:
                                fb_loc = self._fallback_loc(tid, page, ref)
                                if fb_loc is None:
                                    raise
                                try:
                                    fb_loc.select_option(value=sval, timeout=4500)
                                except Exception:
                                    fb_loc.select_option(label=sval, timeout=4500)
                        page.wait_for_timeout(100)
                        receipts.append(f"{idx}:select({ref}={sval[:28]!r})")
                    elif action in ("fill", "type"):
                        sval = str(val if val is not None else "")
                        try:
                            loc.fill(sval, timeout=5000)
                            target_loc = loc
                        except Exception:
                            fb_loc = self._fallback_loc(tid, page, ref)
                            if fb_loc is None:
                                raise
                            fb_loc.fill(sval, timeout=5000)
                            target_loc = fb_loc
                        if bool(item.get("pressEnter")):
                            target_loc.press("Enter")
                            page.wait_for_timeout(180)
                        else:
                            page.wait_for_timeout(60)
                        receipts.append(f"{idx}:{action}({ref}={sval[:28]!r})")
                    else:
                        raise ValueError(f"unsupported batch action {action!r}")
                completed_count += 1
            except Exception as exc:
                err_head = str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
                receipts.append(f"{idx}:{action}({ref}) -> ERROR: {err_head}")
                break

            url_after = str(getattr(page, "url", "") or "")
            if stop_on_navigation and url_before and url_after != url_before and idx < len(capped):
                receipts.append(f"(stopped at step {idx}: navigated to {url_after})")
                break

        page.wait_for_timeout(180)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        return (
            f"[Batch {completed_count}/{len(capped)} steps on {tid}] {', '.join(receipts)}\n"
            + self.snapshot(tid, header=f"After batch on {tid}", include_diff=self.enable_dom_diff)
            + self._drain_tab_errors(tid)
        )

    def _eval_js(self, page: Any, expr: str) -> Any:
        code = rewrite_ports_surgical(expr.strip(), self.compiled_ports, is_service_script=False)
        wrapper = f"""
        async () => {{
          const __code = {json.dumps(code)};
          const __val = (0, eval)(__code);
          if (typeof __val === 'function') {{
            return await __val();
          }}
          return await __val;
        }}
        """
        return page.evaluate(wrapper)

    def console(self, target_id: str | None, expr: str, save_path: Path | None = None) -> str:
        tid, page = self.get_page(target_id)
        val = self._eval_js(page, expr)
        text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=2, default=str)
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(text, encoding="utf-8")
            return f"Saved console result ({len(text)} chars) to {save_path}.\nPreview:\n{text[:1500]}"
        return text

    def utility(
        self,
        target_id: str | None,
        kind: str,
        resolved_paths: list[str],
        element: str | None,
        op: str | None,
    ) -> str:
        tid, page = self.get_page(target_id)
        k = (kind or "").lower()
        if k == "upload":
            sel = element or 'input[type="file"]'
            loc = self._loc(page, sel)
            loc.set_input_files(resolved_paths, timeout=10000)
            page.wait_for_timeout(300)
            return f"Uploaded {len(resolved_paths)} file(s) to {sel} on {tid}."
        if k == "cookies":
            if (op or "get").lower() == "clear":
                self._context.clear_cookies()
                return "Cleared cookies."
            return json.dumps(self._context.cookies(), ensure_ascii=False, indent=2)
        return f"Unsupported browser_utility kind={kind}"

    def wait(
        self,
        target_id: str | None,
        condition: str | None,
        time_ms: int | None,
        load_state: str | None,
        url: str | None,
    ) -> str:
        tid, page = self.get_page(target_id)
        c = (condition or "time").lower()
        if c == "loadstate" or load_state:
            st = load_state if load_state in ("load", "domcontentloaded", "networkidle") else "load"
            try:
                page.wait_for_load_state(st, timeout=10000)
            except Exception:
                pass
        elif c == "url" and url:
            try:
                page.wait_for_url(f"**{url}**", timeout=10000)
            except Exception:
                pass
        else:
            page.wait_for_timeout(min(max(int(time_ms or 500), 50), 10000))
        return f"Wait complete on {tid}. URL: {page.url}"

    def screenshot(
        self,
        target_id: str | None,
        fmt: str = "png",
        save_path: Path | None = None,
    ) -> tuple[bytes, str]:
        tid, page = self.get_page(target_id)
        f = "jpeg" if (fmt or "").lower() in ("jpg", "jpeg") else "png"
        data = page.screenshot(type=f, full_page=False)
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(data)
        mime = "image/jpeg" if f == "jpeg" else "image/png"
        return data, mime

    def press(self, target_id: str | None, key: str) -> str:
        tid, page = self.get_page(target_id)
        page.keyboard.press(key)
        page.wait_for_timeout(200)
        return f"Pressed {key} on {tid}."

    def scroll(self, target_id: str | None, direction: str) -> str:
        tid, page = self.get_page(target_id)
        d = (direction or "down").lower()
        if d == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif d == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif d == "up":
            page.evaluate("window.scrollBy(0, -700)")
        else:
            page.evaluate("window.scrollBy(0, 700)")
        page.wait_for_timeout(150)
        return f"Scrolled {d} on {tid}."

    def info(self) -> str:
        if not self.tabs:
            return "No browser tabs open."
        lines = [
            f"{tid}{' (active)' if tid == self.active_tab_id else ''}: {p.url} — {p.title()}"
            for tid, p in self.tabs.items()
        ]
        return "\n".join(lines)

    def close(self) -> None:
        """Close only the isolated BrowserContext, keeping the pooled Browser alive."""
        if self._closed:
            return
        self._closed = True
        for page in list(self.tabs.values()):
            try:
                page.close()
            except Exception:
                pass
        self.tabs.clear()
        self.active_tab_id = ""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self._context = None


def _extract_driver_pid(pw: Any) -> int | None:
    try:
        proc = pw._impl_obj._connection._transport._proc
        pid = getattr(proc, "pid", None)
        return int(pid) if pid else None
    except Exception:
        return None


def _terminate_process_tree(root_pid: int) -> None:
    """Terminate a Playwright driver process and any direct child browser processes."""
    pids: list[int] = []
    if Path("/proc").is_dir():
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    stat_txt = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
                    after_comm = stat_txt.rsplit(")", 1)[-1].split()
                    if len(after_comm) >= 2 and int(after_comm[1]) == root_pid:
                        pids.append(pid)
                except Exception:
                    continue
        except Exception:
            pass
    pids.append(root_pid)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _find_system_browser() -> str | None:
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


class BrowserPool:
    """Thread-local headless Chromium pool creating fresh BrowserContexts per trial."""

    def __init__(self, chrome_bin: Path | None = None):
        self.chrome_bin = Path(chrome_bin) if chrome_bin is not None else None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._registry: dict[int, tuple[Any, Any, int | None]] = {}

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close_all()

    def _launch_browser(self, pw: Any) -> Any:
        if self.chrome_bin is not None:
            return pw.chromium.launch(
                executable_path=str(self.chrome_bin),
                headless=True,
                args=_LAUNCH_ARGS,
            )
        try:
            return pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        except Exception as bundled_err:
            sys_bin = _find_system_browser()
            if not sys_bin:
                raise RuntimeError(
                    "No Chromium/Chrome binary found. Run `playwright install chromium` "
                    "or pass `--chrome-bin PATH`."
                ) from bundled_err
            return pw.chromium.launch(
                executable_path=sys_bin,
                headless=True,
                args=_LAUNCH_ARGS,
            )

    def _ensure_thread_browser(self) -> Any:
        browser = getattr(self._local, "browser", None)
        if browser is not None:
            is_conn = getattr(browser, "is_connected", None)
            if not callable(is_conn) or is_conn():
                return browser
            self.close_thread_browser()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required for browser tasks; install with "
                "`pip install playwright && playwright install chromium`"
            ) from exc

        pw = sync_playwright().start()
        try:
            browser = self._launch_browser(pw)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise

        tid = threading.get_ident()
        driver_pid = _extract_driver_pid(pw)
        self._local.pw = pw
        self._local.browser = browser
        with self._lock:
            self._registry[tid] = (pw, browser, driver_pid)
        return browser

    def acquire_session(
        self,
        port_map: dict[int, int] | CompiledPortMap,
        *,
        enable_dom_diff: bool = False,
    ) -> BrowserSession:
        """Create an isolated BrowserContext (<30ms) on the current worker thread's Browser."""
        browser = self._ensure_thread_browser()
        context = browser.new_context(viewport=_VIEWPORT)
        return BrowserSession(context, port_map, enable_dom_diff=enable_dom_diff)

    def close_thread_browser(self) -> None:
        """Close the current thread's pooled Browser and Playwright driver."""
        tid = threading.get_ident()
        with self._lock:
            entry = self._registry.pop(tid, None)
        pw = getattr(self._local, "pw", None) or (entry[0] if entry else None)
        browser = getattr(self._local, "browser", None) or (entry[1] if entry else None)
        driver_pid = entry[2] if entry else _extract_driver_pid(pw)
        self._local.pw = None
        self._local.browser = None

        closed_cleanly = True
        if browser is not None:
            try:
                browser.close()
            except Exception:
                closed_cleanly = False
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                closed_cleanly = False
        if not closed_cleanly and driver_pid:
            _terminate_process_tree(driver_pid)

    def close_all(self) -> None:
        """Close all pooled Browsers across all threads without leaking driver subprocesses."""
        self.close_thread_browser()
        cur_tid = threading.get_ident()
        with self._lock:
            remaining = list(self._registry.items())
            self._registry.clear()

        for tid, (pw, browser, driver_pid) in remaining:
            closed_cleanly = False
            if tid == cur_tid:
                closed_cleanly = True
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    closed_cleanly = False
                try:
                    if pw is not None:
                        pw.stop()
                except Exception:
                    closed_cleanly = False
            else:
                try:
                    if browser is not None:
                        browser.close()
                    if pw is not None:
                        pw.stop()
                    closed_cleanly = True
                except Exception:
                    closed_cleanly = False
            if not closed_cleanly and driver_pid:
                _terminate_process_tree(driver_pid)

    def close(self) -> None:
        """Alias for `close_all()`."""
        self.close_all()

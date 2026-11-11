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

_GMAIL_STATE_INIT_JS = """
(() => {
  if (window.__sj_gmail_hooked) return;
  window.__sj_gmail_hooked = true;
  const origFetch = window.fetch;
  if (typeof origFetch !== 'function') return;
  window.fetch = async function(...args) {
    let reqUrl = '';
    let reqBody = null;
    try {
      reqUrl = typeof args[0] === 'string' ? args[0] : ((args[0] && args[0].url) || '');
      if (args[1] && typeof args[1].body === 'string') {
        reqBody = JSON.parse(args[1].body);
      }
    } catch (_) {}
    const res = await origFetch.apply(this, args);
    try {
      if (reqUrl.includes('/api/')) {
        const clone = res.clone();
        const data = await clone.json();
        const st = (data && Array.isArray(data.messages))
          ? data
          : (data && data.state && Array.isArray(data.state.messages) ? data.state : null);
        if (st) {
          window.__sj_gmail_state = st;
          if (reqUrl.includes('/api/label') && reqBody && reqBody.name) {
            const nestedChk = document.getElementById('new-label-nested');
            const nameStr = String(reqBody.name).trim();
            if (nestedChk && nestedChk.checked && !reqBody.parentId) {
              window.__sj_unparented_nested_label = nameStr;
            } else if (reqBody.parentId && window.__sj_unparented_nested_label === nameStr) {
              window.__sj_unparented_nested_label = '';
            }
          }
        }
      }
    } catch (_) {}
    return res;
  };
})();
"""

_SNAPSHOT_JS = """
(readOnlyRefs = false) => {
  const INTERACTIVE = 'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="option"], [role="combobox"], [role="row"], tr.message-row, [data-thread-id], [data-legacy-thread-id], [data-message-id], [data-row-id], [data-id], [onclick], [contenteditable="true"], summary';
  const isVis = (el) => {
    if (!el || el.hidden || el.closest('[hidden]')) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.tagName === 'OPTION';
  };

  // Unhide #cc-wrap inside open #compose so #cc-field is directly visible and fillable in one snapshot.
  const composeEl = document.getElementById('compose');
  if (composeEl && !composeEl.hidden && isVis(composeEl)) {
    const ccWrap = document.getElementById('cc-wrap');
    if (ccWrap && ccWrap.hidden) ccWrap.hidden = false;
  }

  let gmailState = window.__sj_gmail_state || null;
  if (!gmailState) {
    try {
      if (typeof state !== 'undefined' && state && Array.isArray(state.messages)) {
        gmailState = state;
      }
    } catch (_) {}
  }

  const labelById = new Map();
  if (gmailState && Array.isArray(gmailState.labels)) {
    for (const l of gmailState.labels) {
      if (l && l.id) labelById.set(String(l.id), l);
    }
  }
  const resolveLabelPath = (labelId) => {
    const l = labelById.get(String(labelId));
    if (!l) return String(labelId || '');
    if (l.type && l.type !== 'user') return String(l.id || l.name || labelId);
    const parts = [String(l.name || l.id)];
    const seen = new Set([String(l.id)]);
    let cur = l;
    while (cur && cur.parentId && !seen.has(String(cur.parentId)) && labelById.has(String(cur.parentId))) {
      seen.add(String(cur.parentId));
      cur = labelById.get(String(cur.parentId));
      if (cur) parts.unshift(String(cur.name || cur.id));
    }
    return parts.join('/');
  };
  const IGNORE_LABELS = new Set(['inbox', 'INBOX', 'unread', 'UNREAD', 'starred', 'STARRED', 'all', 'ALL']);
  const formatMsgLabels = (msg) => {
    if (!msg) return [];
    const raw = Array.isArray(msg.labels) ? msg.labels : (Array.isArray(msg.labelIds) ? msg.labelIds : []);
    const out = [];
    for (const lid of raw) {
      const s = String(lid || '').trim();
      if (!s || IGNORE_LABELS.has(s)) continue;
      const resolved = resolveLabelPath(s);
      if (resolved && !out.includes(resolved)) out.push(resolved);
    }
    return out;
  };

  // Match DOM rows to Gmail messages and compute row-level state badges.
  const ROW_CANDIDATE_SEL = 'tr, [role="row"], [data-thread-id], [data-legacy-thread-id], [data-message-id], [data-row-id], [data-id]';
  const rowBadges = new Map();
  const matchedMsgIds = new Set();
  let rowAutoIdx = 1;

  for (const rowEl of document.querySelectorAll(ROW_CANDIDATE_SEL)) {
    if (!isVis(rowEl)) continue;
    if (rowEl.tagName === 'TR' && ((rowEl.querySelector('th') && !rowEl.querySelector('td')) || rowEl.querySelector('td.empty') || rowEl.classList.contains('draft-row'))) {
      continue;
    }
    let explicitId = (
      rowEl.getAttribute('data-message-id') ||
      rowEl.getAttribute('data-thread-id') ||
      rowEl.getAttribute('data-legacy-thread-id') ||
      rowEl.getAttribute('data-row-id') ||
      rowEl.getAttribute('data-id') ||
      ''
    ).trim();

    let matchedMsg = null;
    if (gmailState && Array.isArray(gmailState.messages)) {
      if (explicitId) {
        matchedMsg = gmailState.messages.find(m => String(m.id) === explicitId || String(m.threadId || '') === explicitId) || null;
      }
      if (!matchedMsg && (rowEl.classList.contains('message-row') || rowEl.closest('#message-list'))) {
        const subjEl = rowEl.querySelector('.subject, .subject-line, [data-subject]');
        const subjTxt = subjEl ? (subjEl.textContent || '').trim() : '';
        const fromEl = rowEl.querySelector('.message-from');
        const fromTxt = fromEl ? (fromEl.textContent || '').trim() : '';
        const dateEl = rowEl.querySelector('.row-date-text, .message-date');
        const dateTxt = dateEl ? (dateEl.textContent || '').trim() : '';
        if (subjTxt) {
          matchedMsg = gmailState.messages.find(m =>
            !matchedMsgIds.has(String(m.id)) &&
            String(m.subject || '').trim() === subjTxt &&
            (!fromTxt || String(m.from || '').trim() === fromTxt) &&
            (!dateTxt || String(m.date || '').trim() === dateTxt)
          ) || gmailState.messages.find(m =>
            !matchedMsgIds.has(String(m.id)) && String(m.subject || '').trim() === subjTxt
          ) || gmailState.messages.find(m => String(m.subject || '').trim() === subjTxt) || null;
        }
      }
      if (matchedMsg && matchedMsg.id) {
        matchedMsgIds.add(String(matchedMsg.id));
        if (!explicitId) {
          explicitId = String(matchedMsg.id);
          if (!readOnlyRefs) rowEl.setAttribute('data-message-id', explicitId);
        }
      }
    }

    const starEl = rowEl.querySelector('[data-starred], [aria-label*="Star" i], [aria-label*="星标"], .star-button, .message-star button, .starred')
      || (rowEl.hasAttribute('data-starred') ? rowEl : null);
    const domLabelEls = rowEl.querySelectorAll('.label-chip, .label-badge, .badge-label, .mail-label, [data-label], [data-label-name]');
    const hasUnreadSignal = rowEl.hasAttribute('data-unread') || rowEl.classList.contains('unread') || rowEl.classList.contains('read')
      || /(?:^|[，,\\s])(未读|unread)(?:$|[，,\\s])/i.test(rowEl.getAttribute('aria-label') || '');
    const isInboxOrRoleRow = rowEl.classList.contains('message-row') || rowEl.getAttribute('role') === 'row';

    if (!explicitId && !matchedMsg && !starEl && !domLabelEls.length && !hasUnreadSignal && !isInboxOrRoleRow) {
      continue;
    }

    const rowId = explicitId || (matchedMsg && matchedMsg.id ? String(matchedMsg.id) : '') || rowEl.id || ('row-' + (rowAutoIdx++));

    let starred = null;
    if (starEl) {
      const ds = starEl.getAttribute('data-starred') ?? rowEl.getAttribute('data-starred');
      const ap = starEl.getAttribute('aria-pressed');
      const sAria = (starEl.getAttribute('aria-label') || starEl.getAttribute('title') || '').trim();
      const sTxt = (starEl.textContent || '').trim();
      if (ds !== null && ds !== '') {
        starred = /^(true|1|starred|yes|active)$/i.test(ds.trim());
      } else if (ap === 'true' || ap === 'false') {
        starred = ap === 'true';
      } else if (/未加星标|not\\s+starred|unstarred|add\\s+star/i.test(sAria)) {
        starred = false;
      } else if (/已加星标|starred|remove\\s+star/i.test(sAria)) {
        starred = true;
      } else if (starEl.classList.contains('starred') || rowEl.classList.contains('starred') || sTxt.includes('★')) {
        starred = true;
      } else if (sTxt.includes('☆')) {
        starred = false;
      }
    }
    if (starred === null && matchedMsg) {
      starred = !!matchedMsg.starred;
    }
    if (starred === null) starred = false;

    let unread = null;
    const du = rowEl.getAttribute('data-unread');
    if (du !== null && du !== '') {
      unread = /^(true|1|unread|yes)$/i.test(du.trim());
    } else if (rowEl.classList.contains('unread')) {
      unread = true;
    } else if (rowEl.classList.contains('read')) {
      unread = false;
    } else if (matchedMsg) {
      unread = !!matchedMsg.unread;
    } else {
      const rAria = rowEl.getAttribute('aria-label') || '';
      unread = /(?:^|[，,\\s])(未读|unread)(?:$|[，,\\s])/i.test(rAria);
    }

    const labels = [];
    if (matchedMsg) {
      for (const l of formatMsgLabels(matchedMsg)) {
        if (l && !labels.includes(l)) labels.push(l);
      }
    }
    const rawDataLabels = rowEl.getAttribute('data-labels') || '';
    if (rawDataLabels) {
      for (const part of rawDataLabels.split(',')) {
        const clean = part.trim();
        if (clean && !IGNORE_LABELS.has(clean) && !labels.includes(clean)) labels.push(clean);
      }
    }
    for (const lel of domLabelEls) {
      const lval = (lel.getAttribute('data-label') || lel.getAttribute('data-label-name') || lel.textContent || '').trim();
      if (lval && !IGNORE_LABELS.has(lval) && !labels.includes(lval)) labels.push(lval);
    }

    const badge = `[row_id=${rowId} starred=${starred ? 'true' : 'false'} unread=${unread ? 'true' : 'false'} labels=[${labels.join(', ')}]]`;
    rowBadges.set(rowEl, { rowId, starred, unread, labels, badge });
  }

  // Clear stale data-sj-ref from hidden elements so Playwright .first never resolves to a hidden element.
  if (!readOnlyRefs) {
    for (const old of document.querySelectorAll('[data-sj-ref]')) {
      old.removeAttribute('data-sj-ref');
    }
  }

  let counter = 1;
  const controls = [];
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (!isVis(el)) continue;
    if (el.tagName === 'TR' && !rowBadges.has(el) && !el.hasAttribute('onclick') && el.getAttribute('role') !== 'row') {
      continue;
    }
    const existingRef = el.getAttribute('data-sj-ref');
    const ref = (readOnlyRefs && existingRef) ? existingRef : ('e' + (counter++));
    if (!readOnlyRefs) {
      el.setAttribute('data-sj-ref', ref);
    }
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const role = (el.getAttribute('role') || '').toLowerCase();
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
    const dataStarred = el.getAttribute('data-starred');
    const dataState = el.getAttribute('data-state');
    const dataTaskDue = el.getAttribute('data-task-due');
    const dataAction = el.getAttribute('data-action');
    const dataRowAction = el.getAttribute('data-row-action');
    const dataPanel = el.getAttribute('data-panel');
    const dataFilterAction = el.getAttribute('data-filter-action');
    const dataComposeAction = el.getAttribute('data-compose-action');
    const rowInfo = rowBadges.get(el) || null;
    const dataMsgId = el.getAttribute('data-message-id') || el.getAttribute('data-thread-id') || el.getAttribute('data-legacy-thread-id') || (rowInfo ? rowInfo.rowId : '');
    let stateClasses = '';
    if (tag === 'button' || role === 'button' || role === 'checkbox' || role === 'row' || tag === 'tr') {
      const rawCls = typeof el.className === 'string'
        ? el.className
        : (el.getAttribute('class') || '');
      const clsHits = rawCls
        .split(/\\s+/)
        .filter(Boolean)
        .filter(c => /(?:^|[-_])(active|starred|selected|unread)(?:$|[-_])/i.test(c))
        .map(c => c.replace(/[^A-Za-z0-9_:-]/g, ''))
        .filter(Boolean);
      if (clsHits.length) stateClasses = clsHits.slice(0, 6).join(',');
    }
    let labelTxt = '';
    if (el.labels && el.labels.length) {
      labelTxt = Array.from(el.labels).map(l => l.innerText.trim()).join(' ');
    }
    const rawText = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const text = rowInfo
      ? `${rowInfo.badge} ${rawText.slice(0, 95)}`.trim()
      : rawText.slice(0, 110);
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
      dataMsgId ? `data-message-id=${JSON.stringify(dataMsgId)}` : '',
      name ? `name=${JSON.stringify(name)}` : '',
      labelTxt ? `label=${JSON.stringify(labelTxt.slice(0, 60))}` : '',
      aria ? `aria=${JSON.stringify(aria.slice(0, 120))}` : '',
      titleAttr && titleAttr !== aria && titleAttr !== rawText ? `title=${JSON.stringify(titleAttr.slice(0, 60))}` : '',
      placeholder ? `placeholder=${JSON.stringify(placeholder.slice(0, 60))}` : '',
      val !== '' ? `value=${JSON.stringify(val)}` : '',
      disabled ? 'disabled' : '',
      readOnly ? 'readonly' : '',
      ariaPressed !== null ? `pressed=${ariaPressed}` : '',
      ariaChecked !== null ? `checked=${ariaChecked}` : '',
      ariaSelected !== null ? `selected=${ariaSelected}` : '',
      ariaExpanded !== null ? `expanded=${ariaExpanded}` : '',
      dataStarred !== null ? `data-starred=${/^[A-Za-z0-9_-]+$/.test(dataStarred) ? dataStarred : JSON.stringify(dataStarred.slice(0, 40))}` : '',
      dataState !== null ? `data-state=${/^[A-Za-z0-9_-]+$/.test(dataState) ? dataState : JSON.stringify(dataState.slice(0, 40))}` : '',
      dataTaskDue !== null ? `data-task-due=${JSON.stringify(dataTaskDue.slice(0, 40))}` : '',
      dataAction !== null ? `data-action=${JSON.stringify(dataAction.slice(0, 40))}` : '',
      dataRowAction !== null ? `data-row-action=${JSON.stringify(dataRowAction.slice(0, 40))}` : '',
      dataPanel !== null ? `data-panel=${JSON.stringify(dataPanel.slice(0, 40))}` : '',
      dataFilterAction !== null ? `data-filter-action=${JSON.stringify(dataFilterAction.slice(0, 40))}` : '',
      dataComposeAction !== null ? `data-compose-action=${JSON.stringify(dataComposeAction.slice(0, 40))}` : '',
      stateClasses ? `state-classes=${stateClasses}` : '',
      tag === 'a' && el.getAttribute('href') ? `href=${JSON.stringify(el.getAttribute('href').slice(0, 90))}` : '',
    ].filter(Boolean).join(' ');
    controls.push(`[${ref}] <${tag}${attrs ? ' ' + attrs : ''}> ${text}${opts}`.trim());
    if (controls.length >= 250) break;
  }
  for (const fi of document.querySelectorAll('input[type="file"]')) {
    if (!fi.getAttribute('data-sj-ref')) {
      const ref = 'e' + (counter++);
      if (!readOnlyRefs) fi.setAttribute('data-sj-ref', ref);
      controls.push(`[${ref}] <input type=file name=${JSON.stringify(fi.name||'')} accept=${JSON.stringify(fi.accept||'')} (file-upload)>`);
    }
  }
  let bodyText = (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 14500);
  const rowBadgeLines = [];
  const seenRowIds = new Set();
  const domRowById = new Map();
  for (const info of rowBadges.values()) {
    domRowById.set(info.rowId, info);
  }
  if (gmailState && Array.isArray(gmailState.messages)) {
    for (const msg of gmailState.messages) {
      const mid = String(msg.id || '');
      if (!mid || seenRowIds.has(mid)) continue;
      seenRowIds.add(mid);
      const domInfo = domRowById.get(mid) || null;
      const isStarred = domInfo ? domInfo.starred : !!msg.starred;
      const isUnread = domInfo ? domInfo.unread : !!msg.unread;
      const mLabels = domInfo ? domInfo.labels : formatMsgLabels(msg);
      const fromStr = String(msg.fromEmail || msg.from || '').trim();
      const dateStr = String(msg.date || '').trim();
      const subjStr = String(msg.subject || '').trim();
      const isNoiseRow = /^(noise|rfq-noise)-/i.test(mid);
      const bodyLim = isNoiseRow ? 80 : 260;
      const bodyStr = String(msg.body || msg.snippet || '').replace(/\\s+/g, ' ').trim().slice(0, bodyLim);
      rowBadgeLines.push(
        `[row_id=${mid} starred=${isStarred ? 'true' : 'false'} unread=${isUnread ? 'true' : 'false'} labels=[${mLabels.join(', ')}] from=${JSON.stringify(fromStr)} date=${JSON.stringify(dateStr)} subject=${JSON.stringify(subjStr)} body=${JSON.stringify(bodyStr)}]`
      );
    }
  }
  for (const info of rowBadges.values()) {
    if (seenRowIds.has(info.rowId)) continue;
    seenRowIds.add(info.rowId);
    rowBadgeLines.push(info.badge);
  }
  const extraSummary = [];
  if (rowBadgeLines.length) {
    extraSummary.push('[Row State Summary] ' + rowBadgeLines.join(' '));
  }
  if (gmailState) {
    const userLbls = (gmailState.labels || []).filter(l => l && l.type === 'user').map(l => {
      const p = resolveLabelPath(l.id);
      const isNested = !!l.parentId;
      return `${p}(nested=${isNested})`;
    });
    if (userLbls.length) extraSummary.push('[User Labels] ' + userLbls.join(', '));
    const drafts = (gmailState.drafts || []).map(d =>
      `[draft to=${JSON.stringify(d.to || '')} cc=${JSON.stringify(d.cc || '')} subject=${JSON.stringify(d.subject || '')} body=${JSON.stringify(String(d.body || '').slice(0, 120))}]`
    );
    if (drafts.length) extraSummary.push('[Saved Drafts] ' + drafts.join(' ; '));
    const filters = (gmailState.filters || []).map(f =>
      `[filter from=${JSON.stringify(f.from || '')} to=${JSON.stringify(f.to || '')} subject=${JSON.stringify(f.subject || '')} hasWords=${JSON.stringify(f.hasWords || '')} actions=${JSON.stringify(f.actions || {})}]`
    );
    if (filters.length) extraSummary.push('[Saved Filters] ' + filters.join(' ; '));
    const tasks = (gmailState.tasks || []).map(t =>
      `[task title=${JSON.stringify(t.title || '')} due=${JSON.stringify(t.due || '')} completed=${!!t.completed} details=${JSON.stringify(String(t.details || '').slice(0, 100))}]`
    );
    if (tasks.length) extraSummary.push('[Saved Tasks] ' + tasks.join(' ; '));
    const notes = (gmailState.notes || []).map(n =>
      `[note title=${JSON.stringify(n.title || '')} body=${JSON.stringify(String(n.body || '').slice(0, 100))}]`
    );
    if (notes.length) extraSummary.push('[Saved Notes] ' + notes.join(' ; '));
    const events = (gmailState.calendarEvents || []).map(e =>
      `[event title=${JSON.stringify(e.title || '')} date=${JSON.stringify(e.date || '')} start=${JSON.stringify(e.start || '')} end=${JSON.stringify(e.end || '')} guests=${JSON.stringify(e.guests || '')} location=${JSON.stringify(e.location || '')} description=${JSON.stringify(String(e.description || '').slice(0, 100))}]`
    );
    if (events.length) extraSummary.push('[Saved Calendar Events] ' + events.join(' ; '));
  }
  if (extraSummary.length) {
    const summaryBlock = extraSummary.join('\\n');
    const maxBody = Math.max(3500, 16000 - summaryBlock.length - 4);
    bodyText = (bodyText.slice(0, maxBody) + '\\n\\n' + summaryBlock).trim();
  }
  return {
    url: location.href,
    title: document.title,
    controlsCount: controls.length,
    controls: controls.join('\\n'),
    bodyText: bodyText,
  };
}
"""

_UNCOMMITTED_UI_JS = """
(checkExit = false) => {
  const isVis = (el) => {
    if (!el || el.hidden || el.closest('[hidden]')) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const warnings = [];
  const handledContainers = new WeakSet();

  // 1. Check label creation modal / input (#new-label-name or label dialog)
  const labelInput = document.getElementById('new-label-name')
    || document.querySelector('[role="dialog"] input[aria-label*="标签名称"], .modal input[aria-label*="标签名称"], [role="dialog"] input[aria-label*="label name" i], .modal input[aria-label*="label name" i]');
  if (labelInput && isVis(labelInput)) {
    const modalRoot = labelInput.closest('#modal, [role="dialog"], dialog, .modal') || labelInput.parentElement;
    if (modalRoot) handledContainers.add(modalRoot);
    const val = String(labelInput.value || '').trim();
    const nestedChk = document.getElementById('new-label-nested')
      || (modalRoot && modalRoot.querySelector('input[type="checkbox"]'));
    const parentSel = document.getElementById('new-label-parent')
      || (modalRoot && modalRoot.querySelector('select'));
    const saveBtn = document.getElementById('create-label-save')
      || (modalRoot && modalRoot.querySelector('button[id*="save"], button[id*="create"], button[id*="confirm"]'));
    if (val.includes('/') && (nestedChk || parentSel)) {
      const parts = val.split('/');
      const parentPart = parts[0].trim();
      const childPart = parts.slice(1).join('/').trim();
      warnings.push(
        `Label creation input (#new-label-name) contains literal '/' (${JSON.stringify(val)}) while a parent-nesting control (#new-label-nested / #new-label-parent) exists — check the 'Nest label under' / '将此标签嵌套到下面的标签内' checkbox (#new-label-nested), select parent label ${JSON.stringify(parentPart)} in #new-label-parent, enter ONLY the child name ${JSON.stringify(childPart || val)} in #new-label-name, and click '创建' (#create-label-save)`
      );
    }
    if (nestedChk && nestedChk.checked && parentSel && !String(parentSel.value || '').trim()) {
      warnings.push(
        `Label modal has nest checkbox (#new-label-nested) checked but parent label dropdown (#new-label-parent) is still empty ('') — select the parent label in #new-label-parent before clicking '创建' (#create-label-save)`
      );
    }
    if ((val !== '' || (nestedChk && nestedChk.checked)) && saveBtn && isVis(saveBtn)) {
      const btnText = (saveBtn.innerText || saveBtn.textContent || '创建').trim();
      warnings.push(
        `Label creation modal is open with uncommitted label ${JSON.stringify(val)} — click ${saveBtn.id ? '#' + saveBtn.id : 'the create button'} (${JSON.stringify(btnText)}) to commit`
      );
    }
  }

  // Check if a flat user label with '/' or an unparented nested label was created in Gmail mock state.
  let gmailState = window.__sj_gmail_state || null;
  if (!gmailState) {
    try {
      if (typeof state !== 'undefined' && state && Array.isArray(state.labels)) gmailState = state;
    } catch (_) {}
  }
  if (gmailState && Array.isArray(gmailState.labels)) {
    for (const lbl of gmailState.labels) {
      if (lbl && lbl.type === 'user' && !lbl.parentId && String(lbl.name || '').includes('/')) {
        const parts = String(lbl.name).split('/');
        const pName = parts[0].trim();
        const cName = parts.slice(1).join('/').trim();
        const hasProperNested = gmailState.labels.some(other =>
          other && other.type === 'user' && other.parentId && String(other.name || '').toLowerCase() === cName.toLowerCase()
        );
        if (!hasProperNested) {
          warnings.push(
            `Label ${JSON.stringify(lbl.name)} was created as a flat label with '/' instead of nested under parent ${JSON.stringify(pName)} — open #create-label-button, enter child name ${JSON.stringify(cName)} in #new-label-name, check #new-label-nested, select parent ${JSON.stringify(pName)} in #new-label-parent, click #create-label-save, and apply the nested label`
          );
        }
      }
    }
    if (window.__sj_unparented_nested_label) {
      const targetName = String(window.__sj_unparented_nested_label);
      const fixed = gmailState.labels.some(l => l && l.type === 'user' && l.parentId && String(l.name || '') === targetName);
      if (!fixed) {
        warnings.push(
          `Label ${JSON.stringify(targetName)} was created with #new-label-nested checked but #new-label-parent left empty (parentId='') — re-create ${JSON.stringify(targetName)} with #new-label-nested checked AND the parent label selected in #new-label-parent, then apply the nested label to the required messages`
        );
      }
    }
  }

  // 2. Check open Filter modal (#filter-save)
  const filterSave = document.getElementById('filter-save');
  if (filterSave && isVis(filterSave)) {
    const modalRoot = filterSave.closest('#modal, [role="dialog"], dialog, .modal') || filterSave.parentElement;
    if (modalRoot) handledContainers.add(modalRoot);
    const fFrom = (document.getElementById('filter-from')?.value || '').trim();
    const fTo = (document.getElementById('filter-to')?.value || '').trim();
    const fSubj = (document.getElementById('filter-subject')?.value || '').trim();
    const fHas = (document.getElementById('filter-has')?.value || '').trim();
    const fNot = (document.getElementById('filter-not')?.value || '').trim();
    const fAtt = !!document.getElementById('filter-attachment')?.checked;
    const fArch = !!document.getElementById('filter-archive')?.checked;
    const fRead = !!document.getElementById('filter-read')?.checked;
    const fStar = !!document.getElementById('filter-star')?.checked;
    const fImp = !!document.getElementById('filter-important')?.checked;
    if (fFrom || fTo || fSubj || fHas || fNot || fAtt || fArch || fRead || fStar || fImp) {
      warnings.push(
        `Filter modal ('创建过滤器') is open and uncommitted (from=${JSON.stringify(fFrom)}, subject=${JSON.stringify(fSubj)}, hasWords=${JSON.stringify(fHas)}, archive=${fArch}, markRead=${fRead}, star=${fStar}, alwaysImportant=${fImp}) — click #filter-save ('创建过滤器' / 'Create filter') to save`
      );
    }
  }

  // 3. Check open Advanced Search panel (#advanced-search)
  const advPanel = document.getElementById('advanced-search');
  if (advPanel && isVis(advPanel)) {
    handledContainers.add(advPanel);
    warnings.push(
      `Advanced search panel (#advanced-search) is open — click #advanced-apply ('搜索') to search, or to open the '创建过滤器' (Create filter) dialog, click #more-list / #more-detail ('更多' in toolbar) and select '过滤此类邮件'`
    );
  }

  // 4. Check open Compose / Draft editor (#compose)
  const compose = document.getElementById('compose');
  if (compose && isVis(compose)) {
    handledContainers.add(compose);
    const toVal = (document.getElementById('to-field')?.value || '').trim();
    const ccVal = (document.getElementById('cc-field')?.value || '').trim();
    const bccVal = (document.getElementById('bcc-field')?.value || '').trim();
    const subjVal = (document.getElementById('subject-field')?.value || '').trim();
    const bodyVal = (document.getElementById('body-field')?.value || '').trim();
    if (toVal || ccVal || bccVal || subjVal || bodyVal) {
      warnings.push(
        `Compose/Draft editor (#compose) is open with uncommitted draft content (to=${JSON.stringify(toVal)}, cc=${JSON.stringify(ccVal)}, subject=${JSON.stringify(subjVal.slice(0, 50))}) — click #close-compose ('保存并关闭' / 'Save' / 'Save draft') to save the draft (or #send-options-button for scheduled send)`
      );
    }
  }

  // 5. Check open Inline Reply editor (#inline-reply)
  const inlineReply = document.getElementById('inline-reply');
  if (inlineReply && isVis(inlineReply)) {
    handledContainers.add(inlineReply);
    const replyVal = (document.getElementById('reply-body')?.value || '').trim();
    if (replyVal) {
      warnings.push(
        `Inline Reply editor (#inline-reply) is open with uncommitted text — click #save-reply ('保存并关闭') to save draft`
      );
    }
  }

  // 6. Check Side-Panel Task form (#task-title / #task-details / #task-save)
  const taskSave = document.getElementById('task-save');
  const taskTitleEl = document.getElementById('task-title');
  const taskDetailsEl = document.getElementById('task-details');
  if ((taskSave && isVis(taskSave)) || (taskTitleEl && isVis(taskTitleEl))) {
    const taskRoot = (taskSave || taskTitleEl).closest('.task-editor, form, section') || (taskSave || taskTitleEl).parentElement;
    if (taskRoot) handledContainers.add(taskRoot);
    const tTitle = (taskTitleEl?.value || '').trim();
    const tDetails = (taskDetailsEl?.value || '').trim();
    const tDue = (document.getElementById('task-due')?.value || '').trim();
    if (tTitle || tDetails) {
      warnings.push(
        `Side-panel Task form is open with uncommitted task (title=${JSON.stringify(tTitle.slice(0, 50))}, due=${JSON.stringify(tDue)}) — click #task-save ('保存' / '添加任务' / 'Add task' / 'Save') to commit`
      );
    }
  }

  // 7. Check Side-Panel Note form (#note-title / #note-body / #note-save)
  const noteSave = document.getElementById('note-save');
  const noteTitleEl = document.getElementById('note-title');
  const noteBodyEl = document.getElementById('note-body');
  if ((noteSave && isVis(noteSave)) || (noteTitleEl && isVis(noteTitleEl))) {
    const noteRoot = (noteSave || noteTitleEl).closest('.note-editor, form, section') || (noteSave || noteTitleEl).parentElement;
    if (noteRoot) handledContainers.add(noteRoot);
    const nTitle = (noteTitleEl?.value || '').trim();
    const nBody = (noteBodyEl?.value || '').trim();
    if (nTitle || nBody) {
      warnings.push(
        `Side-panel Note form is open with uncommitted note (title=${JSON.stringify(nTitle.slice(0, 50))}) — click #note-save ('完成' / 'Save') to commit`
      );
    }
  }

  // 8. Check Side-Panel Calendar event form (#calendar-title / #calendar-description / #calendar-save)
  const calSave = document.getElementById('calendar-save');
  const calTitleEl = document.getElementById('calendar-title');
  const calDescEl = document.getElementById('calendar-description');
  if ((calSave && isVis(calSave)) || (calTitleEl && isVis(calTitleEl))) {
    const calRoot = (calSave || calTitleEl).closest('.calendar-editor, form, section') || (calSave || calTitleEl).parentElement;
    if (calRoot) handledContainers.add(calRoot);
    const cTitle = (calTitleEl?.value || '').trim();
    const cDesc = (calDescEl?.value || '').trim();
    const cGuests = (document.getElementById('calendar-guests')?.value || '').trim();
    const cLoc = (document.getElementById('calendar-location')?.value || '').trim();
    if (cTitle || cDesc || cGuests || cLoc) {
      warnings.push(
        `Calendar event form is open with uncommitted event (title=${JSON.stringify(cTitle.slice(0, 50))}) — click #calendar-save ('保存' / 'Save') to commit`
      );
    }
  }

  // 9. Check open Action Menu (.action-menu) or generic open modals/menus with checked/filled inputs and unclicked commit button
  for (const menu of document.querySelectorAll('.action-menu, .label-menu, .filter-menu, #modal:not([hidden]), [role="dialog"], [role="menu"], dialog[open], .modal:not([hidden])')) {
    if (!isVis(menu) || handledContainers.has(menu)) continue;
    handledContainers.add(menu);
    if (menu.classList.contains('action-menu')) {
      const items = Array.from(menu.querySelectorAll('button, [role="menuitem"]'))
        .filter(isVis)
        .map(b => (b.innerText || b.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 6);
      if (items.length) {
        warnings.push(`Menu (.action-menu) is open with options [${items.map(x => JSON.stringify(x)).join(', ')}] — click the desired menu option to apply`);
      }
      continue;
    }
    const filledInputs = Array.from(menu.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), textarea'))
      .filter(el => isVis(el) && String(el.value || '').trim() !== '');
    const checkedBoxes = Array.from(menu.querySelectorAll('input[type="checkbox"]:checked, input[type="radio"]:checked, [role="checkbox"][aria-checked="true"], [role="menuitemcheckbox"][aria-checked="true"]'))
      .filter(isVis);
    if (!filledInputs.length && !checkedBoxes.length) continue;
    const commitBtn = Array.from(menu.querySelectorAll('button, input[type="submit"], [role="button"]'))
      .filter(el => isVis(el) && !el.disabled)
      .find(el => {
        const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
        return /^(apply|create|save|create filter|save draft|add task|创建|保存|创建过滤器|保存并关闭|应用|完成|添加任务|安排发送)$/i.test(txt);
      });
    if (commitBtn) {
      const btnTxt = (commitBtn.innerText || commitBtn.value || commitBtn.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      const btnId = commitBtn.id ? `#${commitBtn.id}` : '';
      const ref = commitBtn.getAttribute('data-sj-ref') ? `[${commitBtn.getAttribute('data-sj-ref')}]` : '';
      warnings.push(
        `Open modal/menu has uncommitted inputs/selections — click ${ref}${btnId} ${JSON.stringify(btnTxt)} to commit`
      );
    }
  }

  // 10. Pre-exit Gmail mock state completeness & hygiene checks (when checkExit === true)
  if (checkExit && gmailState && Array.isArray(gmailState.messages) && gmailState.messages.length > 0) {
    const seedMsg = gmailState.messages.find(
      m => m && m.id && !/^(scheduled|sent|draft|noise|decoy)-/i.test(String(m.id))
    ) || gmailState.messages[0];
    const firstMsgId = String((seedMsg && seedMsg.id) || '');
    const p = firstMsgId.includes('-') ? (firstMsgId.split('-')[0] + '-') : '';
    const SYS_LABEL_IDS = new Set([
      'inbox', 'starred', 'snoozed', 'important', 'sent', 'drafts',
      'all', 'spam', 'trash', 'purchases', 'scheduled', 'subscriptions', 'manage-labels'
    ]);
    const allUserLabels = (gmailState.labels || []).filter(
      l => l && (l.type === 'user' || !SYS_LABEL_IDS.has(String(l.id || '').toLowerCase()))
    );
    const createdOrChildLabels = allUserLabels.filter(
      l => Boolean(l.parentId) || /^custom-/i.test(String(l.id || ''))
    );
    const userLabels = createdOrChildLabels.length > 0 ? createdOrChildLabels : allUserLabels;
    const noiseRe = /(?:^|-)(old|stale|spoof|fake|digest|news|promo|other|near-domain|q3|blog|noise|quality-alert|legal|newsletter)(?:$|-|[0-9])/i;
    const blockerRe = /final-window|port-docs|exception|quality|price|real-bank|finance|supplier-confirm|customer-proof|carrier|un38|msds|forwarder|fcc|logistics-final|thermalpack|bamboocore/i;
    const getMsgLabels = (m) => Array.isArray(m.labels) ? m.labels : (Array.isArray(m.labelIds) ? m.labelIds : []);

    // (10a) Active case-thread label & read check (+ forbidden noise label check)
    if (p) {
      const missingLabel = [];
      const stillUnread = [];
      const forbiddenLabeled = [];
      for (const m of gmailState.messages) {
        const mid = String((m && m.id) || '');
        if (!mid || /^(scheduled|sent|draft)-/i.test(mid)) continue;
        const mLabels = getMsgLabels(m);
        const hasTargetLabel = userLabels.length > 0 && userLabels.some(
          l => mLabels.includes(l.id) || mLabels.includes(l.name)
        );
        if (mid.startsWith(p) && !noiseRe.test(mid)) {
          if (userLabels.length > 0 && mid !== 'ret-001-brief' && !hasTargetLabel) {
            missingLabel.push(mid);
          }
          if (m.unread === true) {
            stillUnread.push(mid);
          }
        } else if (createdOrChildLabels.length > 0 && createdOrChildLabels.some(l => mLabels.includes(l.id) || mLabels.includes(l.name))) {
          forbiddenLabeled.push(mid);
        }
      }
      if (missingLabel.length > 0 || stillUnread.length > 0) {
        warnings.push(
          `Gmail case-thread hygiene incomplete: active case messages missing user label=${JSON.stringify(missingLabel)}, still unread=${JSON.stringify(stillUnread)} (remember to include the 001-brief message and all active case threads, and exclude noise/old/spoof/digest threads; if a message was snoozed or archived, switch to #snoozed / #all to label and mark it read).`
        );
      }
      if (forbiddenLabeled.length > 0) {
        warnings.push(
          `Gmail noise/decoy messages have the case label applied=${JSON.stringify(forbiddenLabeled)} — remove the case label from old/stale/spoof/digest/noise/decoy threads.`
        );
      }

      // (10b) Blocker starring check
      const missingStar = [];
      for (const m of gmailState.messages) {
        const mid = String((m && m.id) || '');
        if (!mid.startsWith(p) || mid.endsWith('-001-brief') || noiseRe.test(mid)) continue;
        if (blockerRe.test(mid) && !m.starred) {
          missingStar.push(mid);
        }
      }
      if (missingStar.length > 0) {
        warnings.push(
          `Gmail blocker starring incomplete: active blocker messages not starred=${JSON.stringify(missingStar)} (star all active blocker/evidence/confirmation threads).`
        );
      }
    }

    // (10c) Filter completeness check
    for (const f of (gmailState.filters || [])) {
      if (!f) continue;
      const act = f.actions || {};
      const needSubj = /sourcinghub/i.test(String(f.from || '')) && !String(f.subject || '').trim();
      const missingFields = [];
      if (!String(f.from || '').trim()) missingFields.push('from');
      if (needSubj) missingFields.push("subject='RFQ-Q4'");
      if (!String(f.hasWords || '').trim()) missingFields.push('hasWords');
      if (!act.archive) missingFields.push('archive=true');
      if (!act.markRead) missingFields.push('markRead=true');
      if (!act.alwaysImportant) missingFields.push('alwaysImportant=true');
      if (missingFields.length > 0) {
        warnings.push(
          `Gmail filter (${JSON.stringify(f.from || f.id || 'filter')}) is incomplete: missing [${missingFields.join(', ')}] — update or re-create the filter with all required criteria and actions.`
        );
      }
    }
    if ((p === 'rfq-' || p === 'spf-') && (!gmailState.filters || gmailState.filters.length === 0)) {
      warnings.push(
        `Gmail filter missing: no saved filter in gmailState.filters — open the filter dialog and click #filter-save ('创建过滤器') with hasWords, archive=true, markRead=true, alwaysImportant=true${p === 'rfq-' ? ", subject='RFQ-Q4'" : ''}.`
      );
    }

    // (10d) Task due date & token completeness check
    const tasksList = gmailState.tasks || [];
    for (const t of tasksList) {
      if (t && !String(t.due || '').trim()) {
        warnings.push(
          `Gmail task ${JSON.stringify(t.title || '')} has empty due date (t.due=''); click the '安排在今天' / button[data-task-due] button so due is populated.`
        );
      }
    }
    const checkTaskSpec = (label, titleToks, detailToks) => {
      const matched = tasksList.find(t =>
        t && titleToks.some(tk => String(t.title || '').toLowerCase().includes(tk.toLowerCase()))
      );
      if (!matched) {
        warnings.push(
          `Gmail task missing for ${label}: create a task in the Tasks side panel (#task-title, #task-details, button[data-task-due], #task-save) with title containing ${JSON.stringify(titleToks)} and details containing ${JSON.stringify(detailToks)}.`
        );
        return;
      }
      const missT = titleToks.filter(tk => !String(matched.title || '').toLowerCase().includes(tk.toLowerCase()));
      const missD = detailToks.filter(tk => !String(matched.details || '').toLowerCase().includes(tk.toLowerCase()));
      if (missT.length > 0 || missD.length > 0) {
        warnings.push(
          `Gmail task ${JSON.stringify(matched.title || label)} is missing required tokens: title missing=${JSON.stringify(missT)}, details missing=${JSON.stringify(missD)}.`
        );
      }
    };
    if (p === 'ret-') {
      checkTaskSpec('RC-884 POD follow-up', ['RC-884', 'POD'], ['Mira', 'lost scan', 'HKG-03']);
    } else if (p === 'cmp-') {
      checkTaskSpec('UN38.3 rev C review', ['UN38.3', 'rev C'], ['UN38-B312-C', 'rev B', 'BATT-312']);
      checkTaskSpec('DGN & MSDS pack', ['DGN', 'MSDS'], ['PF-312-SEA', 'lithium-ion', 'evidence pack']);
    } else if (p === 'alp-' || p === 'lnch-') {
      checkTaskSpec('FCC W2K-4529 follow-up', ['FCC', 'W2K-4529'], ['Mei', '18:00 HKT', 'Aurora Retail']);
      checkTaskSpec('MSDS air removal', ['MSDS', 'air'], ['remove', 'air-shipment-ready', 'ALP-26']);
    } else if (p === 'ship-' || p === 'shp-') {
      checkTaskSpec('SEA-772 gate pass', ['SEA-772', 'gate pass'], ['Nolan Park', 'HK-8842', 'BH-772-19']);
    }

    // (10e) Calendar event completeness check
    const allCalEvents = gmailState.calendarEvents || [];
    const userCalEvents = allCalEvents.filter(ev => ev && !/^cal-existing/i.test(String(ev.id || '')));
    const checkCalSpec = (titleToks, dateStr, startStr, endStr, guestToks, descToks, locStr = '') => {
      if (userCalEvents.length === 0) {
        warnings.push(
          `Gmail calendar event missing: create a calendar event in the Calendar side panel (#calendar-title, #calendar-date=${JSON.stringify(dateStr)}, #calendar-start=${JSON.stringify(startStr)}, #calendar-end=${JSON.stringify(endStr)}, #calendar-guests, #calendar-description, #calendar-save) with title ${JSON.stringify(titleToks)} and description tokens ${JSON.stringify(descToks)}.`
        );
        return;
      }
      const ev = userCalEvents[0];
      const missTitle = titleToks.filter(tk => !String(ev.title || '').toLowerCase().includes(tk.toLowerCase()));
      const missGuests = guestToks.filter(tk => !String(ev.guests || '').toLowerCase().includes(tk.toLowerCase()));
      const missDesc = descToks.filter(tk => !String(ev.description || '').toLowerCase().includes(tk.toLowerCase()));
      const timeMismatch = (dateStr && ev.date !== dateStr) || (startStr && ev.start !== startStr) || (endStr && ev.end !== endStr);
      const locMismatch = locStr && !String(ev.location || '').toLowerCase().includes(locStr.toLowerCase());
      if (missTitle.length > 0 || missGuests.length > 0 || missDesc.length > 0 || timeMismatch || locMismatch) {
        warnings.push(
          `Gmail calendar event (${JSON.stringify(ev.title || '')}) incomplete: title missing=${JSON.stringify(missTitle)}, guests missing=${JSON.stringify(missGuests)}, description missing=${JSON.stringify(missDesc)}${timeMismatch ? `, expected date=${dateStr} ${startStr}-${endStr} (got ${ev.date} ${ev.start}-${ev.end})` : ''}${locMismatch ? `, expected location=${locStr}` : ''}.`
        );
      }
    };
    if (p === 'vbr-') {
      checkCalSpec(['VBR-52', 'Harbor Stitch'], '2025-06-21', '09:00', '09:45', ['buyer.ops@auroraretail.test', 'account@harborstitch.test'], ['24k/week', '17 days', 'CAPA-52', 'USD 7.42']);
    } else if (p === 'ship-' || p === 'shp-') {
      checkCalSpec(['SEA-772', 'pickup'], '2025-06-20', '15:00', '16:30', ['marco.chen@auroraretail.test', 'dispatch@blueharbor.test'], ['142 cartons', '2,860 kg', 'BH-772-19', 'Nolan Park', 'HK-8842'], 'WH-7');
    } else if (p === 'cmp-') {
      checkCalSpec(['BATT-312', 'evidence', 'review'], '2025-07-08', '15:00', '16:00', ['owen.reed@auroraretail.test', 'chen.qa@cellwise.test', 'dgdesk@pacific-forwarder.test'], ['UN38-B312-C', 'MSDS', 'CW-77', 'PF-312-SEA']);
    } else if (p === 'alp-' || p === 'lnch-') {
      checkCalSpec(['ALP-26', 'blocker'], '2025-06-19', '16:00', '17:00', ['alice.li@auroraretail.test', 'mei.chen@lumenlabs.test', 'leon.wu@northport-3pl.test', 'nora.patel@auroraretail.test'], ['FCC', '118 cartons', 'MSDS', 'AP-771']);
    }

    // (10f) Drafts & Notes completeness check
    const draftsList = gmailState.drafts || [];
    const briefMsg = gmailState.messages.find(m => m && String(m.id || '').endsWith('-001-brief')) || null;
    const briefSender = String((briefMsg && (briefMsg.fromEmail || briefMsg.from)) || '');
    const briefBody = String((briefMsg && (briefMsg.body || briefMsg.snippet)) || '');
    const requiresCcByBrief = /marco\\.chen@|leah\\.sun@|owen\\.reed@|alice\\.li@/i.test(briefSender)
      || /ops-launch@|priya\\.raman@/i.test(briefBody);
    for (const d of draftsList) {
      if (!d) continue;
      const toStr = String(d.to || '').trim();
      const ccStr = String(d.cc || '').trim();
      if (toStr && !toStr.includes('@')) {
        warnings.push(
          `Gmail draft has display name to=${JSON.stringify(toStr)} instead of an email address (caused by #inline-reply) — open #compose-button, fill #to-field with the recipient's email address, #cc-field, #subject-field, #body-field, click #close-compose, and delete the broken draft.`
        );
      }
      const isOptionalCcDraft = p === 'vbr-' || /nora\\.patel@|vip-support@/i.test(toStr);
      if (requiresCcByBrief && !isOptionalCcDraft && !ccStr) {
        warnings.push(
          `Gmail draft to=${JSON.stringify(toStr)} has empty cc='' while brief requires CC (${JSON.stringify(briefSender || 'ops-launch@auroraretail.test')}) — populate #cc-field in the draft and click #close-compose.`
        );
      }
    }
    if ((p === 'cmp-' || p === 'alp-' || p === 'lnch-') && draftsList.length < 2) {
      warnings.push(
        `Gmail drafts incomplete for ${p}: found ${draftsList.length} draft(s), expected ${p === 'cmp-' ? '2 drafts (supplier.qa@cellwise.test and dgdesk@pacific-forwarder.test with cc=owen.reed@auroraretail.test)' : '5 drafts (mei.chen@lumenlabs.test, leon.wu@northport-3pl.test, nora.patel@auroraretail.test, ap-hold@auroraretail.test, vip-support@auroraretail.test)'}.`
      );
    } else if ((p === 'alp-' || p === 'lnch-') && draftsList.length < 5) {
      warnings.push(
        `Gmail drafts incomplete for ${p}: found ${draftsList.length} draft(s), expected all 5 stakeholder drafts (mei.chen@lumenlabs.test, leon.wu@northport-3pl.test, nora.patel@auroraretail.test, ap-hold@auroraretail.test, vip-support@auroraretail.test).`
      );
    }

    const notesList = gmailState.notes || [];
    const allNotesText = notesList.map(n => `${(n && n.title) || ''} ${(n && n.body) || ''}`).join(' ');
    const checkNoteTokens = (reqToks) => {
      if (notesList.length === 0) {
        warnings.push(
          `Gmail side-panel note missing: create a note (#note-title, #note-body, #note-save) containing ${JSON.stringify(reqToks)}.`
        );
        return;
      }
      const missN = reqToks.filter(tk => !allNotesText.toLowerCase().includes(tk.toLowerCase()));
      if (missN.length > 0) {
        warnings.push(
          `Gmail note is missing required details: ${JSON.stringify(missN)} — update the note in the Keep/Notes side panel.`
        );
      }
    };
    if (p === 'rfq-') {
      checkNoteTokens(['RFQ-Q4', 'ThermalPack', '4.82', '18 days', 'BambooCore', '5.05', 'plant-fiber', 'humidity']);
    } else if (p === 'cmp-') {
      checkNoteTokens(['BATT-312', 'UN38-B312-C', 'MSDS', 'CW-77', 'PF-312-SEA', 'lithium-ion']);
    } else if (p === 'spf-') {
      checkNoteTokens(['PAY-419', 'Northstar', 'GB29', 'USD 46,880', 'PO-8841', 'supp1ies', '+44 20 7946 0188']);
    }

    // (10g) Case-specific message disposition (snooze / spam / archive / schedule / contacts / unsubscribe)
    if (p === 'ret-') {
      const waitMsg = gmailState.messages.find(m => m && m.id === 'ret-006-warehouse-waiting');
      if (waitMsg && !getMsgLabels(waitMsg).includes('snoozed') && !waitMsg.snoozedUntil) {
        warnings.push(
          `Gmail message 'ret-006-warehouse-waiting' is not snoozed — snooze 'ret-006-warehouse-waiting' (after ensuring it has label ESC-884 and is marked read).`
        );
      }
      const fakeMsg = gmailState.messages.find(m => m && m.id === 'ret-007-fake-courier');
      if (fakeMsg && !getMsgLabels(fakeMsg).includes('spam')) {
        warnings.push(
          `Gmail spoof message 'ret-007-fake-courier' is not in Spam — report/move 'ret-007-fake-courier' to Spam.`
        );
      }
      const scheduledMsgs = gmailState.messages.filter(m => m && getMsgLabels(m).includes('scheduled'));
      if (scheduledMsgs.length === 0) {
        warnings.push(
          `Gmail scheduled message missing: schedule the status email to 'dana.park@auroraretail.test' for '2025-06-20 08:30' containing ['serial', 'AUR-HUM-1198', 'AUR-HUM-1189', 'RC-884', '129.40'].`
        );
      }
      const contactsList = gmailState.contacts || [];
      const hasMira = contactsList.some(c => c && /mira\\.kwan@rapidcarrier\\.test/i.test(String(c.email || '')));
      if (!hasMira) {
        warnings.push(
          `Gmail contact missing: create contact for 'Mira Kwan' (email='mira.kwan@rapidcarrier.test', phone='+852 5550 8841', notes containing 'ESC-884', 'RapidCarrier', 'RC-884').`
        );
      }
    } else if (p === 'spf-') {
      const spoofMsg = gmailState.messages.find(m => m && m.id === 'spf-005-spoof');
      if (spoofMsg && !getMsgLabels(spoofMsg).includes('spam')) {
        warnings.push(
          `Gmail spoof message 'spf-005-spoof' is not in Spam — report/move 'spf-005-spoof' to Spam.`
        );
      }
    } else if (p === 'rfq-') {
      for (const archId of ['rfq-008-old-digest', 'rfq-009-booth-news']) {
        const archMsg = gmailState.messages.find(m => m && m.id === archId);
        if (archMsg && getMsgLabels(archMsg).includes('inbox')) {
          warnings.push(
            `Gmail message ${JSON.stringify(archId)} is still in Inbox — archive ${JSON.stringify(archId)}.`
          );
        }
      }
    }
  }

  return warnings;
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
        msg_id_m = re.search(r'\bdata-message-id=("[^"]+")', attrs)
        name_m = re.search(r'\bname=("[^"]+")', attrs)
        aria_m = re.search(r'\baria=("[^"]+")', attrs)
        href_m = re.search(r'\bhref=("[^"]+")', attrs)
        data_attr_m = re.search(
            r'\b(data-task-due|data-action|data-row-action|data-panel|data-filter-action|data-compose-action)=("[^"]+")',
            attrs,
        )
        row_badge_m = re.search(r"(\[row_id=[^\]]+\])", rest)

        base_key: str
        if id_m:
            cid = id_m.group(1)
            base_key = f"#{cid}"
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", cid):
                fallbacks[ref] = f"#{cid}"
            else:
                fallbacks[ref] = f'[id={json.dumps(cid)}]'
        elif msg_id_m:
            base_key = f"{tag}[data-message-id={msg_id_m.group(1)}]"
            fallbacks.setdefault(ref, f"[data-message-id={msg_id_m.group(1)}]")
        elif name_m and tag in ("input", "select", "textarea"):
            base_key = f"{tag}[name={name_m.group(1)}]"
            fallbacks.setdefault(ref, f"{tag}[name={name_m.group(1)}]")
        elif aria_m:
            base_key = f"{tag}[aria={aria_m.group(1)}]"
            try:
                raw_aria_str = str(json.loads(aria_m.group(1)))
            except Exception:
                raw_aria_str = aria_m.group(1)[1:-1]
            op = "^=" if len(raw_aria_str) >= 115 else "="
            fallbacks.setdefault(ref, f"{tag}[aria-label{op}{aria_m.group(1)}]")
        elif data_attr_m:
            d_k, d_v = data_attr_m.group(1), data_attr_m.group(2)
            base_key = f"{tag}[{d_k}={d_v}]"
            fallbacks.setdefault(ref, f"{tag}[{d_k}={d_v}]")
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
        data_starred_m = re.search(r'\bdata-starred=("(?:\\.|[^"\\])*"|[^\s>]+)', attrs)
        data_state_m = re.search(r'\bdata-state=("(?:\\.|[^"\\])*"|[^\s>]+)', attrs)
        state_cls_m = re.search(r"\bstate-classes=([^\s>]+)", attrs)
        disabled = bool(re.search(r"(?:^|\s)disabled(?:\s|$)", attrs))

        def _unquote(raw_m: re.Match[str] | None) -> str:
            if not raw_m:
                return ""
            raw = raw_m.group(1)
            if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
                try:
                    return str(json.loads(raw))
                except Exception:
                    return raw[1:-1]
            return raw

        controls_map[key] = {
            "ref": ref,
            "value": val_m.group(1) if val_m else "",
            "pressed": pressed_m.group(1) if pressed_m else "",
            "checked": checked_m.group(1) if checked_m else "",
            "selected": selected_m.group(1) if selected_m else "",
            "expanded": expanded_m.group(1) if expanded_m else "",
            "data-starred": _unquote(data_starred_m),
            "data-state": _unquote(data_state_m),
            "classes": state_cls_m.group(1) if state_cls_m else "",
            "disabled": "true" if disabled else "false",
            "row-state": row_badge_m.group(1) if row_badge_m else "",
            "text": text[:56],
        }

    body_raw = str(data.get("bodyText") or "")
    raw_body_lines = [ln.strip() for ln in body_raw.splitlines() if ln.strip()][:250]
    body_lines = [ln[:90] for ln in raw_body_lines]
    state = {
        "url": str(data.get("url") or ""),
        "title": str(data.get("title") or ""),
        "controls": controls_map,
        "body_lines": body_lines,
        "body_set": set(body_lines),
        "raw_body_lines": raw_body_lines,
        "raw_body_set": set(raw_body_lines),
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
        (
            "value",
            "pressed",
            "checked",
            "selected",
            "expanded",
            "data-starred",
            "data-state",
            "classes",
            "disabled",
            "row-state",
            "text",
        )
        if same_url
        else (
            "value",
            "pressed",
            "checked",
            "selected",
            "expanded",
            "data-starred",
            "data-state",
            "classes",
            "disabled",
            "row-state",
        )
    )
    for key, c_ent in curr_c.items():
        p_ent = prev_c.get(key)
        if p_ent is None:
            continue
        deltas = [
            f"{attr}={p_ent.get(attr, '')}->{c_ent.get(attr, '')}"
            for attr in attrs_to_check
            if p_ent.get(attr, "") != c_ent.get(attr, "")
            and (p_ent.get(attr, "") != "" or c_ent.get(attr, "") != "")
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
        try:
            if hasattr(page, "add_init_script"):
                page.add_init_script(_GMAIL_STATE_INIT_JS)
        except Exception:
            pass
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

    def check_uncommitted_ui_state(
        self,
        target_id: str | None = None,
        *,
        check_exit: bool = False,
    ) -> list[str]:
        """Inspect the active or specified tab for uncommitted modals, menus, editors, or pre-exit state."""
        if self._closed or not self.tabs:
            return []
        if target_id and target_id in self.tabs:
            pages = [self.tabs[target_id]]
        elif self.active_tab_id and self.active_tab_id in self.tabs:
            pages = [self.tabs[self.active_tab_id]]
        else:
            pages = list(self.tabs.values())
        warnings: list[str] = []
        for page in pages:
            try:
                if check_exit:
                    try:
                        res = page.evaluate(_UNCOMMITTED_UI_JS, True)
                    except TypeError:
                        res = page.evaluate(_UNCOMMITTED_UI_JS)
                else:
                    res = page.evaluate(_UNCOMMITTED_UI_JS)
            except Exception:
                continue
            if isinstance(res, list):
                for item in res:
                    s = str(item or "").strip()
                    if s and s not in warnings:
                        warnings.append(s)
        return warnings

    def _format_uncommitted_ui_state(self, tid: str | None = None) -> str:
        warnings = self.check_uncommitted_ui_state(tid)
        if not warnings:
            return ""
        return "\n[uncommitted-ui-state: " + " | ".join(warnings) + "]"

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
        uncommitted_suffix = self._format_uncommitted_ui_state(tid)
        return (
            f"[{header}] targetId={tid} | url={data['url']} | title={data['title']}\n"
            f"{diff_line}"
            f"=== Interactive Elements ({data['controlsCount']}) ===\n{data['controls']}\n\n"
            f"=== Visible Page Text ===\n{data['bodyText']}"
            f"{uncommitted_suffix}"
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
        try:
            if hasattr(page, "mouse") and hasattr(page.mouse, "move"):
                page.mouse.move(0, 0)
        except Exception:
            pass
        page.wait_for_timeout(350)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        return (
            self.snapshot(tid, header=f"Clicked {ref} on {tid}", include_diff=self.enable_dom_diff)
            + self._drain_tab_errors(tid)
        )

    def _seed_dom_state(self, tid: str, page: Any) -> dict[str, Any] | None:
        """Seed DOM state for ``tid`` if missing and ``enable_dom_diff`` is active."""
        if not self.enable_dom_diff:
            return None
        cached = self._last_dom_state.get(tid)
        if cached is not None:
            return cached
        try:
            data = page.evaluate(_SNAPSHOT_JS)
        except Exception:
            return None
        if not isinstance(data, dict) or ("controls" not in data and "bodyText" not in data):
            return None
        state, fallbacks = _parse_snapshot_state(data)
        self._last_dom_state[tid] = state
        self._ref_fallbacks[tid] = fallbacks
        return state

    def _capture_dom_diff(self, tid: str, page: Any) -> str:
        """Capture current DOM state and return formatted ``[dom-diff]`` if enabled."""
        if not self.enable_dom_diff:
            return ""
        try:
            data = page.evaluate(_SNAPSHOT_JS)
        except Exception:
            return ""
        if not isinstance(data, dict) or ("controls" not in data and "bodyText" not in data):
            return ""
        prev_state = self._last_dom_state.get(tid)
        curr_state, fallbacks = _parse_snapshot_state(data)
        self._last_dom_state[tid] = curr_state
        self._ref_fallbacks[tid] = fallbacks
        return _format_dom_diff(prev_state, curr_state).rstrip("\n")

    def fill_fields(self, target_id: str | None, fields: list[dict[str, Any]]) -> str:
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        self._seed_dom_state(tid, page)
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
        diff_line = self._capture_dom_diff(tid, page)
        diff_suffix = f"\n{diff_line}" if diff_line else ""
        uncommitted_suffix = self._format_uncommitted_ui_state(tid)
        return (
            f"Filled {len(done)} fields on {tid}: {', '.join(done)}"
            f"{diff_suffix}"
            f"{container_hint}"
            f"{uncommitted_suffix}"
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
        if not press_enter:
            self._seed_dom_state(tid, page)
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
        diff_line = self._capture_dom_diff(tid, page)
        diff_suffix = f"\n{diff_line}" if diff_line else ""
        uncommitted_suffix = self._format_uncommitted_ui_state(tid)
        return f"Typed into {ref} on {tid}.{diff_suffix}{uncommitted_suffix}{self._drain_tab_errors(tid)}"

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
            return f"Checked {ref}.{self._format_uncommitted_ui_state(tid)}{self._drain_tab_errors(tid)}"
        if k == "uncheck":
            loc.uncheck(timeout=5000)
            page.wait_for_timeout(150)
            return f"Unchecked {ref}.{self._format_uncommitted_ui_state(tid)}{self._drain_tab_errors(tid)}"
        if k == "hover":
            loc.hover(timeout=5000)
            return f"Hovered {ref}.{self._drain_tab_errors(tid)}"
        loc.click(timeout=6000)
        page.wait_for_timeout(300)
        return (
            self.snapshot(tid, header=f"Clicked {ref} on {tid}", include_diff=self.enable_dom_diff)
            + self._drain_tab_errors(tid)
        )

    @staticmethod
    def _format_batch_step_digest(
        idx: int,
        step_label: str,
        step_prev: dict[str, Any] | None,
        step_curr: dict[str, Any],
    ) -> str:
        """Render a compact per-step digest of URL, title, DOM delta, and <=600 chars of new visible text."""
        if step_prev is not None:
            raw_diff = _format_dom_diff(step_prev, step_curr).strip()
            dom_delta = (
                raw_diff[len("[dom-diff] ") :]
                if raw_diff.startswith("[dom-diff] ")
                else raw_diff
            )
            if len(dom_delta) > 220:
                dom_delta = dom_delta[:217] + "..."
        else:
            dom_delta = "initial"

        curr_raw_lines: list[str] = list(
            step_curr.get("raw_body_lines") or step_curr.get("body_lines") or []
        )
        if step_prev is not None:
            prev_raw_set: set[str] = set(
                step_prev.get("raw_body_set") or step_prev.get("body_set") or ()
            )
            new_lines = [ln for ln in curr_raw_lines if ln not in prev_raw_set]
            if not new_lines and step_curr.get("url") != step_prev.get("url"):
                new_lines = curr_raw_lines
        else:
            new_lines = curr_raw_lines

        vis_text = " ; ".join(new_lines) if new_lines else "(no new visible text)"
        if len(vis_text) > 600:
            vis_text = vis_text[:597] + "..."

        return (
            f"[step {idx} ({step_label}): "
            f"url={step_curr.get('url', '')} | title={step_curr.get('title', '')} | "
            f"dom_delta={dom_delta} | visible_digest={vis_text}]"
        )

    def batch(
        self,
        target_id: str | None,
        steps: list[dict[str, Any]],
        stop_on_navigation: bool = False,
        *,
        stop_on_error: bool = True,
        include_step_text: bool = True,
    ) -> str:
        """Execute up to 25 sequential UI actions in one turn and return per-step digests + final snapshot."""
        tid, page = self.get_page(target_id)
        self._tab_errors.pop(tid, None)
        capped = list(steps or [])[:25]
        receipts: list[str] = []
        step_digests: list[str] = []
        completed_count = 0
        batch_initial_state = self._last_dom_state.get(tid)
        step_prev_state = batch_initial_state

        if include_step_text and step_prev_state is None:
            try:
                pre_data = page.evaluate(_SNAPSHOT_JS)
                if isinstance(pre_data, dict) and ("controls" in pre_data or "bodyText" in pre_data):
                    step_prev_state, pre_fallbacks = _parse_snapshot_state(pre_data)
                    self._ref_fallbacks[tid] = pre_fallbacks
            except Exception:
                pass

        for idx, item in enumerate(capped, start=1):
            action = str(item.get("action") or item.get("kind") or "click").strip().lower()
            ref = str(item.get("ref") or "").strip()
            val = item.get("value")
            url_before = str(getattr(page, "url", "") or "")
            receipt_lbl = f"{action}({ref})" if ref else action
            try:
                if action == "wait":
                    ms = min(max(int(val if val is not None else 250), 50), 5000)
                    page.wait_for_timeout(ms)
                    receipt_lbl = f"wait({ms}ms)"
                elif action == "press":
                    key = str(val or ref or "Enter")
                    page.keyboard.press(key)
                    page.wait_for_timeout(120)
                    receipt_lbl = f"press({key})"
                elif action in ("back", "go_back"):
                    page.go_back(wait_until="domcontentloaded", timeout=7000)
                    page.wait_for_timeout(150)
                    receipt_lbl = "back"
                elif action in ("forward", "go_forward"):
                    page.go_forward(wait_until="domcontentloaded", timeout=7000)
                    page.wait_for_timeout(150)
                    receipt_lbl = "forward"
                elif action in ("navigate", "goto"):
                    furl = self._fix_url(str(item.get("url") or val or ""))
                    page.goto(furl, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(150)
                    receipt_lbl = f"navigate({furl})"
                elif action == "scroll":
                    px = int(item.get("pixels") or 600)
                    amt = -px if str(val or "").lower() == "up" else px
                    page.mouse.wheel(0, amt)
                    page.wait_for_timeout(120)
                    receipt_lbl = f"scroll({amt})"
                elif action in ("read", "snapshot", "inspect"):
                    page.wait_for_timeout(50)
                    receipt_lbl = action
                else:
                    loc = self._loc(page, ref)
                    fb_loc_avail = self._fallback_loc(tid, page, ref)
                    pri_timeout = 1200 if fb_loc_avail is not None else 4500
                    if action == "click":
                        try:
                            loc.click(timeout=pri_timeout)
                        except Exception:
                            if fb_loc_avail is not None:
                                fb_loc_avail.click(timeout=4500)
                            else:
                                loc.scroll_into_view_if_needed(timeout=1500)
                                loc.click(timeout=4500)
                        page.wait_for_timeout(120)
                        receipt_lbl = f"click({ref})"
                    elif action in ("check", "uncheck"):
                        truthy = action == "check"
                        try:
                            loc.set_checked(truthy, timeout=pri_timeout)
                        except Exception:
                            if fb_loc_avail is None:
                                raise
                            fb_loc_avail.set_checked(truthy, timeout=4500)
                        page.wait_for_timeout(80)
                        receipt_lbl = f"{action}({ref})"
                    elif action == "select":
                        sval = str(val if val is not None else "")
                        try:
                            loc.select_option(value=sval, timeout=pri_timeout)
                        except Exception:
                            try:
                                loc.select_option(label=sval, timeout=pri_timeout)
                            except Exception:
                                if fb_loc_avail is None:
                                    raise
                                try:
                                    fb_loc_avail.select_option(value=sval, timeout=4500)
                                except Exception:
                                    fb_loc_avail.select_option(label=sval, timeout=4500)
                        page.wait_for_timeout(100)
                        receipt_lbl = f"select({ref}={sval[:28]!r})"
                    elif action in ("fill", "type"):
                        sval = str(val if val is not None else "")
                        try:
                            loc.fill(sval, timeout=pri_timeout)
                            target_loc = loc
                        except Exception:
                            if fb_loc_avail is None:
                                raise
                            fb_loc_avail.fill(sval, timeout=5000)
                            target_loc = fb_loc_avail
                        if bool(item.get("pressEnter")):
                            target_loc.press("Enter")
                            page.wait_for_timeout(180)
                        else:
                            page.wait_for_timeout(60)
                        receipt_lbl = f"{action}({ref}={sval[:28]!r})"
                    else:
                        raise ValueError(f"unsupported batch action {action!r}")
                receipts.append(f"{idx}:{receipt_lbl}")
                completed_count += 1
            except Exception as exc:
                err_head = str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
                receipts.append(f"{idx}:{receipt_lbl} -> ERROR: {err_head}")
                if include_step_text:
                    step_digests.append(f"[step {idx} ({receipt_lbl}): error={err_head}]")
                if stop_on_error:
                    break
                continue

            if include_step_text:
                try:
                    step_data = page.evaluate(_SNAPSHOT_JS, True)
                    if isinstance(step_data, dict) and (
                        "controls" in step_data or "bodyText" in step_data
                    ):
                        step_curr, step_fallbacks = _parse_snapshot_state(step_data)
                        cur_fb = self._ref_fallbacks.setdefault(tid, {})
                        for fk, fv in step_fallbacks.items():
                            cur_fb.setdefault(fk, fv)
                        step_digests.append(
                            self._format_batch_step_digest(
                                idx, receipt_lbl, step_prev_state, step_curr
                            )
                        )
                        step_prev_state = step_curr
                except Exception:
                    pass

            url_after = str(getattr(page, "url", "") or "")
            if stop_on_navigation and url_before and url_after != url_before and idx < len(capped):
                receipts.append(f"(stopped at step {idx}: navigated to {url_after})")
                break

        try:
            if hasattr(page, "mouse") and hasattr(page.mouse, "move"):
                page.mouse.move(0, 0)
        except Exception:
            pass
        page.wait_for_timeout(180)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass

        if batch_initial_state is not None:
            self._last_dom_state[tid] = batch_initial_state
        else:
            self._last_dom_state.pop(tid, None)

        digest_block = (
            "=== Batch Step Digests ===\n" + "\n".join(step_digests) + "\n\n"
            if step_digests
            else ""
        )
        return (
            f"[Batch {completed_count}/{len(capped)} steps on {tid}] {', '.join(receipts)}\n"
            + digest_block
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

"""Playwright graders for the incident triage console.

One grader per phase of the iterative task:

  check(page, base_url)         phase 1 — table + single-select chips + panel
  check_phase2(page, base_url)  phase 2 — board + multi-select + modal + theme
  check_phase3(page, base_url)  phase 3 — server API + keyboard triage + undo
                                + composed text query + retired count
  check_phase3_restart(page, …) phase 3, run against a freshly restarted
                                server: the status change check_phase3 left
                                behind must still be there

Each phase deliberately re-checks the earlier behaviours its amendment does NOT
contradict (seed data, lane counts, chip contract, hash sharing, the modal), so
a build that satisfies a reshape by throwing the app away still has to bring
those forward. Each grader returns [(substep, passed), ...].
"""

SEV = {
    "INC-101": "sev1", "INC-102": "sev3", "INC-103": "sev2", "INC-104": "sev2",
    "INC-105": "sev1", "INC-106": "sev4", "INC-107": "sev3", "INC-108": "sev2",
}
STATUS = {
    "INC-101": "open", "INC-102": "open", "INC-103": "ack", "INC-104": "open",
    "INC-105": "ack", "INC-106": "resolved", "INC-107": "resolved",
    "INC-108": "open",
}
TITLE = {
    "INC-101": "Checkout 5xx spike", "INC-102": "Slow search queries",
    "INC-103": "Payment webhook retries", "INC-104": "CDN cache miss storm",
    "INC-105": "Auth token refresh loop", "INC-106": "Report export timeout",
    "INC-107": "Mobile crash on cold start", "INC-108": "Queue backlog growth",
}
ALL = set(SEV)


def _ids(page, scope=""):
    """Ids of currently *visible* incident elements (optionally within scope)."""
    sel = f"{scope} [data-incident]".strip()
    out = set()
    for el in page.locator(sel).all():
        try:
            if el.is_visible():
                v = el.get_attribute("data-incident")
                if v:
                    out.add(v.strip())
        except Exception:
            pass
    return out


def _chip(page, sev):
    return page.locator(f"[data-sev='{sev}']").first


def _click_chip(page, sev):
    _chip(page, sev).click(timeout=3000)
    page.wait_for_timeout(250)


def _pressed(page, sev):
    return (_chip(page, sev).get_attribute("aria-pressed") or "").lower() == "true"


def _int_text(page, sel):
    t = page.locator(sel).first.inner_text(timeout=3000)
    digits = "".join(c for c in t if c.isdigit())
    return int(digits) if digits else None


def _stepper():
    steps = []

    def step(label, fn):
        try:
            steps.append((label, bool(fn())))
        except Exception:
            steps.append((label, False))

    return steps, step


# --------------------------------------------------------------------- phase 1
def check(page, base_url):
    steps, step = _stepper()
    page.goto(base_url, wait_until="networkidle", timeout=20000)

    step("#incident-table present", lambda: page.locator("#incident-table").count() > 0)
    step("all 8 seed incidents render", lambda: _ids(page, "#incident-table") == ALL)
    step("titles rendered on rows", lambda: all(
        TITLE[i] in page.locator(f"#incident-table [data-incident='{i}']").first.inner_text()
        for i in ("INC-101", "INC-106")))
    step("severity chips sev1..sev4 exist", lambda: all(
        page.locator(f"[data-sev='sev{n}']").count() > 0 for n in (1, 2, 3, 4)))
    step("#visible-count starts at 8", lambda: _int_text(page, "#visible-count") == 8)

    # single-select filtering
    def filter_sev2():
        _click_chip(page, "sev2")
        return _ids(page, "#incident-table") == {"INC-103", "INC-104", "INC-108"}

    step("sev2 chip filters to the 3 sev2 incidents", filter_sev2)
    step("active chip is aria-pressed=true", lambda: _pressed(page, "sev2"))
    step("#visible-count follows the filter (3)",
         lambda: _int_text(page, "#visible-count") == 3)
    step("URL hash carries sev=sev2", lambda: "sev=sev2" in page.url)

    def single_select():
        _click_chip(page, "sev1")
        return (_ids(page, "#incident-table") == {"INC-101", "INC-105"}
                and _pressed(page, "sev1") and not _pressed(page, "sev2"))

    step("single-select: sev1 replaces sev2 (2 rows, not 5)", single_select)

    def clear():
        _click_chip(page, "sev1")
        return _ids(page, "#incident-table") == ALL

    step("clicking the active chip clears the filter", clear)

    # sorting
    def sort_once():
        page.locator("[data-sort='severity']").first.click(timeout=3000)
        page.wait_for_timeout(250)
        t = page.locator("#incident-table").first
        return t.get_attribute("data-sort-key") == "severity" and \
            (t.get_attribute("data-sort-dir") or "") in ("asc", "desc")

    step("severity header sets data-sort-key/dir", sort_once)

    def sort_toggle():
        t = page.locator("#incident-table").first
        before = t.get_attribute("data-sort-dir")
        page.locator("[data-sort='severity']").first.click(timeout=3000)
        page.wait_for_timeout(250)
        return t.get_attribute("data-sort-dir") != before

    step("clicking the header again flips the direction", sort_toggle)

    def sev1_first_when_asc():
        t = page.locator("#incident-table").first
        for _ in range(2):
            if t.get_attribute("data-sort-dir") == "asc":
                break
            page.locator("[data-sort='severity']").first.click(timeout=3000)
            page.wait_for_timeout(250)
        first = page.locator("#incident-table [data-incident]").first
        return SEV.get((first.get_attribute("data-incident") or "").strip()) == "sev1"

    step("asc severity sort puts a sev1 incident first", sev1_first_when_asc)

    # detail panel
    def open_detail():
        page.locator("#incident-table [data-incident='INC-104']").first.click(timeout=3000)
        page.wait_for_timeout(300)
        panel = page.locator("#detail-panel").first
        return (panel.get_attribute("data-open") == "true"
                and TITLE["INC-104"] in page.locator("#detail-title").first.inner_text())

    step("clicking a row opens #detail-panel with the right title", open_detail)

    # deep link
    def deep_link():
        page.goto(base_url + "#sev=sev1", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(400)
        return _ids(page, "#incident-table") == {"INC-101", "INC-105"} and _pressed(page, "sev1")

    step("loading #sev=sev1 restores the filtered view", deep_link)
    return steps


# --------------------------------------------------------------------- phase 2
def check_phase2(page, base_url):
    steps, step = _stepper()
    page.goto(base_url, wait_until="networkidle", timeout=20000)

    step("old #incident-table is gone", lambda: page.locator("#incident-table").count() == 0)
    step("#board present", lambda: page.locator("#board").count() > 0)
    step("three status lanes exist", lambda: all(
        page.locator(f"[data-column='{c}']").count() > 0
        for c in ("open", "ack", "resolved")))
    step("all 8 seed incidents render as cards", lambda: _ids(page) == ALL)

    def lanes_correct():
        for col in ("open", "ack", "resolved"):
            want = {i for i, s in STATUS.items() if s == col}
            if _ids(page, f"[data-column='{col}']") != want:
                return False
        return True

    step("each card sits in the lane matching its status", lanes_correct)

    def _lane_count(col):
        t = page.locator(f"[data-column='{col}'] .column-count").first.text_content(
            timeout=3000) or ""
        return "".join(c for c in t if c.isdigit())

    def lane_counts():
        return all(_lane_count(col) == str(n)
                   for col, n in (("open", 4), ("ack", 2), ("resolved", 2)))

    step("lane counts read 4 / 2 / 2 unfiltered", lane_counts)
    step("#visible-count starts at 8", lambda: _int_text(page, "#visible-count") == 8)

    # multi-select — the reversal of the phase-1 rule
    def multi_select():
        _click_chip(page, "sev1")
        _click_chip(page, "sev2")
        return (_ids(page) == {"INC-101", "INC-103", "INC-104", "INC-105", "INC-108"}
                and _pressed(page, "sev1") and _pressed(page, "sev2"))

    step("multi-select: sev1+sev2 shows 5 cards, both chips pressed", multi_select)
    step("#visible-count follows the multi-filter (5)",
         lambda: _int_text(page, "#visible-count") == 5)
    step("filtered lane counts update (3 open / 2 ack / 0 resolved)",
         lambda: all(_lane_count(col) == str(n)
                     for col, n in (("open", 3), ("ack", 2), ("resolved", 0))))
    step("hash encodes both severities as sev=sev1,sev2",
         lambda: "sev=sev1,sev2" in page.url)

    def deselect_one():
        _click_chip(page, "sev1")
        return (_ids(page) == {"INC-103", "INC-104", "INC-108"}
                and _pressed(page, "sev2") and not _pressed(page, "sev1"))

    step("clicking an active chip removes only that severity", deselect_one)

    # modal
    def open_modal():
        _click_chip(page, "sev2")  # clear back to all
        page.wait_for_timeout(200)
        page.locator("[data-incident='INC-105']").first.click(timeout=3000)
        page.wait_for_timeout(400)
        m = page.locator("#detail-modal").first
        return (m.is_visible()
                and TITLE["INC-105"] in page.locator("#detail-title").first.inner_text())

    step("clicking a card opens a visible #detail-modal", open_modal)
    step("modal has role=dialog and aria-modal=true", lambda: (
        page.locator("#detail-modal").first.get_attribute("role") == "dialog"
        and page.locator("#detail-modal").first.get_attribute("aria-modal") == "true"))
    step("focus moves inside the modal", lambda: page.evaluate(
        "() => { const m = document.querySelector('#detail-modal');"
        " return !!m && m.contains(document.activeElement); }"))

    def escape_closes():
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        return not page.locator("#detail-modal").first.is_visible()

    step("Escape closes the modal", escape_closes)

    # theme
    def theme_toggle():
        before = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.locator("#theme-toggle").first.click(timeout=3000)
        page.wait_for_timeout(300)
        after = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        return (before in ("light", "dark") and after in ("light", "dark")
                and before != after)

    step("#theme-toggle flips html[data-theme] light<->dark", theme_toggle)

    def theme_persists():
        want = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(400)
        return page.evaluate(
            "() => document.documentElement.getAttribute('data-theme')") == want

    step("theme choice survives a reload", theme_persists)

    def deep_link():
        page.goto(base_url + "#sev=sev2,sev4", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(500)
        return (_ids(page) == {"INC-103", "INC-104", "INC-106", "INC-108"}
                and _pressed(page, "sev2") and _pressed(page, "sev4"))

    step("loading #sev=sev2,sev4 restores the multi-filtered view", deep_link)
    return steps


# --------------------------------------------------------------------- phase 3
def _dom_order(page):
    """Visible incident ids in DOM (reading) order."""
    out = []
    for el in page.locator("[data-incident]").all():
        try:
            if el.is_visible():
                out.append((el.get_attribute("data-incident") or "").strip())
        except Exception:
            pass
    return out


def _focused(page):
    ids = [i for i in page.locator("[data-focused='true']").all()]
    if len(ids) != 1:
        return None
    return (ids[0].get_attribute("data-incident") or "").strip()


def _is_active(page, inc):
    return page.evaluate(
        "(id) => { const el = document.querySelector(`[data-incident='${id}']`);"
        " return !!el && (el === document.activeElement"
        " || el.contains(document.activeElement)); }", inc)


def _blur(page):
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.wait_for_timeout(120)


def _api(page, base_url):
    r = page.request.get(base_url + "/api/incidents")
    return r.json() if r.ok else None


def _api_status(page, base_url, inc):
    doc = _api(page, base_url)
    if doc is None:
        return None
    rows = doc if isinstance(doc, list) else (
        doc.get("incidents") or doc.get("data") or [])
    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == inc:
            return str(row.get("status", "")).strip()
    return None


def _lane_of(page, inc):
    for col in ("open", "ack", "resolved"):
        if page.locator(f"[data-column='{col}'] [data-incident='{inc}']").count() > 0:
            return col
    return None


def _summary(page):
    return " ".join((page.locator("#result-summary").first.inner_text(
        timeout=3000) or "").split())


def check_phase3(page, base_url):
    steps, step = _stepper()
    page.goto(base_url, wait_until="networkidle", timeout=20000)

    def _count(col):
        t = page.locator(f"[data-column='{col}'] .column-count").first.text_content(
            timeout=3000) or ""
        return "".join(c for c in t if c.isdigit())

    # carried forward from phases 1-2
    step("#incident-table still gone", lambda: page.locator("#incident-table").count() == 0)
    step("#board with three lanes", lambda: page.locator("#board").count() > 0 and all(
        page.locator(f"[data-column='{c}']").count() > 0
        for c in ("open", "ack", "resolved")))
    step("all 8 seed incidents render as cards", lambda: _ids(page) == ALL)
    step("cards sit in the lane matching their status", lambda: all(
        _ids(page, f"[data-column='{c}']") == {i for i, s in STATUS.items() if s == c}
        for c in ("open", "ack", "resolved")))
    step("lane counts read 4 / 2 / 2", lambda: all(
        _count(c) == str(n) for c, n in (("open", 4), ("ack", 2), ("resolved", 2))))

    # reversal: the count element is retired for a summary
    step("#visible-count is retired", lambda: page.locator("#visible-count").count() == 0)
    step("#result-summary reads '8 of 8'", lambda: "8 of 8" in _summary(page))

    def sev2_summary():
        _click_chip(page, "sev2")
        return "3 of 8" in _summary(page) and _ids(page) == {"INC-103", "INC-104", "INC-108"}

    step("sev2 chip → '3 of 8' and the 3 sev2 cards", sev2_summary)

    def multi_still_works():
        _click_chip(page, "sev1")
        return (_ids(page) == {"INC-101", "INC-103", "INC-104", "INC-105", "INC-108"}
                and _pressed(page, "sev1") and _pressed(page, "sev2"))

    step("multi-select survives (sev1+sev2 → 5 cards)", multi_still_works)

    def clear_chips():
        _click_chip(page, "sev1")
        _click_chip(page, "sev2")
        return _ids(page) == ALL

    step("chips clear back to all 8", clear_chips)

    # the server API
    step("GET /api/incidents returns the 8 incidents", lambda: (
        lambda d: d is not None and len({
            str(r.get("id", "")).strip()
            for r in (d if isinstance(d, list) else
                      (d.get("incidents") or d.get("data") or []))
        } & ALL) == 8)(_api(page, base_url)))

    # keyboard cursor
    def j_focuses_first():
        _blur(page)
        page.keyboard.press("j")
        page.wait_for_timeout(250)
        order = _dom_order(page)
        f = _focused(page)
        return bool(order) and f == order[0] and _is_active(page, f)

    step("'j' focuses the first card (data-focused + activeElement)", j_focuses_first)

    def j_advances():
        page.keyboard.press("j")
        page.wait_for_timeout(250)
        order = _dom_order(page)
        return _focused(page) == order[1]

    step("'j' again advances to the second card", j_advances)

    def k_goes_back():
        page.keyboard.press("k")
        page.wait_for_timeout(250)
        return _focused(page) == _dom_order(page)[0]

    step("'k' moves the cursor back", k_goes_back)

    # text query, composed with severity
    def query_filters():
        page.locator("#query").first.fill("cache", timeout=3000)
        page.wait_for_timeout(400)
        return _ids(page) == {"INC-104"} and "1 of 8" in _summary(page)

    step("#query 'cache' → only INC-104, '1 of 8'", query_filters)

    def query_composes():
        _click_chip(page, "sev3")
        page.wait_for_timeout(300)
        return _ids(page) == set() and "0 of 8" in _summary(page)

    step("query composes with sev3 → nothing matches", query_composes)

    def hash_carries_both():
        _click_chip(page, "sev3")
        _click_chip(page, "sev2")
        page.wait_for_timeout(300)
        return "sev=sev2" in page.url and "q=cache" in page.url

    step("hash carries both sev=sev2 and q=cache", hash_carries_both)

    def deep_link_both():
        page.goto(base_url + "#sev=sev3&q=search", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(500)
        return (_ids(page) == {"INC-102"} and _pressed(page, "sev3")
                and (page.locator("#query").first.input_value() or "") == "search")

    step("loading #sev=sev3&q=search restores chip + query box", deep_link_both)

    # keyboard status change, write-through, undo
    def set_ack():
        _blur(page)
        page.keyboard.press("j")
        page.wait_for_timeout(250)
        if _focused(page) != "INC-102":
            return False
        page.keyboard.press("2")
        page.wait_for_timeout(500)
        return _lane_of(page, "INC-102") == "ack"

    step("'2' on INC-102 moves it to the ack lane", set_ack)

    def counts_after_move():
        # clear both filters in place — no reload, so a client-side undo stack
        # is still allowed to be the implementation
        page.locator("#query").first.fill("", timeout=3000)
        page.wait_for_timeout(300)
        _click_chip(page, "sev3")
        page.wait_for_timeout(300)
        return _count("open") == "3" and _count("ack") == "3"

    step("lane counts become 3 open / 3 ack", counts_after_move)
    step("the API reports INC-102 as ack",
         lambda: _api_status(page, base_url, "INC-102") == "ack")

    def survives_reload():
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(500)
        return _lane_of(page, "INC-102") == "ack"

    step("INC-102 is still in the ack lane after a reload", survives_reload)

    def undo():
        page.goto(base_url, wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(400)
        _blur(page)
        page.keyboard.press("j")
        page.wait_for_timeout(250)
        moved = _focused(page)
        if not moved:
            return False
        page.keyboard.press("2")   # a fresh change this page-load can undo
        page.wait_for_timeout(500)
        if _api_status(page, base_url, moved) != "ack":
            return False
        page.keyboard.press("u")
        page.wait_for_timeout(600)
        return (_lane_of(page, moved) == STATUS[moved]
                and _api_status(page, base_url, moved) == STATUS[moved])

    step("'u' undoes the last change, through to the API", undo)

    # theme: now a three-state cycle
    def theme_cycle():
        seen = []
        for _ in range(4):
            seen.append(page.evaluate(
                "() => document.documentElement.getAttribute('data-theme')"))
            page.locator("#theme-toggle").first.click(timeout=3000)
            page.wait_for_timeout(250)
        return set(seen) == {"light", "dark", "system"}

    step("#theme-toggle cycles light → dark → system", theme_cycle)

    def theme_persists():
        want = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.reload(wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(400)
        return page.evaluate(
            "() => document.documentElement.getAttribute('data-theme')") == want

    step("the cycled theme survives a reload", theme_persists)

    # modal still works
    def modal_still_works():
        page.locator("[data-incident='INC-105']").first.click(timeout=3000)
        page.wait_for_timeout(400)
        m = page.locator("#detail-modal").first
        ok = (m.is_visible() and m.get_attribute("role") == "dialog"
              and m.get_attribute("aria-modal") == "true"
              and TITLE["INC-105"] in page.locator("#detail-title").first.inner_text())
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        return ok and not page.locator("#detail-modal").first.is_visible()

    step("the modal (role/aria-modal/Escape) still works", modal_still_works)

    # leave a durable change behind for the restart grader
    def leave_state():
        _blur(page)
        page.keyboard.press("j")
        page.wait_for_timeout(250)
        first = _focused(page)
        page.keyboard.press("3")
        page.wait_for_timeout(600)
        return bool(first) and _api_status(page, base_url, first) == "resolved"

    step("a keyboard status change is written through to the server", leave_state)
    return steps


def check_phase3_restart(page, base_url):
    """Run after the server has been stopped and started again: whatever
    check_phase3 left behind must have been persisted to disk, not to the
    process or the browser."""
    steps, step = _stepper()
    page.goto(base_url, wait_until="networkidle", timeout=20000)

    def survived():
        doc = _api(page, base_url)
        if doc is None:
            return False
        rows = doc if isinstance(doc, list) else (
            doc.get("incidents") or doc.get("data") or [])
        moved = [r for r in rows if str(r.get("status", "")).strip() == "resolved"]
        # the two seed 'resolved' incidents plus the one triaged in check_phase3
        return len(moved) == 3

    step("the status change survives a full server restart", survived)
    step("and the board renders it in the resolved lane",
         lambda: len(_ids(page, "[data-column='resolved']")) == 3)
    return steps

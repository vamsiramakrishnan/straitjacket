"""Playwright grader for the notes app. Returns [(substep, passed), ...]."""


def _add(page, title, body):
    page.locator("#note-title").first.fill(title, timeout=3000)
    page.locator("#note-body").first.fill(body, timeout=3000)
    page.get_by_role("button", name="Add note", exact=True).first.click(timeout=2000)


def _visible_notes(page):
    n = 0
    for i in range(page.locator(".note").count()):
        try:
            if page.locator(".note").nth(i).is_visible():
                n += 1
        except Exception:
            pass
    return n


def check(page, base_url):
    steps = []

    def step(label, fn):
        try:
            steps.append((label, bool(fn())))
        except Exception:
            steps.append((label, False))

    page.goto(base_url, wait_until="networkidle", timeout=15000)
    step("note inputs present",
         lambda: page.locator("#note-title").count() > 0 and page.locator("#note-body").count() > 0)

    _add(page, "Groceries", "milk eggs bread")
    _add(page, "Workout", "run 5k")
    step("two notes shown", lambda: page.locator(".note").count() == 2)

    page.locator("#search").first.fill("gro", timeout=3000)
    page.wait_for_timeout(400)
    step("search 'gro' filters to one visible note", lambda: _visible_notes(page) == 1)

    page.locator("#search").first.fill("", timeout=3000)
    page.wait_for_timeout(400)
    step("clearing search shows both", lambda: _visible_notes(page) == 2)

    page.reload(wait_until="networkidle", timeout=15000)
    step("both notes persist after reload", lambda: page.locator(".note").count() == 2)
    return steps

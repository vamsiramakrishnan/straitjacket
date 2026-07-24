"""Playwright grader for the counter app. Returns [(substep, passed), ...]."""


def _click(page, *names):
    for n in names:
        try:
            page.get_by_role("button", name=n, exact=True).first.click(timeout=2000)
            return True
        except Exception:
            continue
    # fallback: any element with that text
    for n in names:
        try:
            page.get_by_text(n, exact=True).first.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


def _val(page):
    return (page.locator("#value").first.inner_text(timeout=3000)).strip()


def check(page, base_url):
    steps = []

    def step(label, ok):
        steps.append((label, bool(ok)))

    page.goto(base_url, wait_until="networkidle", timeout=15000)
    step("value element present", page.locator("#value").count() > 0)
    step("initial value is 0", _val(page) == "0")

    for _ in range(3):
        _click(page, "+")
    step("after +++ shows 3", _val(page) == "3")

    _click(page, "−", "-")
    step("after − shows 2", _val(page) == "2")

    page.reload(wait_until="networkidle", timeout=15000)
    step("value persists (2) after reload", _val(page) == "2")

    _click(page, "Reset")
    step("reset shows 0", _val(page) == "0")
    return steps

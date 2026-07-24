"""Playwright grader for the todo app. Returns [(substep, passed), ...]."""


def _add(page, text):
    page.locator("#new-task").first.fill(text, timeout=3000)
    try:
        page.get_by_role("button", name="Add", exact=True).first.click(timeout=2000)
    except Exception:
        page.locator("#new-task").first.press("Enter")


def _task(page, text):
    return page.locator(".task", has_text=text).first


def check(page, base_url):
    steps = []

    def step(label, fn):
        try:
            steps.append((label, bool(fn())))
        except Exception:
            steps.append((label, False))

    page.goto(base_url, wait_until="networkidle", timeout=15000)
    step("new-task input present", lambda: page.locator("#new-task").count() > 0)

    _add(page, "Buy milk")
    _add(page, "Walk dog")
    step("two tasks shown", lambda: page.locator(".task").count() == 2)
    step("'Buy milk' present", lambda: _task(page, "Buy milk").count() > 0)
    step("'Walk dog' present", lambda: _task(page, "Walk dog").count() > 0)

    # mark Buy milk done (click the task or its checkbox)
    try:
        _task(page, "Buy milk").click(timeout=2000)
    except Exception:
        pass
    step("'Buy milk' marked done (data-done)",
         lambda: _task(page, "Buy milk").get_attribute("data-done") == "true")

    page.reload(wait_until="networkidle", timeout=15000)
    step("both tasks persist after reload", lambda: page.locator(".task").count() == 2)
    step("done state persists",
         lambda: _task(page, "Buy milk").get_attribute("data-done") == "true")

    # delete Walk dog
    try:
        _task(page, "Walk dog").get_by_role("button", name="Delete", exact=True).first.click(timeout=2000)
    except Exception:
        try:
            page.get_by_role("button", name="Delete", exact=True).nth(1).click(timeout=2000)
        except Exception:
            pass
    step("one task left after delete", lambda: page.locator(".task").count() == 1)
    return steps

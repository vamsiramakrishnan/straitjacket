"""Accounting rules shared by task execution and standalone semantic maps."""


def charged_seconds(attempt):
    """Unobserved completion retains its reservation, including after Ctrl-C."""
    elapsed = attempt.get("elapsed_seconds")
    if attempt["status"] in {"running", "uncertain"} or elapsed is None:
        return max(attempt["reserved_seconds"], elapsed or 0)
    return elapsed

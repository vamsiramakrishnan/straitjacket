"""Multi-worker paired-arm suite orchestrator for AccioBench."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
import sys
from typing import Any

from .browser import BrowserPool
from .config import BenchConfig, RunMetrics
from .loop import run_trial, scored_run_sandbox
from .providers import create_provider

__all__ = ["run_suite"]

_VALID_ARMS = frozenset({"raw", "sj"})
_BASE_SLOT_INDEX = 1

UNSANDBOXED_WARNING = (
    "!" * 88 + "\n"
    "!! WARNING: --allow-unsandboxed: bubblewrap is not active, so this run has NO\n"
    "!! filesystem isolation on either arm. The agent can read the dataset, the grader and\n"
    "!! its private answers. Every result is recorded with sandbox=\"none\" and every report\n"
    "!! states how many trials ran this way. Do not publish these numbers as sandboxed.\n"
    + "!" * 88
)


def _normalize_arms(arms: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw_arm in arms:
        for part in str(raw_arm).split(","):
            arm = part.strip().lower()
            if not arm:
                continue
            if arm not in _VALID_ARMS:
                raise ValueError(
                    f"Unsupported arm {arm!r}. Expected one of {sorted(_VALID_ARMS)}."
                )
            if arm not in cleaned:
                cleaned.append(arm)
    return tuple(cleaned)


def run_suite(
    config: BenchConfig,
    task_ids: list[str],
    *,
    wave_map: dict[str, str] | None = None,
    bucket_map: dict[str, str] | None = None,
    raised: list[RunMetrics] | None = None,
) -> list[RunMetrics]:
    """Execute ``task_ids`` across ``config.arms`` with isolated worker slots and pooled browsers.

    A trial that raises is returned as an in-memory stand-in (``termination="error"``)
    and nothing is written for it; when ``raised`` is given, each stand-in is appended
    to it too, so the caller can tell them from results that are on disk."""
    arms = _normalize_arms(config.arms)
    cleaned_tasks = [t.strip() for t in task_ids if t and t.strip()]
    if not cleaned_tasks or not arms:
        return []
    # Refuses (UnsandboxedRunRefused) before any provider, browser or trial is set up.
    if scored_run_sandbox(config) == "none":
        print(UNSANDBOXED_WARNING, file=sys.stderr, flush=True)

    work_items: list[tuple[int, str, str]] = []
    idx = 0
    for tid in cleaned_tasks:
        for arm in arms:
            work_items.append((idx, tid, arm))
            idx += 1

    config.runs_dir.mkdir(parents=True, exist_ok=True)
    if "sj" in arms:
        config.ctx_state_home.mkdir(parents=True, exist_ok=True)

    provider = create_provider(config)
    browser_pool = BrowserPool(config.chrome_bin)
    results: list[RunMetrics | None] = [None] * len(work_items)

    work_queue: queue.Queue[tuple[int, str, str]] = queue.Queue()
    for item in work_items:
        work_queue.put(item)

    n_workers = max(1, min(int(config.workers), len(work_items)))
    w_lookup: dict[str, str] = wave_map or {}
    b_lookup: dict[str, str] = bucket_map or {}

    def _worker_loop(worker_idx: int) -> None:
        slot = _BASE_SLOT_INDEX + int(config.slot_offset) + worker_idx
        try:
            while True:
                try:
                    item_idx, tid, arm = work_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    metrics = run_trial(
                        config,
                        tid,
                        arm,
                        slot,
                        provider,
                        browser_pool,
                        wave=w_lookup.get(tid, "custom"),
                        bucket=b_lookup.get(tid, "unknown"),
                    )
                    results[item_idx] = metrics
                except Exception as exc:
                    fallback = RunMetrics(
                        task_id=tid,
                        task_rel=tid,
                        category=tid.split("-", 1)[0] if "-" in tid else "unknown",
                        arm=arm,
                        model_id=config.model,
                        wave=w_lookup.get(tid, "custom"),
                        bucket=b_lookup.get(tid, "unknown"),
                        termination="error",
                        notes=[str(exc)],
                    )
                    results[item_idx] = fallback
                    if raised is not None:
                        raised.append(fallback)
                finally:
                    work_queue.task_done()
        finally:
            try:
                browser_pool.close_thread_browser()
            except Exception:
                pass

    try:
        if n_workers == 1:
            _worker_loop(0)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = [executor.submit(_worker_loop, i) for i in range(n_workers)]
                for fut in as_completed(futures):
                    fut.result()
    finally:
        try:
            browser_pool.close_all()
        finally:
            provider.close()

    return [r for r in results if r is not None]

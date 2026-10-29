"""Frozen synthetic ranking experiment; no generated code reaches production."""
from pathlib import Path
from ctx.digest.block_selection import prepare_evidence, features, select_evidence
from evals.alphaevolve.sandbox import run_candidate, INVALID_SCORE, controller_evaluation, candidate_code

TITLE = "Query-aware addressed block ranking"
METRIC_NAME = "retained_answer_blocks"
HERE = Path(__file__).parent
PROBLEM_PATH = HERE / "PROBLEM.md"
INITIAL_PROGRAM_CODE = (HERE / "program.py").read_text()
PROMOTION_REQUIRES_REPLAY = True

# Distinct scenarios, not randomized repetitions of one example. Labels stay
# outside candidate inputs. No holdout feedback is supplied to managed search.
SEARCH_CASES = (
    ("cache", "cache " * 80 + "\n\ncache p95=400ms\n", 50, (1,)),
    ("permissions", "# Settings\npermissions mode=0644\n\nunrelated notes\n", 50, (1,)),
    ("cache timeout", "cache timeout discussion " * 2 + "\n\ncache timeout=30\n\ncache timeout=0 on worker\n", 70, (1, 2)),
    ("database pool", "database pool warming normally\n\ndatabase pool exhausted\n\nrestart pending\n", 28, (1,)),
    ("request auth", "request auth token rejected\n\nrequest auth docs index\n", 30, (0,)),
    ("build failed", "build failed compilation\n\nsee compiler:\n```\nerror: missing symbol\n```\n", 100, (0, 1)),
)
HOLDOUT_CASES = (
    ("queue", "queue " * 90 + "\n\nqueue depth=12\n", 40, (1,)),
    ("retry", "# Runtime\nretry limit=3\n\nstatus ready\n", 40, (1,)),
    ("disk space", "disk space inventory entry " * 2 + "\n\ndisk space=0\n\ndisk space reserve=4GiB\n", 65, (1, 2)),
    ("tls handshake", "tls handshake documentation listing\n\ntls handshake refused\n", 25, (1,)),
    ("migration schema", "migration schema invalid; rollback required\n\nmigration schema OK\n", 48, (0,)),
    ("retry failure", "# Retry\nretry failure on node7\n\nnode7:\n```\nattempts=0\n```\n", 100, (1, 2)),
)
ADVERSARIAL_CASES = (
    ("healthy", "healthy " * 100 + "\n\nhealthy=false\n", 30, (1,)),
    ("token", "# Auth\n```\ntoken = None\n```\n\nnoise\n", 40, (1,)),
    ("DEBUG", "DEBUG enabled and diagnostic output\n\nDEBUG secret missing\n", 30, (1,)),
    ("connection error", "connection error: unrecoverable deadlock\n\nconnection error\n", 44, (0,)),
    ("Region CPU", "Region CPU=100%\n\nAppendix notes: Region CPU throttled\n", 60, (0, 1)),
    ("license", "license not found\n\n```\nlicense = null\n\nretry = false\n```\n", 80, (0, 1)),
)


def _score(code, cases):
    prepared = [prepare_evidence(text) for _, text, _, _ in cases]
    calls = [{"items": features(blocks, case[0])} for blocks, case in zip(prepared, cases)]
    result = run_candidate(code, "rank_blocks", calls)
    if result["error"]:
        return {"score": INVALID_SCORE, "error": result["error"]}
    retained = 0
    rows = {}
    try:
        for index, (blocks, case, order) in enumerate(zip(prepared, cases, result["outputs"])):
            # Gold labels are evaluation-only, not forced into the selector.
            selection = select_evidence(blocks, order, case[2], set())
            recall = len(set(case[3]) & set(selection["kept"])) / len(case[3])
            retained += recall
            rows[str(index)] = {"answer_recall": recall, "bytes": selection["bytes"],
                               "budget": case[2], "kept": selection["kept"]}
    except (ValueError, TypeError, KeyError) as exc:
        return {"score": INVALID_SCORE, "error": str(exc)}
    return {"score": 100 * retained / len(cases), "error": None,
            "cases": rows, "elapsed_ms": result["elapsed_ms"]}


def score_candidate(code):
    return _score(code, SEARCH_CASES)


def score_holdout(code):
    return _score(code, HOLDOUT_CASES)


def score_adversarial(code):
    return _score(code, ADVERSARIAL_CASES)


def evaluation_function(program_candidate):
    result = score_candidate(candidate_code(program_candidate))
    return controller_evaluation(METRIC_NAME, result["score"], result["error"] or "search split only")

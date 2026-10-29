"""Compare fixed baselines and an optional managed winner; never promote code."""
import argparse
import hashlib
import json
from pathlib import Path

from evals.alphaevolve.block_selection import evaluate
from evals.alphaevolve.registry import experiment_fingerprint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    seed = evaluate.INITIAL_PROGRAM_CODE
    programs = {
        'seed': seed,
        'density': seed.replace('-item["relevance"]', '-item["relevance"] / max(1, item["cost"])'),
    }
    if args.candidate:
        programs['managed_candidate'] = args.candidate.read_text()
    results = {}
    for name, code in programs.items():
        results[name] = {
            'source_sha256': hashlib.sha256(code.encode()).hexdigest(),
            'search': evaluate.score_candidate(code),
            'holdout': evaluate.score_holdout(code),
            'adversarial': evaluate.score_adversarial(code),
        }
    report = {'schema': 'ctx.block-selection-benchmark/v1',
              'dataset_fingerprint': experiment_fingerprint('block-selection'),
              'evidence': '18 synthetic cases; no measured agent turns or task success',
              'promotion_ready': False, 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({name: {split: value[split]['score']
                             for split in ('search', 'holdout', 'adversarial')}
                      for name, value in results.items()}, indent=2))


if __name__ == '__main__':
    main()

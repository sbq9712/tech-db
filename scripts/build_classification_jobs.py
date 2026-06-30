#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='data/processed/manifest.json')
    parser.add_argument('--output', default='data/processed/classification-jobs.json')
    parser.add_argument('--limit', type=int, default=38121)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / args.input).read_text(encoding='utf-8'))
    jobs = {
        'created_at': manifest['meta'].get('processed_at'),
        'taxonomy_version': manifest['meta'].get('taxonomy_version'),
        'extractor_version': 'v4-classification-batch',
        'target_unclassified': args.limit,
        'strategy': {
            'phase_1': 'keyword_and_taxonomy_candidates',
            'phase_2': 'small_model_choice_within_candidates',
            'phase_3': 'low_confidence_review_only',
            'cache_key': ['url', 'content_hash', 'taxonomy_version', 'extractor_version'],
        },
        'notes': [
            'Do not classify full text freely.',
            'Prefer candidate-restricted classification.',
            'Keep audit trail for every decision.',
        ],
    }
    (root / args.output).write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(jobs, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

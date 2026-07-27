# Legacy Scripts

Files in this directory are historical references only and must not be used as production entry points.

Production invariants:

- `auto_pipeline.py` is the only supported source-sync pipeline.
- `scripts/build_snapshot.py` is the only supported shard/manifest publisher.
- `data/category-taxonomy.json` is the immutable category taxonomy.
- Run `python3 scripts/validate_data_contract.py` before any data push.
- AI classification, summaries, scoring, tagging and clustering are run only under GLM after explicit user approval.

The legacy files may contain obsolete shard counts, category separators, token handling, or `git add -A`. They are retained only for forensic history.

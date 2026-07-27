#!/bin/bash
# Full pipeline: Stage 1 (classify+tag) -> Stage 2 (params) -> commit -> push
set -e
source /home/rhett/.gh_token_env
cd /home/rhett/tech-db-fresh

echo '============================================'
echo 'TECH-DB AUTO-SYNC PIPELINE'
echo "Started: $(date)"
echo '============================================'

# Stage 1: classify + tag
echo ''
echo '>>> STAGE 1: classify_and_tag.py'
python3 scripts/classify_and_tag.py --resume --workers 6 --batch-size 50
echo "Stage 1 done: $?"

# Stage 2: extract params
echo ''
echo '>>> STAGE 2: extract_params.py'
python3 scripts/extract_params.py --resume --workers 6 --batch-size 50
echo "Stage 2 done: $?"

# Git commit + push (token via env var)
echo ''
echo '>>> GIT COMMIT + PUSH'
git add -A
git commit -m "chore: daily auto-sync with tags + params" || echo "Nothing to commit"
REPO_URL="https://${GH_TOKEN}@github.com/sbq9712/tech-db.git"
git remote set-url origin "$REPO_URL"
git push origin main
git push origin main:gh-pages
git remote set-url origin https://github.com/sbq9712/tech-db.git

echo ''
echo '============================================'
echo "PIPELINE COMPLETE: $(date)"
echo '============================================'

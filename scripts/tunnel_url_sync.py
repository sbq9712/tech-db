#!/usr/bin/env python3
"""Sync the current cloudflared quick-tunnel URL into qa.js + push.
Called by techdb-tunnel.service whenever a new tunnel starts (every WSL boot
gets a new random URL). Steps:
  1. Read current URL from qa.js
  2. If changed: sed-replace, bump cache version in index.html
  3. Validate data contract (fail-closed: no push on broken data)
  4. Commit + push with the GH token from .gh_env
Exit 0 even on push failure (tunnel must stay up); errors are logged.
"""
import re, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

def log(m):
    print(f"[tunnel_sync] {m}", flush=True)

def sh(cmd, timeout=120):
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)

def main():
    new_url = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", new_url):
        log(f"invalid/missing tunnel URL: {new_url!r} — nothing to do")
        return
    qa = (REPO / "qa.js").read_text(encoding="utf-8")
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", qa)
    cur = m.group(0) if m else ""
    if cur == new_url:
        log(f"URL unchanged: {new_url}")
        return
    log(f"URL change: {cur} -> {new_url}")
    qa = qa.replace(cur, new_url) if cur else qa
    (REPO / "qa.js").write_text(qa, encoding="utf-8")
    # cache-bust
    idx_p = REPO / "index.html"
    idx = idx_p.read_text(encoding="utf-8")
    vm = re.search(r"qa\.js\?v=(\d+)", idx)
    if vm:
        idx = idx.replace(f"qa.js?v={vm.group(1)}", f"qa.js?v={int(vm.group(1)) + 1}")
        idx_p.write_text(idx, encoding="utf-8")
        log(f"cache version v{vm.group(1)} -> v{int(vm.group(1)) + 1}")
    # contract (fail closed)
    r = sh([str(REPO / ".venv" / "bin" / "python"), "scripts/validate_data_contract.py"], timeout=300)
    if r.returncode != 0:
        log("contract FAILED — not pushing (tunnel still serves via local URL)")
        return
    sh(["git", "add", "qa.js", "index.html"])
    c = sh(["git", "commit", "-m", f"fix: update tunnel URL — {new_url.split('//')[1].split('.')[0]}"])
    if c.returncode != 0:
        log("nothing to commit")
        return
    # token
    token = ""
    env = REPO / ".gh_env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GH_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if not token:
        log("no GH_TOKEN in .gh_env — commit kept locally, push skipped")
        return
    p = sh(["git", "push", f"https://sbq9712:{token}@github.com/sbq9712/tech-db.git", "main"], timeout=120)
    if p.returncode != 0:
        # CI may have pushed (auto-sync/nightly) since our last pull —
        # rebase the tunnel commit on top and retry once.
        sh(["git", "fetch", "origin", "main"], timeout=120)
        r = sh(["git", "rebase", "origin/main"], timeout=120)
        if r.returncode == 0:
            p = sh(["git", "push", f"https://sbq9712:{token}@github.com/sbq9712/tech-db.git", "main"], timeout=120)
            log("push after rebase: " + ("OK" if p.returncode == 0 else p.stderr.strip()[-150:]))
        else:
            sh(["git", "rebase", "--abort"], timeout=60)
            log("rebase failed — tunnel commit kept locally: " + r.stderr.strip()[-150:])
            return
    else:
        log("push: OK")

if __name__ == "__main__":
    main()

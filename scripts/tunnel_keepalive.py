#!/usr/bin/env python3
"""Run cloudflared quick tunnel as a child process; whenever it (re)connects
with a new URL, sync qa.js + push (scripts/tunnel_url_sync.py). Reconnects if
cloudflared dies. Designed as the main process of techdb-tunnel.service."""
import re, signal, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLOUDFLARED = Path.home() / "bin" / "cloudflared"
LOG = REPO / "runtime" / "cloudflared.log"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

def log(m):
    print(f"[tunnel_keepalive {time.strftime('%H:%M:%S')}] {m}", flush=True)

def sync_url(url):
    try:
        r = subprocess.run([str(REPO / ".venv" / "bin" / "python"),
                            str(REPO / "scripts" / "tunnel_url_sync.py"), url],
                           cwd=REPO, capture_output=True, text=True, timeout=600)
        log("url_sync: " + (r.stdout.strip()[-200:] or r.stderr.strip()[-200:]))
    except Exception as e:
        log(f"url_sync failed: {e}")

def main():
    seen_urls = set()
    while True:
        log("starting cloudflared...")
        with open(LOG, "ab") as lf:
            p = subprocess.Popen(
                [str(CLOUDFLARED), "tunnel", "--url", "http://localhost:8765",
                 "--no-autoupdate"],
                stdout=lf, stderr=subprocess.STDOUT, cwd=REPO)
        deadline = time.time() + 90
        url = None
        pos = LOG.stat().st_size if LOG.exists() else 0
        while time.time() < deadline and p.poll() is None:
            time.sleep(2)
            try:
                tail = LOG.read_bytes()[pos:pos + 200000]
            except OSError:
                continue
            m = URL_RE.findall(tail.decode("utf-8", "replace"))
            if m:
                url = m[-1]
                break
        if url and url not in seen_urls:
            seen_urls.add(url)
            log(f"new tunnel URL: {url}")
            sync_url(url)
        elif not url:
            log("no URL found within 90s")
        try:
            p.wait(timeout=5 * 3600)
        except subprocess.TimeoutExpired:
            log("cloudflared up 5h — restarting for fresh connection")
            p.terminate()
            p.wait(30)
        log(f"cloudflared exited rc={p.returncode}; restarting in 10s")
        time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

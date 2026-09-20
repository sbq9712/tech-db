#!/usr/bin/env python3
"""Run cloudflared quick tunnel as a child process; whenever it (re)connects
with a new URL, sync qa.js + push (scripts/tunnel_url_sync.py). Reconnects if
cloudflared dies. Designed as the main process of techdb-tunnel.service."""
import re, signal, subprocess, sys, time, urllib.request
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

def _healthy(url):
    """Probe the tunnel end-to-end. Only trust 3 consecutive failures (the
    caller's policy) so a flaky local proxy doesn't churn the URL."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/health",
                                     headers={"User-Agent": "tunnel-keepalive/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False

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
        # 2026-09-20: was an unconditional 5h restart — every restart rotates
        # the quick-tunnel URL and Pages (plus browser caches) lag behind,
        # leaving windows where the site points at a dead URL. Now: keep the
        # process until it exits on its own or 3 consecutive health probes
        # (10 min apart) fail — i.e. only restart genuinely dead tunnels.
        fails = 0
        while p.poll() is None:
            time.sleep(600)
            if p.poll() is not None:
                break
            if _healthy(url):
                fails = 0
            else:
                fails += 1
                log(f"health probe failed ({fails}/3)")
                if fails >= 3:
                    log("tunnel unhealthy — restarting cloudflared")
                    p.terminate()
                    try:
                        p.wait(30)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    break
        log(f"cloudflared exited rc={p.returncode}; restarting in 10s")
        time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

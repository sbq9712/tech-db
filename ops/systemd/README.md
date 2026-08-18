# tech-db local services (systemd user units)

These units keep the local stack alive across session close / WSL restart —
the previous nohup/setsid approach died whenever the last WSL session closed
(systemd-logind kills the session scope; see 2026-08-18 incident).

Install (already done on this machine):
    loginctl enable-linger rhett          # user manager survives logout + starts on VM boot
    cp techdb-*.service techdb-*.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now techdb-data-sync.timer techdb-vector.timer \
                                  techdb-server.service techdb-tunnel.service

Units:
- techdb-data-sync.service(+timer) — git pull + lite rebuild + BM25 (every 30 min)
- techdb-vector.service(+timer)     — vector index incremental embed (after sync; server auto-restarts)
- techdb-server.service             — Q&A backend :8765 + portal :8097 (Restart=always)
- techdb-tunnel.service             — cloudflared quick tunnel + qa.js URL auto-sync/push

Boot order: data-sync (fast) → server+tunnel; vector runs async, restarts server when done.

Note: `pkill -f` patterns in ExecStartPre use bracket-escapes (e.g. `server[.]py`)
so pkill never matches its own bash -c command line — a literal pattern kills the
control process and the unit fails with status=15/TERM.

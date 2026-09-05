#!/bin/bash
# Double-click (or run) this on the desktop: starts Alfred and opens his page.
# Everything else -- machines, jobs, approvals -- happens in the browser.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"   # where install.sh puts nats-server
exec python3 run_server.py --config "${1:-configs/desktop.toml}" --open

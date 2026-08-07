#!/bin/bash
# Installing Alfred ONTO Micron OS (or any Linux). This is the owner's own
# deliberate act -- the OS never does it for you.
#   ~/Alfred/install.sh          install the assistant on this machine
#   ~/Alfred/install.sh worker   this machine offers its hands to the household
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="${1:-assistant}"

echo "== Alfred: installing dependencies =="
pip install -r "$HERE/requirements.txt" --break-system-packages

if [ "$ROLE" = "worker" ]; then
  sudo cp "$HERE/alfred-worker.service" /etc/systemd/system/alfred-worker@.service
  sudo systemctl daemon-reload
  sudo systemctl enable "alfred-worker@$USER.service"
  sudo systemctl restart "alfred-worker@$USER.service"
  echo "== This machine now offers its hands to the household. =="
  exit 0
fi

if ! command -v ollama >/dev/null; then
  echo "== Installing Ollama (his engine) =="
  curl -fsSL https://ollama.com/install.sh | sh
fi
echo "== Pulling his mind (qwen2.5:7b, ~5GB; the long part) =="
ollama pull qwen2.5:7b

# If Micron OS runs here, it hosts him: restart moves him in.
if systemctl list-unit-files 2>/dev/null | grep -q "micronos@"; then
  sudo systemctl restart "micronos@$USER.service" || true
  echo "== Alfred is in service. Micron OS will show him in the bar. =="
else
  echo "== Alfred installed. Start him with run_core.py, or install"
  echo "   Micron OS to give him a house. =="
fi

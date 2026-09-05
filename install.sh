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

# The brain hosts the bus other machines join. One static binary, no service
# to manage: run_core.py / run_server.py start and stop it themselves.
if ! command -v nats-server >/dev/null && [ ! -x "$HOME/.local/bin/nats-server" ]; then
  echo "== Installing nats-server (the bus the laptop and other machines join) =="
  NATS_VERSION="v2.10.22"
  case "$(uname -m)" in
    x86_64)  NATS_ARCH=amd64 ;;
    aarch64) NATS_ARCH=arm64 ;;
    armv7l)  NATS_ARCH=arm7 ;;
    *)       echo "   unknown arch $(uname -m); install nats-server by hand"; NATS_ARCH="" ;;
  esac
  if [ -n "$NATS_ARCH" ]; then
    TARBALL="nats-server-$NATS_VERSION-linux-$NATS_ARCH"
    TMP="$(mktemp -d)"
    curl -fsSL "https://github.com/nats-io/nats-server/releases/download/$NATS_VERSION/$TARBALL.tar.gz" \
      | tar -xz -C "$TMP"
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$TMP/$TARBALL/nats-server" "$HOME/.local/bin/nats-server"
    rm -rf "$TMP"
  fi
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

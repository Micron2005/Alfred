#!/usr/bin/env bash
# Micron OS installer — the house goes on the machine; Alfred runs it.
#   ./deploy/install-alfredos.sh core     (desktop: shell server at boot)
#   ./deploy/install-alfredos.sh worker   (any other machine: hands at boot)
#   ./deploy/install-alfredos.sh kiosk    (also: shell opens fullscreen at login)
set -euo pipefail
ROLE="${1:-core}"
ME="$(whoami)"

install_unit () {
  sudo cp "deploy/alfred-$1.service" "/etc/systemd/system/alfred-$1@.service"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "alfred-$1@${ME}.service"
  echo "alfred-$1 running as ${ME}; logs: journalctl -u alfred-$1@${ME} -f"
}

case "$ROLE" in
  core)   install_unit core
          echo "Micron OS shell: http://localhost:8710" ;;
  worker) install_unit worker ;;
  kiosk|device)
    # kiosk: this screen opens the shell at login (desktop itself).
    # device: a household terminal — worker service AND kiosk, with the
    #         shell pointed at the core machine. One Alfred, many doors.
    CORE_HOST="${2:-localhost}"
    if [ "$ROLE" = "device" ]; then
      install_unit worker
    fi
    SHELL_URL="http://${CORE_HOST}:8710/?device=$(hostname -s)"
    mkdir -p ~/.config/autostart
    cat > ~/.config/autostart/micronos-shell.desktop << DESKTOP
[Desktop Entry]
Type=Application
Name=Micron OS Shell
Comment=Open the Micron OS shell fullscreen at login
Exec=sh -c 'sleep 4; chromium --app=${SHELL_URL} --start-fullscreen 2>/dev/null || google-chrome --app=${SHELL_URL} --start-fullscreen 2>/dev/null || firefox --kiosk ${SHELL_URL}'
X-GNOME-Autostart-enabled=true
DESKTOP
    echo "This machine is now a Micron OS terminal -> ${SHELL_URL}"
    echo "Undo kiosk with: rm ~/.config/autostart/micronos-shell.desktop" ;;
  sudo)
    # Scoped, passwordless sudo for exactly the two binaries the os.apply
    # catalog uses. Deliberately NOT "NOPASSWD: ALL" — the catalog plus this
    # scope is the whole blast radius of an approved change.
    echo "${ME} ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/systemctl" \
      | sudo tee /etc/sudoers.d/micronos-scoped >/dev/null
    sudo chmod 440 /etc/sudoers.d/micronos-scoped
    echo "scoped sudo installed for ${ME} (apt-get, systemctl only)"
    echo "remove with: sudo rm /etc/sudoers.d/micronos-scoped" ;;
  harden)
    # The layers that actually carry the weight, per SECURITY.md. None of
    # these can lock the owner out; all are standard Ubuntu hygiene.
    echo "Enabling the firewall (deny incoming, allow outgoing)..."
    sudo apt-get install -y ufw >/dev/null 2>&1 || true
    sudo ufw --force enable
    sudo ufw default deny incoming
    sudo ufw default allow outgoing
    # SSH only if you actually use it to reach this machine:
    # sudo ufw allow ssh
    echo "Enabling automatic security updates..."
    sudo apt-get install -y unattended-upgrades >/dev/null 2>&1 || true
    sudo dpkg-reconfigure -f noninteractive unattended-upgrades || true
    echo
    echo "Done. Alfred already binds to localhost by default, so nothing off"
    echo "this machine can reach him unless you pass --host 0.0.0.0 AND open a"
    echo "port above. The firewall + auto-updates are the real perimeter." ;;
  *) echo "usage: $0 core|worker|kiosk [host]|device <host>|sudo|harden"; exit 1 ;;
esac

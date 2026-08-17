#!/usr/bin/env bash
# =============================================================================
# MotoNav — kiosk install
#
#   sudo bash install_kiosk.sh
#
# Makes MotoNav the only thing on the Pi: starts at power-on, owns the
# screen, no login prompt, no console text, no cursor, no flicker.
#
# Undo with:  sudo bash install_kiosk.sh --uninstall
# =============================================================================
set -euo pipefail

APP_DIR="/opt/motonav"
SERVICE="/etc/systemd/system/motonav.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Bookworm moved the boot config
BOOTCFG="/boot/firmware/config.txt"
BOOTCMD="/boot/firmware/cmdline.txt"
[[ -f "$BOOTCFG" ]] || BOOTCFG="/boot/config.txt"
[[ -f "$BOOTCMD" ]] || BOOTCMD="/boot/cmdline.txt"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo:  sudo bash install_kiosk.sh"
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Removing kiosk mode..."
  systemctl stop motonav 2>/dev/null || true
  systemctl disable motonav 2>/dev/null || true
  rm -f "$SERVICE"
  systemctl daemon-reload
  systemctl enable getty@tty1 2>/dev/null || true
  systemctl start getty@tty1 2>/dev/null || true
  # restore console messages
  sed -i 's/ *logo\.nologo//g; s/ *consoleblank=[0-9]*//g; s/ *vt\.global_cursor_default=[01]//g; s/ *loglevel=[0-9]//g; s/ *quiet//g' "$BOOTCMD" || true
  echo "Kiosk mode removed. The login console is back."
  echo "Reboot to apply:  sudo reboot"
  exit 0
fi

echo "==========================================="
echo "  MotoNav — kiosk install"
echo "==========================================="

# ── 1. dependencies ─────────────────────────────────────────────────────────
echo "[1/6] Dependencies..."
NEED=""
python3 -c "import pygame" 2>/dev/null || NEED="$NEED python3-pygame"
python3 -c "import numpy"  2>/dev/null || NEED="$NEED python3-numpy"
python3 -c "import bluetooth" 2>/dev/null || NEED="$NEED python3-bluez"
if [[ -n "$NEED" ]]; then
  apt-get update -qq
  apt-get install -y $NEED
  echo "      installed:$NEED"
else
  echo "      all present"
fi

# ── 2. install app ──────────────────────────────────────────────────────────
echo "[2/6] Installing to $APP_DIR ..."
mkdir -p "$APP_DIR"
cp "$SRC_DIR/motonav.py" "$APP_DIR/"
chmod +x "$APP_DIR/motonav.py"
# tilestore.py is not optional: without it `import tilestore` fails, the app
# runs with no tile store at all, and the map panel draws an empty plate
for f in tilestore.py pbfimport.py; do
  if [[ -f "$SRC_DIR/$f" ]]; then cp "$SRC_DIR/$f" "$APP_DIR/"; fi
done
if [[ -d "$SRC_DIR/icons" ]]; then cp -r "$SRC_DIR/icons" "$APP_DIR/"; fi
# keep any existing calibration / rotation settings
for f in touch_cal.conf rotation.conf; do
  if [[ -f "$SRC_DIR/$f" && ! -f "$APP_DIR/$f" ]]; then cp "$SRC_DIR/$f" "$APP_DIR/"; fi
done

# ── 3. stop the console fighting for the screen ─────────────────────────────
echo "[3/6] Disabling the login console on tty1..."
systemctl disable getty@tty1.service 2>/dev/null || true
systemctl stop getty@tty1.service 2>/dev/null || true
# some images use a serial console too
systemctl disable serial-getty@ttyS0.service 2>/dev/null || true

# ── 4. quiet the boot text + hide the cursor ────────────────────────────────
echo "[4/6] Quieting boot output..."
if [[ -f "$BOOTCMD" ]]; then
  cp "$BOOTCMD" "${BOOTCMD}.motonav.bak" 2>/dev/null || true
  # strip any previous additions, then append ours (single line file!)
  sed -i 's/ *logo\.nologo//g; s/ *consoleblank=[0-9]*//g; s/ *vt\.global_cursor_default=[01]//g' "$BOOTCMD"
  sed -i '1 s/$/ logo.nologo consoleblank=0 vt.global_cursor_default=0/' "$BOOTCMD"
fi

# rainbow splash off
if [[ -f "$BOOTCFG" ]]; then
  grep -q "^disable_splash=1" "$BOOTCFG" || echo "disable_splash=1" >> "$BOOTCFG"
fi

# ── 5. systemd service ──────────────────────────────────────────────────────
echo "[5/6] Creating service..."
cat > "$SERVICE" <<'EOF'
[Unit]
Description=MotoNav navigation display (kiosk)
After=multi-user.target network.target bluetooth.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/motonav

# let the SPI panel, touch device and network settle
ExecStartPre=/bin/sleep 6
# Blank the console so no text or cursor shows behind us. setterm acts on
# its OWN stdout, which under systemd is the journal — so it has to be
# redirected at /dev/tty0 explicitly or it silently does nothing. (The app
# also does this itself; this just covers the gap before it starts.)
ExecStartPre=-/bin/sh -c '/usr/bin/setterm --cursor off --blank 0 --powerdown 0 > /dev/tty0'
ExecStartPre=-/bin/sh -c 'echo 0 > /sys/class/graphics/fbcon/cursor_blink'
ExecStart=/usr/bin/python3 /opt/motonav/motonav.py

Restart=always
RestartSec=4

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ── 6. enable ───────────────────────────────────────────────────────────────
echo "[6/6] Enabling..."
systemctl daemon-reload
systemctl enable motonav.service

# hide the console cursor now too
echo 0 > /sys/class/graphics/fbcon/cursor_blink 2>/dev/null || true

echo ""
echo "==========================================="
echo "  Kiosk mode installed."
echo ""
echo "  Start now   : sudo systemctl start motonav"
echo "  Logs        : sudo journalctl -u motonav -f"
echo "  Stop        : sudo systemctl stop motonav"
echo "  Update app  : sudo cp motonav.py $APP_DIR/ && sudo systemctl restart motonav"
echo "  Remove all  : sudo bash install_kiosk.sh --uninstall"
echo ""
echo "  SSH still works normally."
echo "  Reboot to test:  sudo reboot"
echo "==========================================="

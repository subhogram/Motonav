MotoNav
=======

A self-contained navigator display for Raspberry Pi OS Lite (console). Receives turn-by-turn navigation and GPS from an Android phone over WiFi (TCP :9999) and renders a cyberpunk HUD to a small SPI/FB panel.

**Features**
- Turn-by-turn guidance with map, chevron, and route highlighting
- Day / Night themes (auto based on sun times)
- Touch input, on-screen keyboard, and in-app calibration
- WiFi discovery and basic network/BT management
- Demo mode for development without a phone
- Streamed offline map tiles: the phone sends a rolling buffer of road
  data (~15 km ahead of the rider, refreshed every ~20 s / 800 m) instead
  of the whole trip up front, so the Pi's buffer never runs dry and
  neither side ever has to move the whole map at once

**Requirements**
- Raspberry Pi running Raspberry Pi OS Lite (console)
- Python 3
- pygame (python3-pygame) for SDL rendering
- Optional: BlueZ/python3-bluez for Bluetooth features

Install dependencies (Debian/Raspbian):

```bash
sudo apt-get update
sudo apt-get install -y python3-pygame
# Optional Bluetooth support
sudo apt-get install -y python3-bluez bluez
```

**Run**
From the project directory run:

```bash
sudo python3 motonav.py           # auto-detect display and run
sudo python3 motonav.py --list    # list detected framebuffers, then exit
sudo python3 motonav.py --demo    # simulated driving without a phone
sudo python3 motonav.py --calibrate # start touch calibration immediately
sudo python3 motonav.py --fb /dev/fb1 # force specific framebuffer device
```

Environment variables (optional):
- `TCP_PORT` — port to receive nav messages (default 9999)
- `SCREEN_W`, `SCREEN_H` — override expected screen resolution (default 480x320)
- `DISCOVERY_PORT` — discovery UDP port (default 9998)

Files of interest
- motonav.py — main application
- region.mnosm — optional offline OSM region file (saved by the app)
- icons/ — directory for UI icon assets

Troubleshooting
- If SDL does not detect the framebuffer, try `--fb /dev/fb1` or run `--list` to inspect devices.
- For touch calibration, use `--calibrate` and follow on-screen prompts; the app stores calibration in touch_cal.conf.

Development
- Use `--demo` to exercise rendering without a phone connection.

License
- MIT-style, see project for details.

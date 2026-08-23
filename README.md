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
  data (~8 km ahead of the rider, refreshed every ~20 s / 800 m) instead
  of the whole trip up front, so the Pi's buffer never runs dry and
  neither side ever has to move the whole map at once
- Lakes, parks and rivers are drawn under the roads, so the map reads as
  somewhere rather than as a bare street grid (see "Land cover" below)
- Tuned for the Pi Zero W: road geometry is simplified when it is stored,
  tiles decode on a background thread, route position is tracked
  incrementally rather than rescanned, and the redraw rate follows what is
  actually happening on screen (see "Performance" below)

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
sudo python3 motonav.py --keep-console # leave the console in text mode (debug)
```

The app takes the Linux virtual terminal into graphics mode while it runs, so
the kernel's framebuffer console stops painting its blinking cursor into the
same memory we draw pixels into — without that, a flashing black block shows
up somewhere on the map. It is handed back on exit, including on SIGTERM, so
stopping the service leaves a usable console. Run with `--keep-console` if you
need console text visible while debugging.

Environment variables (optional):
- `TCP_PORT` — port to receive nav messages (default 9999)
- `SCREEN_W`, `SCREEN_H` — override expected screen resolution (default 480x320)
- `DISCOVERY_PORT` — discovery UDP port (default 9998)

Files of interest
- motonav.py — main application
- region.mnosm — optional offline OSM region file (saved by the app)
- icons/ — directory for UI icon assets

Land cover

Besides roads, tiles carry lakes (class 8), rivers (9) and parks, forest and
grass (10). They are drawn as filled shapes underneath the road network, and
they cost very little: there are only a handful of them in view at any time,
they are simplified harder than roads (6 m rather than 2 m), and anything
under ~25 m across is thrown away when the tile is written because it cannot
be seen on a 480x320 panel anyway.

- **RAM is not the constraint here** — the tile cache holds 12 decoded cells,
  a few MB out of 512. What costs is the single ARMv6 core, so the map spends
  at most `AREA_POINT_BUDGET` (1500) projected points per frame on land cover,
  biggest shapes first. Somewhere pathological — a river delta, a national
  park — drops its smallest shapes instead of dropping the frame rate. This is
  also why buildings are *not* included: same code path, but ten to fifty
  times the shape count in a city.
- **Minimal map mode draws none of it**, staying route-only as before.
- **Land cover comes from OSM ways only.** Lakes and forests mapped as
  multipolygon *relations* are missed. Ordinary closed-way lakes, parks and
  rivers — nearly everything that reads at this size — come through.
- **Tiles you already have will not be refilled.** Gap detection asks whether
  a tile exists, not what is in it, so ground already covered by a roads-only
  import stays roads-only. Use `clear_map` from the phone and let the buffer
  re-stream, or re-run `pbfimport.py`, to pull land cover into it.
- `pbfimport.py --no-areas` restores the old roads-only import.

Performance (Raspberry Pi Zero W)

The Zero W has one 1 GHz ARMv6 core to share between drawing, the phone
link and tile streaming, so the display is built around not wasting it:

- **Install numpy** (`sudo apt install python3-numpy`). It is optional, but
  without it every frame is converted to the panel's pixel format the slow
  way. The app prints a warning at startup if it is missing.
- **Tile geometry is simplified when stored** (2 m tolerance for roads, 6 m
  for land cover), so the draw loop projects far fewer points than OSM ships.
- **Tiles decode on a background thread.** A tile that is not resident yet
  is skipped for a frame or two rather than stalling the UI on disk I/O.
- **Position along the route is tracked incrementally**, and distances come
  from a cumulative table built once per route, so nothing walks the whole
  trip per GPS fix or per frame.
- **Redraw rate follows what is happening**: ~15 fps while a finger is on
  the glass, ~10 fps while riding, ~3 fps parked, and it is clamped further
  if frames turn out to be expensive so drawing can never eat the whole
  core. A frame identical to the last one is not pushed to the panel, though
  the screen is repainted at least every 2 s regardless, so anything that
  draws into the framebuffer behind the app's back gets scrubbed away.
- **The buffer is 8 km ahead / 1.5 km behind** (`BUFFER_AHEAD_KM` in
  motonav.py, mirrored by `Router.BUFFER_AHEAD_KM` in the Android app —
  change both together).

Troubleshooting
- If SDL does not detect the framebuffer, try `--fb /dev/fb1` or run `--list` to inspect devices.
- If touch feels laggy or the UI stutters, check that numpy is installed and
  that `maps/` is not full of tiles imported before simplification existed —
  `clear_map` from the phone and letting the buffer re-stream will shrink them.
- For touch calibration, use `--calibrate` and follow on-screen prompts; the app stores calibration in touch_cal.conf.
- A blinking black block sitting on the map is the kernel console's text
  cursor drawing into the framebuffer underneath the app. It should not
  happen any more, but if it does, the app was almost certainly not run as
  root (it needs to open `/dev/tty0` to quiet the console) — check the
  startup log for a `Console:` line saying so.

Development
- Use `--demo` to exercise rendering without a phone connection. It drives a
  real, turning polyline through a small fabricated street grid on a
  continuous loop — turn markers, ETA, and the past trail all track an
  actual road instead of a wandering arrow on a blank grid — so it also
  doubles as a quick way to show off navigation without a rider, a phone,
  or a network connection.

License
- MIT-style, see project for details.

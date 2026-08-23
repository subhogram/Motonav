# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two halves of one product, in one repo:

- **Pi display** (Python, repo root) — a self-contained navigator HUD for Raspberry Pi OS Lite, drawing straight to a 480x320 SPI framebuffer with pygame/SDL. No X, no browser.
- **Android app** ([android/](android/), Java) — the rider's phone. Resolves a destination, routes it, and feeds the Pi position + map data over Wi-Fi or Bluetooth.

The Pi does the navigating; the phone is a data source. Once a route and steps are delivered, the Pi computes turn guidance itself from GPS fixes (`STEP_MODE`) rather than mirroring the phone's screen.

## Commands

### Pi app (must run as root — needs `/dev/fb*` and `/dev/tty0`)

```bash
sudo python3 motonav.py              # auto-detect display and run
sudo python3 motonav.py --demo       # simulated ride, no phone needed
sudo python3 motonav.py --list       # list detected framebuffers, exit
sudo python3 motonav.py --fb /dev/fb1
sudo python3 motonav.py --calibrate
sudo python3 motonav.py --keep-console   # leave console in text mode (debug)
```

Deps: `python3-pygame`, `python3-numpy` (optional but a large perf win — the app warns at startup if missing), `python3-bluez` (optional, for BT).

Env overrides: `TCP_PORT` (9999), `DISCOVERY_PORT` (9998), `RFCOMM_CHANNEL` (22), `SCREEN_W`/`SCREEN_H` (480x320).

### Offline map import

```bash
python3 pbfimport.py karnataka.osm.pbf              # -> ./maps/ tiles
python3 pbfimport.py x.pbf --highways major|normal|full
python3 pbfimport.py x.pbf --no-areas               # roads only
python3 pbfimport.py x.pbf --bbox S,W,N,E
```

Needs `pip3 install protobuf`. One-time, slow on a Zero W.

### Android

There is **no Gradle wrapper checked in** — build from Android Studio (Build > Build APK(s)), or run `gradle` against [android/](android/) with a local install. `local.properties` (SDK path) is gitignored and must exist locally.

### Kiosk deploy

`sudo bash install_kiosk.sh` installs to `/opt/motonav`, adds a systemd unit, and takes over tty1. `--uninstall` reverses it.

### Tests

There is no test suite. `--demo` is the verification path: it drives a real turning polyline through a fabricated street grid on a loop, so turn markers, ETA and the trail all exercise real code.

## Architecture

### The wire protocol is the contract

Both sides speak **newline-delimited JSON** over either transport (TCP :9999, or Bluetooth RFCOMM channel 22). [motonav.py](motonav.py) `handle_message()` is the single authoritative list of message types — read it first when touching anything cross-device. Phone→Pi: `route`, `steps`, `pos`, `time`, `theme`, `mapmode`, `region_begin`/`region_chunk`/`region_end`, `gaps_req`, `metrics_req`, `clear_map`, `end_nav`, `nav_stop`, `nav_idle`, `ping`. Pi→phone (via `reply()`): `map_gaps`, metrics.

Coordinates are **fixed-point integers**, and the scale differs by message: route/step points are `1e5`, tile files are `1e6`. Getting this wrong yields plausible-looking positions off by ~10x.

The phone finds the Pi by UDP broadcast on :9998 (`DISCOVERY_MAGIC`), so no typed IP is needed.

### Tiles are the shared format

[tilestore.py](tilestore.py) defines the `.mnt` tile format, and [TileCache.java](android/app/src/main/java/com/motonav/TileCache.java) reimplements it on the phone. Both use the same 0.05° grid so the phone can send exactly the cells the Pi lacks. **Any format change must land in both.** The class byte is the extension point: 1–7 are roads, 8/9/10 are water/waterway/green, and older builds silently drop classes they don't know — so adding a class is backward-compatible, changing the header is not.

Tiles are append-only. A second download over the same cell appends rather than rewrites, which is why gap-filling never re-sends known ground — and also why **coverage checks ask "does this tile exist", not "what's in it"**. Ground imported roads-only stays roads-only until `clear_map` or a re-import.

### Map streaming (the rolling buffer)

Not a whole-route download. The Pi keeps a window around the rider — `BUFFER_AHEAD_KM = 8.0`, `BUFFER_BEHIND_KM = 1.5` — and re-checks coverage as they advance (`maybe_stream_buffer` → `report_map_gaps`, throttled by `GAP_CHECK_MIN_S`/`GAP_CHECK_MOVE_M`). Missing tiles are merged into a few large bounding boxes and sent as `map_gaps`; [GapFiller.java](android/app/src/main/java/com/motonav/GapFiller.java) fetches exactly those.

`BUFFER_AHEAD_KM` exists **twice** — [motonav.py:2555](motonav.py#L2555) and [Router.java:41](android/app/src/main/java/com/motonav/Router.java#L41). Change both together; the comments in each say so.

### Routing chain (phone)

`destination link → LinkResolver → coords → OSRM → geometry + steps → Overpass → 3 km corridor`. All free, keyless OSM services. [Router.java](android/app/src/main/java/com/motonav/Router.java) orchestrates; [Progress.java](android/app/src/main/java/com/motonav/Progress.java) is the single source of truth for what the UI reports.

[Link.java](android/app/src/main/java/com/motonav/Link.java) owns both transports and routes messages to whichever is up (`auto`/`wifi`/`bt`). The Pi listens on both simultaneously and tracks `active_transport` from whichever last delivered a message.

### Pi Zero W performance constraints

One 1 GHz ARMv6 core shared between drawing, the phone link and tile streaming. RAM is not the binding constraint; CPU is. Existing code is shaped around this and changes should respect it:

- Geometry is simplified (Douglas–Peucker) **when written**, not when drawn — 2 m for roads, 6 m for land cover. Sub-25 m rings are discarded at write time.
- Tiles decode on a background thread. `ways_near()` never blocks a frame on disk I/O — it skips non-resident tiles and prefetches them. Its answer is memoised until the viewport moves or `_gen` bumps.
- Land cover draws under roads with a hard `AREA_POINT_BUDGET` (1500 projected points/frame), biggest shapes first. Buildings are deliberately excluded — same code path, 10–50x the shape count.
- Route position is tracked **incrementally** with a cumulative-distance table built once per route. Nothing may walk the whole route per fix or per frame.
- Redraw rate is adaptive: ~15 fps on touch, ~10 riding, ~3 parked, clamped further if frames prove expensive. Identical frames aren't pushed, but a repaint is forced every 2 s so anything drawing into the framebuffer behind the app gets scrubbed.

### Display ownership

The app puts the Linux VT into graphics mode (`ConsoleGuard`) so the kernel console stops painting its cursor into the same memory. It's restored on exit including SIGTERM. This requires root; without it a blinking black block appears over the map. `FBWriter` handles the pixel-format conversion to the panel (numpy fast path).

## Conventions

`motonav.py` is a single ~4300-line module organised by `# ── section ──` banner comments rather than split into packages — that's deliberate for deployment simplicity on the Pi. Follow the existing banner structure when adding code.

Shared state lives in module-level dicts (`nav`, `route_pts`, `route_meta`, `trail`) each guarded by its own lock (`nav_lock`, `route_lock`, `trail_lock`, …). Network handlers, the touch thread and the render loop all touch these concurrently — take the right lock, and don't hold one across I/O.

Comments here explain *why*, often at length, and frequently record a constraint that isn't visible locally (a matching constant on the other side, a Zero W budget, a format-compat rule). Preserve them; match that register when adding new ones.

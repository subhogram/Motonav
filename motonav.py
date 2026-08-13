#!/usr/bin/env python3
"""
MotoNav — self-contained navigator display for Raspberry Pi OS Lite (console)

Receives turn-by-turn + GPS from the Android app over WiFi (TCP :9999) and
renders a cyberpunk HUD with day/night themes to the 3.5" panel.

DEPENDENCIES (install once):
    sudo apt-get update
    sudo apt-get install -y python3-pygame

RUN:
    sudo python3 motonav.py            # auto-detect display
    sudo python3 motonav.py --list     # show detected framebuffers, then exit
    sudo python3 motonav.py --demo     # simulated driving, no phone needed
    sudo python3 motonav.py --calibrate  # touch calibration straight away
    sudo python3 motonav.py --fb /dev/fb1   # force a specific framebuffer

Tap the mode chip (top-right) to cycle AUTO / DAY / NIGHT themes.
Press ESC or Ctrl+C to quit.
"""

import os
import sys
import json
import math
import time
import socket
import re
import base64
try:
    import bluetooth          # python3-bluez (optional)
except ImportError:
    bluetooth = None
import threading
import datetime as dt

# ── Parse args BEFORE importing pygame (SDL env must be set first) ────────────
ARGS = sys.argv[1:]
DEMO = "--demo" in ARGS
LIST_ONLY = "--list" in ARGS
CALIBRATE = "--calibrate" in ARGS

FORCE_FB = None
if "--fb" in ARGS:
    i = ARGS.index("--fb")
    if i + 1 < len(ARGS):
        FORCE_FB = ARGS[i + 1]


def detect_framebuffers():
    """Return a list of (device, name) for each framebuffer present."""
    out = []
    try:
        with open("/proc/fb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    idx, name = parts
                    out.append((f"/dev/fb{idx}", name))
    except Exception:
        pass
    return out


def pick_framebuffer():
    """Prefer an SPI panel (ili9486 etc.) over the generic BCM/HDMI one."""
    if FORCE_FB:
        return FORCE_FB
    fbs = detect_framebuffers()
    if not fbs:
        return "/dev/fb0"
    # Prefer a device whose driver name looks like an SPI/TFT panel
    for dev, name in fbs:
        low = name.lower()
        if any(k in low for k in ("ili", "st77", "hx8", "tft", "spi", "fb_")):
            return dev
    # Otherwise if more than one exists, fb1 is usually the add-on panel
    if len(fbs) > 1:
        return fbs[1][0]
    return fbs[0][0]


FB_DEV = pick_framebuffer()

if LIST_ONLY:
    print("Detected framebuffers:")
    for dev, name in detect_framebuffers() or [("(none)", "")]:
        print(f"  {dev}   {name}")
    print(f"\nWould use: {FB_DEV}")
    sys.exit(0)

# ── SDL configuration for console (no X) ──────────────────────────────────────
os.environ.setdefault("SDL_FBDEV", FB_DEV)
os.environ.setdefault("SDL_NOMOUSE", "0")      # we want touch events
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
# Touch input: SDL2 reads /dev/input/event* via evdev
os.environ.setdefault("SDL_MOUSEDRV", "TSLIB")

import pygame  # noqa: E402
import mmap, fcntl, struct
try:
    import numpy as _np
except ImportError:
    _np = None


class FBWriter:
    """
    Writes pygame Surfaces straight to a Linux framebuffer via mmap.
    Needed because SDL2 on Bookworm has no fbcon driver.
    Auto-detects resolution / bpp / line length from the device.
    """

    FBIOGET_VSCREENINFO = 0x4600
    FBIOGET_FSCREENINFO = 0x4602

    def __init__(self, dev):
        self.dev = dev
        self.fd = os.open(dev, os.O_RDWR)
        vinfo = fcntl.ioctl(self.fd, self.FBIOGET_VSCREENINFO, b"\x00" * 160)
        self.w, self.h = struct.unpack("II", vinfo[0:8])
        self.bpp = struct.unpack("I", vinfo[24:28])[0]
        finfo = fcntl.ioctl(self.fd, self.FBIOGET_FSCREENINFO, b"\x00" * 80)
        line = struct.unpack("I", finfo[48:52])[0]
        self.stride = line if line else self.w * (self.bpp // 8)
        self.size = self.stride * self.h
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
        print(f"Framebuffer {dev}: {self.w}x{self.h} {self.bpp}bpp stride={self.stride}")

    def blit(self, surface):
        with rot_lock:
            r = screen_rotation
        if r:
            # pygame rotates counter-clockwise for positive angles
            surface = pygame.transform.rotate(surface, -r)
        if surface.get_size() != (self.w, self.h):
            sw, sh = surface.get_size()
            # preserve aspect ratio; letterbox rather than stretch
            k = min(self.w / sw, self.h / sh)
            nw, nh = max(1, int(sw * k)), max(1, int(sh * k))
            scaled = pygame.transform.smoothscale(surface, (nw, nh))
            canvas = pygame.Surface((self.w, self.h))
            canvas.fill(C.get("BG", (0, 0, 0)))
            canvas.blit(scaled, ((self.w - nw) // 2, (self.h - nh) // 2))
            surface = canvas
        if self.bpp == 16:
            data = self._to565(surface)
        else:
            # 32bpp: framebuffer wants BGRX
            raw = pygame.image.tostring(surface, "RGBX")
            b = bytearray(raw)
            b[0::4], b[2::4] = bytes(b[2::4]), bytes(b[0::4])
            data = bytes(b)
        self.mm.seek(0)
        self.mm.write(data)

    def _to565(self, surface):
        """Convert a surface to RGB565 bytes (vectorised when numpy present)."""
        if _np is not None:
            arr = pygame.surfarray.pixels3d(surface)          # (w,h,3)
            arr = _np.transpose(arr, (1, 0, 2))               # -> (h,w,3)
            r = arr[:, :, 0].astype(_np.uint16)
            g = arr[:, :, 1].astype(_np.uint16)
            b = arr[:, :, 2].astype(_np.uint16)
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            return v.astype("<u2").tobytes()
        # slow fallback
        raw = pygame.image.tostring(surface, "RGB")
        out = bytearray(self.w * self.h * 2)
        for i in range(self.w * self.h):
            r = raw[i*3]; g = raw[i*3+1]; b = raw[i*3+2]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i*2] = v & 0xFF; out[i*2+1] = (v >> 8) & 0xFF
        return bytes(out)

    def close(self):
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


# ── Config ────────────────────────────────────────────────────────────────────
TCP_PORT = int(os.environ.get("TCP_PORT", "9999"))
SCREEN_W = int(os.environ.get("SCREEN_W", "480"))
SCREEN_H = int(os.environ.get("SCREEN_H", "320"))
FPS = 12

# ── Themes ────────────────────────────────────────────────────────────────────
THEMES = {
    # GTA-style daytime map: pale parchment land, white roads, cyan route
    "day": {
        "BG": (233, 231, 223), "PANEL": (245, 243, 236), "PANEL2": (245, 243, 236),
        "BORDER": (196, 194, 183), "GRID": (214, 212, 202),
        "ACCENT": (59, 169, 224), "GREEN": (74, 158, 63), "WARN": (222, 138, 20),
        "RED": (196, 60, 50), "TEXT": (34, 33, 29), "MUTED": (124, 122, 112),
        "GHOST": (150, 200, 230), "MAPBG": (216, 214, 203),
        "ROUTE": (59, 169, 224), "ROUTEGLOW": (150, 208, 238),
        "ROADCASE": (184, 182, 171), "PLAYER": (255, 255, 255),
        "PLAYEREDGE": (43, 43, 43), "MAPEDGE": (43, 43, 43),
        "PAST": (169, 167, 156), "PASTCASE": (196, 194, 183),
        "ROADFILL": (255, 255, 255),
        "ROUTEFAR": (96, 176, 224),
        "GHOSTROAD": (196, 194, 184),
    },
    # GTA night map: dark slate land, cool grey roads, same cyan route
    "night": {
        "BG": (24, 27, 33), "PANEL": (32, 36, 44), "PANEL2": (32, 36, 44),
        "BORDER": (58, 64, 76), "GRID": (44, 50, 60),
        "ACCENT": (72, 190, 245), "GREEN": (94, 200, 90), "WARN": (240, 165, 40),
        "RED": (226, 80, 70), "TEXT": (236, 240, 246), "MUTED": (128, 140, 158),
        "GHOST": (30, 90, 130), "MAPBG": (38, 43, 52),
        "ROUTE": (72, 190, 245), "ROUTEGLOW": (36, 96, 132),
        "ROADCASE": (54, 61, 73), "PLAYER": (255, 255, 255),
        "PLAYEREDGE": (18, 20, 25), "MAPEDGE": (12, 14, 18),
        "PAST": (86, 96, 112), "PASTCASE": (58, 66, 80),
        "ROADFILL": (78, 88, 104),
        "ROUTEFAR": (58, 140, 190),
        "GHOSTROAD": (58, 64, 76),
    },
}

theme_mode = "day"           # auto | day | night
theme_lock = threading.Lock()
C = dict(THEMES["night"])    # active palette
MODE_CHIP = None             # (x, y, w, h) tap target

# ── Shared state ──────────────────────────────────────────────────────────────
active_transport = None       # "WIFI" | "BT" | None
transport_lock = threading.Lock()

NAV_STALE_SEC = 20        # no nav update this long -> assume navigation ended
last_nav_ms = 0.0

# Map pan / zoom (touch exploration of the route)
map_pan = {"dx_m": 0.0, "dy_m": 0.0, "zoom": 1.0, "touched_ms": 0.0}
MAP_RECENTER_SEC = 12          # auto snap back after this long untouched
MAP_RECT = None                # last drawn map rect (for hit testing)
MAP_BTN_ZIN = None
MAP_BTN_ZOUT = None
MAP_BTN_RECENTER = None
pan_lock = threading.Lock()


def map_is_panned():
    with pan_lock:
        return (abs(map_pan["dx_m"]) > 1 or abs(map_pan["dy_m"]) > 1
                or abs(map_pan["zoom"] - 1.0) > 0.01)


def map_recenter():
    with pan_lock:
        map_pan["dx_m"] = 0.0
        map_pan["dy_m"] = 0.0
        map_pan["zoom"] = 1.0
        map_pan["touched_ms"] = 0.0


def map_zoom(factor):
    with pan_lock:
        map_pan["zoom"] = max(0.25, min(6.0, map_pan["zoom"] * factor))
        map_pan["touched_ms"] = time.time()


def map_drag(ddx_px, ddy_px, mpp):
    with pan_lock:
        map_pan["dx_m"] -= ddx_px * mpp
        map_pan["dy_m"] += ddy_px * mpp
        map_pan["touched_ms"] = time.time()


def map_pan_watchdog():
    """Snap back to the rider after a period with no touches."""
    while True:
        time.sleep(1)
        with pan_lock:
            t = map_pan["touched_ms"]
        if t and (time.time() - t) > MAP_RECENTER_SEC:
            map_recenter()


# Full trip route (list of (lat, lon)) sent by the phone
route_pts = []
route_steps = []          # [{lat, lon, m, t, r}] maneuvers along the route
route_meta = {"dest": "", "km": 0.0, "sec": 0.0}
route_lock = threading.Lock()
STEP_MODE = False         # True once the phone sends steps (standalone nav)

# "minimal"  = Google mode: route only, everything else greyed right back
# "detailed" = OSM mode: full street map from the downloaded region
map_detail = "detailed"

# Clock taken from the phone (the Pi has no RTC)
phone_time = {"epoch": 0.0, "tz": None, "at": 0.0}
time_lock = threading.Lock()


def now_dt():
    """Local time, using the phone's clock when we have it."""
    with time_lock:
        ep, tz, at = phone_time["epoch"], phone_time["tz"], phone_time["at"]
    if ep and at:
        drift = time.time() - at
        # timezone-aware UTC, then shift to the phone's offset
        utc = dt.datetime.fromtimestamp(ep + drift, dt.timezone.utc)
        if tz is not None:
            return (utc + dt.timedelta(seconds=tz)).replace(tzinfo=None)
        return utc.replace(tzinfo=None)
    return dt.datetime.now()


def have_phone_time():
    with time_lock:
        return bool(phone_time["epoch"])

nav = {
    "instruction": "", "road": "", "distance_m": None, "distance_raw": "",
    "maneuver": "", "duration_s": None, "eta_seconds": None,
    "arrived": False, "connected": False,
    "lat": None, "lon": None, "bearing": None, "speed_kph": None,
    "region_msg": "",
}
nav_lock = threading.Lock()

trail = []
TRAIL_MAX = 400
trail_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  SUN TIMES (for auto day/night) — pure math, no network
# ══════════════════════════════════════════════════════════════════════════════
def _julian_day(d):
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    return d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def sun_times(lat, lon, when=None):
    """Return (sunrise_min, sunset_min) as minutes since local midnight."""
    if lat is None or lon is None:
        return None, None
    when = when or now_dt()
    try:
        n = _julian_day(when) - 2451545.0 + 0.0008 - lon / 360.0
        M = (357.5291 + 0.98560028 * n) % 360
        Mr = math.radians(M)
        Cc = 1.9148 * math.sin(Mr) + 0.02 * math.sin(2 * Mr) + 0.0003 * math.sin(3 * Mr)
        L = (M + Cc + 180 + 102.9372) % 360
        Lr = math.radians(L)
        Jt = 2451545.0 + n + 0.0053 * math.sin(Mr) - 0.0069 * math.sin(2 * Lr)
        decl = math.asin(math.sin(Lr) * math.sin(math.radians(23.44)))
        latr = math.radians(lat)
        cosH = ((math.sin(math.radians(-0.833)) - math.sin(latr) * math.sin(decl))
                / (math.cos(latr) * math.cos(decl)))
        if not -1 <= cosH <= 1:
            return None, None
        H = math.degrees(math.acos(cosH))
        tz_off = -(time.altzone if time.daylight else time.timezone) / 3600.0

        def to_local_min(j):
            return (((j + 0.5) % 1.0) * 1440.0 + tz_off * 60.0) % 1440.0

        return to_local_min(Jt - H / 360.0), to_local_min(Jt + H / 360.0)
    except Exception:
        return None, None


def resolve_theme(d):
    with theme_lock:
        mode = theme_mode
    if mode in ("day", "night"):
        return mode
    sr, ss = sun_times(d.get("lat"), d.get("lon"))
    now = now_dt()
    now_min = now.hour * 60 + now.minute
    if sr is None:
        return "day" if 390 <= now_min <= 1110 else "night"   # 06:30–18:30
    return "day" if sr <= now_min <= ss else "night"


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTING
# ══════════════════════════════════════════════════════════════════════════════
def fmt_distance(m):
    if m is None:
        return "--", ""
    return (f"{m/1000:.1f}", "KM") if m >= 1000 else (str(int(round(m))), "M")


def fmt_duration(s):
    if s is None:
        return "--"
    mn = int(round(s / 60))
    return f"{mn} MIN" if mn < 60 else f"{mn//60}H {mn%60:02d}M"


def fmt_eta(s):
    if s is None:
        return "--:--"
    return (now_dt() + dt.timedelta(seconds=s)).strftime("%H:%M")


def accent_for(d):
    if d["arrived"]:
        return C["GREEN"]
    m = (d["maneuver"] or "").lower()
    if "off" in m:
        return C["RED"]
    if "u-turn" in m or "sharp" in m:
        return C["WARN"]
    return C["ROUTE"]


def moved_enough(a, b, min_m=2.0):
    dlat = (b[0] - a[0]) * 111320.0
    dlon = (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0]))
    return dlat * dlat + dlon * dlon >= min_m * min_m


# ══════════════════════════════════════════════════════════════════════════════
#  DRAWING
# ══════════════════════════════════════════════════════════════════════════════
def text(surf, s, font, col, x, y, align="left", max_w=None):
    s = str(s)
    if max_w:
        while font.size(s)[0] > max_w and len(s) > 2:
            s = s[:-2] + "…"
    img = font.render(s, True, col)
    r = img.get_rect()
    if align == "center":
        r.centerx = x
    elif align == "right":
        r.right = x
    else:
        r.left = x
    r.top = y
    surf.blit(img, r)
    return r


def draw_icon(surf, key, cx, cy, size, col):
    """Flat neon turn arrows — bezier-smoothed, no glow."""
    r = size * 0.42
    lw = max(3, int(size / 13))

    def P(rx, ry):
        return (int(cx + rx * r), int(cy + ry * r))

    def line(a, b):
        pygame.draw.line(surf, col, P(*a), P(*b), lw)

    def bez(p0, p1, p2, p3, steps=24):
        pts = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1 - t
            x = mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0]
            y = mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]
            pts.append((int(x), int(y)))
        return pts

    def curve(a, b, c_, d):
        pts = bez(P(*a), P(*b), P(*c_), P(*d))
        if len(pts) > 1:
            pygame.draw.lines(surf, col, False, pts, lw)

    def head(tip, prev, sz=None):
        sz = sz or lw * 2.8
        tx, ty = P(*tip)
        fx, fy = P(*prev)
        dx, dy = tx - fx, ty - fy
        ln = math.hypot(dx, dy) or 1
        nx, ny = dx / ln, dy / ln
        pygame.draw.polygon(surf, col, [
            (tx, ty),
            (tx - nx*sz - ny*sz*0.8, ty - ny*sz + nx*sz*0.8),
            (tx - nx*sz*0.4, ty - ny*sz*0.4),
            (tx - nx*sz + ny*sz*0.8, ty - ny*sz - nx*sz*0.8),
        ])

    m = (key or "").lower()
    if m in ("", "straight"):
        line([0, .85], [0, -.42]); head([0, -.58], [0, -.1], lw*3.2)
    elif m == "turn-left":
        curve([.3, .85], [.3, -.1], [.3, -.45], [-.58, -.45]); head([-.7, -.45], [-.3, -.45], lw*3)
    elif m == "turn-right":
        curve([-.3, .85], [-.3, -.1], [-.3, -.45], [.58, -.45]); head([.7, -.45], [.3, -.45], lw*3)
    elif m == "slight-left":
        curve([.18, .85], [.18, .25], [-.18, -.22], [-.55, -.7]); head([-.66, -.8], [-.4, -.56], lw*2.8)
    elif m == "slight-right":
        curve([-.18, .85], [-.18, .25], [.18, -.22], [.55, -.7]); head([.66, -.8], [.4, -.56], lw*2.8)
    elif m == "sharp-left":
        curve([.32, .85], [.32, 0], [.32, -.7], [-.55, -.7])
        curve([-.55, -.7], [-.72, -.7], [-.72, -.55], [-.72, .1]); head([-.72, .22], [-.72, -.2], lw*2.8)
    elif m == "sharp-right":
        curve([-.32, .85], [-.32, 0], [-.32, -.7], [.55, -.7])
        curve([.55, -.7], [.72, -.7], [.72, -.55], [.72, .1]); head([.72, .22], [.72, -.2], lw*2.8)
    elif "u-turn" in m:
        if "right" in m:
            curve([-.38, .85], [-.38, 0], [-.38, -.85], [.38, -.85])
            curve([.38, -.85], [.38, -.5], [.38, -.2], [.38, .6]); head([.38, .72], [.38, .3], lw*2.8)
        else:
            curve([.38, .85], [.38, 0], [.38, -.85], [-.38, -.85])
            curve([-.38, -.85], [-.38, -.5], [-.38, -.2], [-.38, .6]); head([-.38, .72], [-.38, .3], lw*2.8)
    elif "roundabout" in m or "rotary" in m:
        rad = int(r * .5)
        pygame.draw.circle(surf, col, (cx, int(cy - r*.05)), rad, lw)
        pygame.draw.line(surf, col, (cx, int(cy + r*.85)), (cx, int(cy - r*.05) + rad), lw)
        head([.42, -.52], [.22, -.34], lw*2.6)
    elif "destination" in m or "arriv" in m:
        pr = int(r * .58)
        pcy = int(cy - r*.18)
        pygame.draw.circle(surf, col, (cx, pcy), pr, lw)
        pygame.draw.lines(surf, col, False, [
            (int(cx - pr*.4), pcy), (int(cx - pr*.07), int(pcy + pr*.38)),
            (int(cx + pr*.5), int(pcy - pr*.4))], lw)
        pygame.draw.line(surf, col, (cx, pcy + pr), P(0, .85), lw)
    else:
        line([0, .85], [0, -.42]); head([0, -.58], [0, -.1], lw*3.2)



def _draw_turn_marker(surf, fonts, d, pt, rect):
    if not pt:
        return
    rx, ry, rw, rh = rect
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    tx_, ty_ = pt
    if rx + 6 < tx_ < rx + rw - 6 and ry + 6 < ty_ < ry + rh - 6:
        pygame.draw.circle(surf, C["PLAYER"], (int(tx_), int(ty_)), 7)
        pygame.draw.circle(surf, C["ROUTE"], (int(tx_), int(ty_)), 7, 3)
        dv_, du_ = fmt_distance(d.get("distance_m"))
        text(surf, f"{dv_}{du_.lower()}", f_lbl, C["TEXT"], int(tx_) + 11, int(ty_) - 14)


def draw_map(surf, fonts, d, rect):
    """GTA-style flat top-down minimap: parchment land, glowing cyan route,
    white chevron player marker, dark rounded border."""
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    rx, ry, rw, rh = rect

    # Map plate with rounded corners + dark GTA border
    pygame.draw.rect(surf, C["MAPBG"], rect, border_radius=6)

    global MAP_RECT
    MAP_RECT = rect

    with pan_lock:
        pan_dx, pan_dy, zoom = map_pan["dx_m"], map_pan["dy_m"], map_pan["zoom"]
    panned = (abs(pan_dx) > 1 or abs(pan_dy) > 1 or abs(zoom - 1.0) > 0.01)

    cx = rx + rw // 2
    # when exploring, centre the view; when riding, sit low in the frame
    cy = ry + (rh // 2 if panned else int(rh * 0.70))
    lat, lon, brg = d.get("lat"), d.get("lon"), d.get("bearing")
    MPP = 1.3 / max(0.25, zoom)
    MDEG = 111320.0

    clip = surf.get_clip()
    surf.set_clip(rect)

    have_map = tiles is not None and lat is not None and tiles.covers(lat, lon)

    if not have_map and not (map_detail == "minimal"):
        # fallback: faint block-grid
        sp = 34
        ox = int((lon * 100000) % sp) if lon else 0
        oy = int((lat * 100000) % sp) if lat else 0
        for gx in range(rx - ox, rx + rw + sp, sp):
            pygame.draw.line(surf, C["GRID"], (gx, ry), (gx, ry + rh), 1)
        for gy in range(ry - oy, ry + rh + sp, sp):
            pygame.draw.line(surf, C["GRID"], (rx, gy), (rx + rw, gy), 1)

    def to_screen(plat, plon):
        dlat = (plat - lat) * MDEG
        dlon = (plon - lon) * MDEG * math.cos(math.radians(lat))
        ex, ny = dlon, dlat
        # heading-up only while following; north-up while exploring
        if brg is not None and not panned:
            a = math.radians(brg)
            ex, ny = ex*math.cos(a) - ny*math.sin(a), ex*math.sin(a) + ny*math.cos(a)
        ex -= pan_dx
        ny -= pan_dy
        return (int(cx + ex / MPP), int(cy - ny / MPP))

    minimal = (map_detail == "minimal")

    # ── real OSM roads ──
    if have_map and not minimal:
        span_m = max(rw, rh) * MPP * 0.75
        try:
            near = tiles.ways_near(lat, lon, radius_m=span_m)
            # casing pass then fill pass so junctions look right
            for style_pass in (0, 1):
                for cls, pts in near:
                    w_fill, w_case, major = tilestore.CLASS_STYLE.get(cls, (2, 3, False))
                    if style_pass == 0:
                        col, wdt = C["ROADCASE"], w_case
                    else:
                        col, wdt = C["ROADFILL"], w_fill
                    scr = [to_screen(p[0], p[1]) for p in pts]
                    # cheap cull: skip ways entirely off-panel
                    if all(x < rx - 40 or x > rx + rw + 40 or
                           y < ry - 40 or y > ry + rh + 40 for x, y in scr):
                        continue
                    if len(scr) >= 2:
                        pygame.draw.lines(surf, col, False, scr, max(1, int(wdt)))
        except Exception:
            pass

    # ── minimal mode: only ghost the surrounding roads so junctions read ──
    if have_map and minimal and lat is not None:
        try:
            for cls, wpts in tiles.ways_near(lat, lon, radius_m=max(rw, rh) * MPP * 0.6):
                if cls > 5:          # skip residential clutter entirely
                    continue
                scr = [to_screen(p[0], p[1]) for p in wpts]
                if all(x < rx - 30 or x > rx + rw + 30 or
                       y < ry - 30 or y > ry + rh + 30 for x, y in scr):
                    continue
                if len(scr) >= 2:
                    pygame.draw.lines(surf, C["GHOSTROAD"], False, scr, 2)
        except Exception:
            pass

    # ── FULL ROUTE (whole trip, drawn first so the rest sits on top) ──
    if lat is not None:
        with route_lock:
            rpts = list(route_pts)
        if len(rpts) >= 2:
            # cheap culling: keep points within a generous box of the view
            span_m = max(rw, rh) * MPP * 1.8
            dlat = span_m / 111320.0
            dlon = span_m / max(1.0, 111320.0 * math.cos(math.radians(lat)))
            vis, run = [], []
            for (pla, plo) in rpts:
                if abs(pla - lat) <= dlat and abs(plo - lon) <= dlon:
                    run.append(to_screen(pla, plo))
                else:
                    if len(run) >= 2:
                        vis.append(run)
                    run = []
            if len(run) >= 2:
                vis.append(run)
            for seg in vis:
                try:
                    if minimal:
                        pygame.draw.lines(surf, C["ROUTEFAR"], False, seg, 7)
                    else:
                        pygame.draw.lines(surf, C["ROADCASE"], False, seg, 13)
                        pygame.draw.lines(surf, C["ROUTEFAR"], False, seg, 9)
                except Exception:
                    pass

    # ── PAST trail (dimmed — where you've been) ──
    if lat is not None:
        with trail_lock:
            pts = list(trail)
        if len(pts) >= 2:
            scr = [to_screen(p[0], p[1]) for p in pts]
            try:
                pygame.draw.lines(surf, C["PASTCASE"], False, scr, 9)
                pygame.draw.lines(surf, C["PAST"], False, scr, 6)
            except Exception:
                pass

    # ── AHEAD path ──
    turn_pt = None
    road_drawn = False

    # 1) BEST: we have the real route — draw the part still ahead of us.
    if lat is not None:
        with route_lock:
            rp = list(route_pts)
        if len(rp) >= 2:
            # where are we on the route?
            best_i, best_d = 0, float("inf")
            for i, p in enumerate(rp):
                dd = abs(p[0] - lat) + abs(p[1] - lon)
                if dd < best_d:
                    best_d, best_i = dd, i

            # only trust it if we're actually near the line
            near_m = _seg_len((lat, lon), rp[best_i])
            if near_m < 150:
                ahead = rp[best_i:]
                # trim to what fits on screen so we don't draw the whole trip twice
                span_m = max(rw, rh) * MPP * 1.6
                acc, cut = 0.0, len(ahead)
                for i in range(len(ahead) - 1):
                    acc += _seg_len(ahead[i], ahead[i + 1])
                    if acc > span_m:
                        cut = i + 2
                        break
                seg = [to_screen(p[0], p[1]) for p in ahead[:cut]]
                if len(seg) >= 2:
                    try:
                        pygame.draw.lines(surf, C["ROUTEGLOW"], False, seg, 13)
                        pygame.draw.lines(surf, C["ROADCASE"], False, seg, 9)
                        pygame.draw.lines(surf, C["ROUTE"], False, seg, 6)
                        road_drawn = True
                    except Exception:
                        pass

                # marker at the next maneuver, if we know it
                dist_m = d.get("distance_m")
                if dist_m:
                    acc, mk = 0.0, None
                    for i in range(best_i, len(rp) - 1):
                        acc += _seg_len(rp[i], rp[i + 1])
                        if acc >= dist_m:
                            mk = rp[i + 1]
                            break
                    if mk:
                        turn_pt = to_screen(mk[0], mk[1])

    # 2) FALLBACK: no route loaded — approximate from the turn data alone
    if not road_drawn:
        dist_m = d.get("distance_m")
        man = (d.get("maneuver") or "").lower()
        if dist_m is not None and dist_m > 0 and not d.get("arrived"):
            ahead_px = dist_m / MPP
            max_px = (cy - ry) * 0.70
            ahead_px = max(28, min(ahead_px, max_px))

            p0 = (cx, cy)
            p1 = (cx, int(cy - ahead_px))
            bend = -1 if ("left" in man and "u-turn" not in man) else (
                    1 if ("right" in man and "u-turn" not in man) else 0)
            seg = int(min(rw, rh) * 0.30)

            if "u-turn" in man:
                pts_ahead = [p0, p1, (cx + seg // 2, p1[1]),
                             (cx + seg // 2, p1[1] + seg // 2)]
            elif "roundabout" in man:
                pts_ahead = [p0, p1, (cx + seg // 2, p1[1] - seg // 3)]
            elif bend:
                r_ = min(14, seg // 3)
                pts_ahead = [p0, (cx, p1[1] + r_), (cx + bend * r_, p1[1]),
                             (cx + bend * seg, p1[1])]
            else:
                pts_ahead = [p0, p1, (cx, p1[1] - seg // 2)]

            try:
                pygame.draw.lines(surf, C["ROUTEGLOW"], False, pts_ahead, 13)
                pygame.draw.lines(surf, C["ROADCASE"], False, pts_ahead, 9)
                pygame.draw.lines(surf, C["ROUTE"], False, pts_ahead, 6)
            except Exception:
                pass
            turn_pt = p1

    surf.set_clip(clip)

    # turn marker + distance label at the bend
    if turn_pt is not None:
        tx_, ty_ = turn_pt
        if rx + 6 < tx_ < rx + rw - 6 and ry + 6 < ty_ < ry + rh - 6:
            pygame.draw.circle(surf, C["PLAYER"], (tx_, ty_), 7)
            pygame.draw.circle(surf, C["ROUTE"], (tx_, ty_), 7, 3)
            dv_, du_ = fmt_distance(d.get("distance_m"))
            text(surf, f"{dv_}{du_.lower()}", f_lbl, C["TEXT"], tx_ + 11, ty_ - 14)

    # Player chevron — white with dark outline, GTA style
    if lat is not None:
        sz = 12
        tri = [(0, -sz), (sz*.8, sz*.85), (0, sz*.4), (-sz*.8, sz*.85)]
        pts = [(cx + a, cy + b) for a, b in tri]
        pygame.draw.polygon(surf, C["PLAYER"], pts)
        pygame.draw.polygon(surf, C["PLAYEREDGE"], pts, 2)
    else:
        text(surf, "ACQUIRING GPS", f_lbl, C["MUTED"], cx, cy - 6, "center")

    rmsg = d.get("region_msg") or ""
    if rmsg:
        text(surf, rmsg, f_lbl, C["ROUTE"], rx + rw // 2, ry + 6, "center")

    # Dark GTA map border
    pygame.draw.rect(surf, C["MAPEDGE"], rect, 3, border_radius=6)

    # North indicator
    global MAP_BTN_ZIN, MAP_BTN_ZOUT, MAP_BTN_RECENTER
    bs = 22
    MAP_BTN_ZIN  = (rx + rw - bs - 5, ry + rh - bs * 2 - 9, bs, bs)
    MAP_BTN_ZOUT = (rx + rw - bs - 5, ry + rh - bs - 5, bs, bs)
    for r_, lbl in ((MAP_BTN_ZIN, "+"), (MAP_BTN_ZOUT, "-")):
        pygame.draw.rect(surf, C["PANEL"], r_, border_radius=3)
        pygame.draw.rect(surf, C["MAPEDGE"], r_, 1, border_radius=3)
        text(surf, lbl, f_tile, C["TEXT"], r_[0] + bs // 2, r_[1] + 2, "center")

    if panned:
        rw_ = 74
        MAP_BTN_RECENTER = (rx + rw // 2 - rw_ // 2, ry + 5, rw_, 20)
        pygame.draw.rect(surf, C["ROUTE"], MAP_BTN_RECENTER, border_radius=3)
        text(surf, "RECENTER", f_lbl, C["PANEL"],
             MAP_BTN_RECENTER[0] + rw_ // 2, MAP_BTN_RECENTER[1] + 4, "center")
        text(surf, f"{zoom:.1f}x  north-up", f_lbl, C["MUTED"], rx + 6, ry + 6)
    else:
        MAP_BTN_RECENTER = None
        text(surf, "N", f_lbl, C["MUTED"], rx + rw - 13, ry + 6, "center")

    # Speed badge, bottom-left inside the map
    spd = d.get("speed_kph")
    if spd is not None:
        bw, bh = 62, 18
        bx, by_ = rx + 6, ry + rh - bh - 6
        pygame.draw.rect(surf, C["PANEL"], (bx, by_, bw, bh), border_radius=3)
        pygame.draw.rect(surf, C["MAPEDGE"], (bx, by_, bw, bh), 1, border_radius=3)
        text(surf, f"{int(round(spd))} km/h", f_lbl, C["TEXT"], bx + bw // 2, by_ + 4, "center")


def build_frame(screen, fonts, d, flash):
    C.clear()
    C.update(THEMES[resolve_theme(d)])
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts

    W, H = screen.get_size()
    sc = min(H / 320.0, W / 480.0 * 1.0) if W >= H else H / 480.0
    sc = max(0.65, min(sc, 1.6))
    TOP, BOT = int(32 * sc), int(44 * sc)
    MID = H - TOP - BOT
    SPX = int(W * 0.40)
    ac = accent_for(d)

    if cal_active:
        draw_calibration(screen, fonts, sc)
        return

    screen.fill(C["BG"])

    # ── top bar ──
    pygame.draw.rect(screen, C["PANEL"], (0, 0, W, TOP))
    pygame.draw.line(screen, C["BORDER"], (0, TOP), (W, TOP), 1)
    text(screen, "MOTONAV", f_lbl, C["ROUTE"], 10, int(8 * sc))

    linked = d["connected"]
    with transport_lock:
        tname = active_transport
    lc = C["GREEN"] if linked else C["WARN"]
    pygame.draw.circle(screen, lc, (W // 2 - 78, TOP // 2), 4)
    label = tname if (linked and tname) else ("PHONE" if linked else "NO LINK")
    text(screen, label, f_lbl, lc, W // 2 - 70, int(8 * sc))

    # ── settings gear chip + power chip (right side) ──
    global PWR_CHIP, SET_CHIP
    pw_w, chip_h = int(30 * sc), TOP - int(7 * sc)
    pw_x, chip_y = W - pw_w - 6, int(4 * sc)
    PWR_CHIP = (pw_x, chip_y, pw_w, chip_h)
    pygame.draw.rect(screen, C["PANEL2"], PWR_CHIP, border_radius=3)
    pygame.draw.rect(screen, C["RED"], PWR_CHIP, 1, border_radius=3)

    def _pwr_glyph():
        gcx, gcy = pw_x + pw_w // 2, chip_y + chip_h // 2
        rr_ = int(5 * sc)
        pygame.draw.arc(screen, C["RED"], (gcx - rr_, gcy - rr_, rr_ * 2, rr_ * 2),
                        math.radians(-60), math.radians(240), 2)
        pygame.draw.line(screen, C["RED"], (gcx, gcy - rr_ - 1), (gcx, gcy), 2)
    blit_icon(screen, "power", PWR_CHIP, _pwr_glyph)

    # rotate chip
    global ROT_CHIP
    rot_w = int(30 * sc)
    rot_x = pw_x - rot_w - int(5 * sc)
    ROT_CHIP = (rot_x, chip_y, rot_w, chip_h)
    pygame.draw.rect(screen, C["PANEL2"], ROT_CHIP, border_radius=3)
    pygame.draw.rect(screen, C["BORDER"], ROT_CHIP, 1, border_radius=3)

    def _rot_glyph():
        rcx, rcy = rot_x + rot_w // 2, chip_y + chip_h // 2
        rad = int(5 * sc)
        pygame.draw.arc(screen, C["MUTED"], (rcx - rad, rcy - rad, rad * 2, rad * 2),
                        math.radians(40), math.radians(320), 2)
        pygame.draw.polygon(screen, C["MUTED"], [
            (rcx + rad, rcy - rad + 1), (rcx + rad - 4, rcy - rad - 2),
            (rcx + rad + 2, rcy - rad - 3)])
    blit_icon(screen, "rotate", ROT_CHIP, _rot_glyph)

    set_w = int(30 * sc)
    set_x = rot_x - set_w - int(5 * sc)
    SET_CHIP = (set_x, chip_y, set_w, chip_h)
    pygame.draw.rect(screen, C["PANEL2"], SET_CHIP, border_radius=3)
    pygame.draw.rect(screen, C["BORDER"], SET_CHIP, 1, border_radius=3)

    def _gear_glyph():
        scx, scy = set_x + set_w // 2, chip_y + chip_h // 2
        gr = int(4.5 * sc)
        pygame.draw.circle(screen, C["MUTED"], (scx, scy), gr, 2)
        for ang in range(0, 360, 60):
            a = math.radians(ang)
            pygame.draw.line(screen, C["MUTED"],
                (scx + int(math.cos(a) * (gr + 1)), scy + int(math.sin(a) * (gr + 1))),
                (scx + int(math.cos(a) * (gr + 4)), scy + int(math.sin(a) * (gr + 4))), 2)
    blit_icon(screen, "settings", SET_CHIP, _gear_glyph)

    # wifi status text (informational only now)
    with wifi_lock:
        w_ssid = wifi_state["ssid"]
        w_sig = wifi_state["signal"]
    if w_ssid:
        text(screen, f"{signal_bars(w_sig)} {w_ssid[:10]}", f_lbl, C["GREEN"],
             set_x - int(8 * sc), int(8 * sc), "right")
    else:
        text(screen, "wifi off", f_lbl, C["MUTED"], set_x - int(8 * sc), int(8 * sc), "right")

    # ── left: arrow + distance + instruction ──
    pygame.draw.rect(screen, C["PANEL2"], (0, TOP, SPX, MID))
    pygame.draw.line(screen, C["BORDER"], (SPX, TOP), (SPX, TOP + MID), 1)
    # GTA route-blue accent bar
    pygame.draw.rect(screen, C["ROUTE"], (0, TOP + 6, 4, MID - 12))

    arrow_h = int(MID * 0.42)
    acx, acy = SPX // 2, TOP + arrow_h // 2 + int(4 * sc)
    asz = int(min(SPX, arrow_h) * 0.78)

    if d["arrived"]:
        draw_icon(screen, "destination", acx, acy, asz,
                  C["GREEN"] if flash < 18 else C["PANEL2"])
    elif not d["instruction"]:
        for i in range(12):
            a = math.radians(i * 30 + flash * 6)
            dim = tuple(int(v * (0.8 if i % 2 == 0 else 0.25)) for v in C["MUTED"])
            sx = acx + int(math.cos(a) * asz * .38); sy = acy + int(math.sin(a) * asz * .38)
            ex = acx + int(math.cos(a + .45) * asz * .38); ey = acy + int(math.sin(a + .45) * asz * .38)
            pygame.draw.line(screen, dim, (sx, sy), (ex, ey), 2)
    else:
        draw_icon(screen, d["maneuver"], acx, acy, asz, ac)

    info_y = TOP + arrow_h + int(2 * sc)
    if d["arrived"]:
        text(screen, "ARRIVED", f_med, C["GREEN"], SPX // 2, info_y + int(6 * sc), "center")
        text(screen, "DESTINATION REACHED", f_lbl, C["MUTED"], SPX // 2, info_y + int(32 * sc), "center")
    elif not d["instruction"]:
        text(screen, "WAITING FOR NAV", f_sm, C["MUTED"], SPX // 2, info_y + int(12 * sc), "center")
        text(screen, "START GOOGLE MAPS", f_lbl, C["MUTED"], SPX // 2, info_y + int(34 * sc), "center")
    else:
        dv, du = fmt_distance(d["distance_m"])
        dw = f_big.size(dv)[0]
        uw = f_unit.size(du)[0]
        dx0 = SPX // 2 - (dw + uw + 6) // 2
        text(screen, dv, f_big, C["TEXT"], dx0, info_y)
        text(screen, du.lower(), f_unit, C["MUTED"], dx0 + dw + 6, info_y + int(14 * sc))

        iy = info_y + f_big.get_height() + int(2 * sc)
        instr = d["instruction"].upper()
        cw = SPX - 20
        l1 = l2 = ""
        for w in instr.split():
            t = (l1 + " " + w).strip()
            if f_sm.size(t)[0] <= cw:
                l1 = t
            else:
                l2 = (l2 + " " + w).strip()
        text(screen, l1, f_sm, C["TEXT"], SPX // 2, iy, "center", max_w=cw)
        if l2:
            text(screen, l2, f_lbl, C["MUTED"], SPX // 2,
                 iy + f_sm.get_height() + int(2 * sc), "center", max_w=cw)

    # ── right: GTA minimap (inset with margin) ──
    m = int(7 * sc)
    draw_map(screen, fonts, d, (SPX + m, TOP + m, W - SPX - m * 2, MID - m * 2))

    # ── bottom bar ──
    by = TOP + MID
    pygame.draw.rect(screen, C["PANEL"], (0, by, W, BOT))
    pygame.draw.line(screen, C["BORDER"], (0, by), (W, by), 1)
    tw = W // 3
    for i in (1, 2):
        pygame.draw.line(screen, C["BORDER"], (tw * i, by + 7), (tw * i, by + BOT - 7), 1)
    ty = by + int(5 * sc)
    vy = ty + int(12 * sc)
    text(screen, "ETA", f_lbl, C["MUTED"], tw // 2, ty, "center")
    text(screen, fmt_eta(d["eta_seconds"]), f_tile, C["ROUTE"], tw // 2, vy, "center")
    text(screen, "REMAIN", f_lbl, C["MUTED"], tw + tw // 2, ty, "center")
    text(screen, fmt_duration(d["duration_s"]).lower(), f_tile, C["TEXT"], tw + tw // 2, vy, "center")
    text(screen, "TIME", f_lbl, C["MUTED"], tw * 2 + tw // 2, ty, "center")
    text(screen, now_dt().strftime("%H:%M"), f_tile, C["TEXT"],
         tw * 2 + tw // 2, vy, "center")

    # ── WiFi overlay panel ──
    if wifi_panel_open:
        draw_wifi_panel(screen, fonts, sc)
    if bt_panel_open:
        draw_bt_panel(screen, fonts, sc)
    if kb_open:
        draw_keyboard(screen, fonts, sc)
    if settings_open:
        draw_settings(screen, fonts, sc)
    if power_confirm:
        draw_power_dialog(screen, fonts, sc)



def draw_wifi_panel(screen, fonts, sc):
    """Full-screen-ish overlay listing WiFi networks. Tap a SAVED one to join."""
    global WIFI_ROWS, WIFI_BTN_RESCAN, WIFI_BTN_CLOSE
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H

    pad = int(14 * sc)
    px, py = pad, pad
    pw, ph = W - pad * 2, H - pad * 2

    pygame.draw.rect(screen, C["PANEL"], (px, py, pw, ph), border_radius=6)
    pygame.draw.rect(screen, C["MAPEDGE"], (px, py, pw, ph), 2, border_radius=6)

    with wifi_lock:
        nets = list(wifi_state["networks"])
        ip = wifi_state["ip"]
        msg = wifi_state["msg"]
        busy = wifi_state["busy"]

    # header
    text(screen, "WI-FI NETWORKS", f_sm, C["TEXT"], px + 10, py + 7)
    ipt = f"IP {ip}" if ip else "no IP"
    text(screen, ipt, f_lbl, C["MUTED"], px + pw - 10, py + 10, "right")
    pygame.draw.line(screen, C["BORDER"], (px + 8, py + int(26 * sc)),
                     (px + pw - 8, py + int(26 * sc)), 1)

    # rows
    WIFI_ROWS = []
    row_h = int(24 * sc)
    ry0 = py + int(30 * sc)
    max_rows = max(1, (ph - int(72 * sc)) // row_h)
    for i, n in enumerate(nets[:max_rows]):
        r = (px + 8, ry0 + i * row_h, pw - 16, row_h - 2)
        if n["active"]:
            pygame.draw.rect(screen, C["ROUTEGLOW"], r, border_radius=3)
        elif n["saved"]:
            pygame.draw.rect(screen, C["PANEL2"], r, border_radius=3)
        col = C["TEXT"] if (n["saved"] or n["active"]) else C["MUTED"]
        text(screen, signal_bars(n["signal"]), f_lbl, C["MUTED"], r[0] + 6, r[1] + int(6 * sc))
        text(screen, n["ssid"][:22], f_lbl, col, r[0] + int(36 * sc), r[1] + int(6 * sc))
        tag = "CONNECTED" if n["active"] else ("SAVED" if n["saved"] else "LOCKED")
        tcol = C["GREEN"] if n["active"] else (C["ROUTE"] if n["saved"] else C["MUTED"])
        text(screen, tag, f_lbl, tcol, r[0] + r[2] - 6, r[1] + int(6 * sc), "right")
        WIFI_ROWS.append((r, n["ssid"], n["saved"] or n["active"]))

    if not nets:
        text(screen, "No networks found", f_lbl, C["MUTED"], px + pw // 2,
             ry0 + int(20 * sc), "center")

    # status line
    if msg:
        text(screen, msg, f_lbl, C["WARN"] if not busy else C["MUTED"],
             px + pw // 2, py + ph - int(40 * sc), "center")

    # buttons
    bw, bh = int(90 * sc), int(24 * sc)
    by_ = py + ph - bh - int(8 * sc)
    WIFI_BTN_RESCAN = (px + 10, by_, bw, bh)
    WIFI_BTN_CLOSE = (px + pw - bw - 10, by_, bw, bh)
    for rect, label, col in ((WIFI_BTN_RESCAN, "RESCAN", C["ROUTE"]),
                             (WIFI_BTN_CLOSE, "CLOSE", C["MUTED"])):
        pygame.draw.rect(screen, C["PANEL2"], rect, border_radius=3)
        pygame.draw.rect(screen, col, rect, 1, border_radius=3)
        text(screen, label, f_lbl, col, rect[0] + rect[2] // 2, rect[1] + int(7 * sc), "center")


# ══════════════════════════════════════════════════════════════════════════════
#  NETWORK
# ══════════════════════════════════════════════════════════════════════════════


def _seg_len(a, b):
    dlat = (b[0] - a[0]) * 111320.0
    dlon = (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon)


def update_step_guidance(lat, lon):
    """
    Standalone turn-by-turn: work out where we are along the route and
    which maneuver is next, then fill the nav display ourselves.
    Used when the phone sent OSRM steps (no Google Maps needed).
    """
    if not STEP_MODE or lat is None:
        return
    with route_lock:
        pts = list(route_pts)
        steps = list(route_steps)
    if len(pts) < 2 or not steps:
        return

    # nearest point on the route to us
    best_i, best_d = 0, float("inf")
    for i, p in enumerate(pts):
        d = _seg_len((lat, lon), p)
        if d < best_d:
            best_d, best_i = d, i

    # the next maneuver ahead of that point
    nxt = None
    for s in steps:
        si = _nearest_route_index(pts, s["lat"], s["lon"])
        if si > best_i + 1:
            nxt = (s, si)
            break
    if nxt is None:
        nxt = (steps[-1], len(pts) - 1)
    step, si = nxt

    # distance along the route from us to that maneuver
    dist = _seg_len((lat, lon), pts[best_i])
    for i in range(best_i, min(si, len(pts) - 1)):
        dist += _seg_len(pts[i], pts[i + 1])

    arrived = step["m"] == "destination" and dist < 30

    with nav_lock:
        nav["instruction"] = step["t"]
        nav["road"] = step["r"]
        nav["maneuver"] = step["m"]
        nav["distance_m"] = dist
        nav["distance_raw"] = (f"{dist/1000:.1f} km" if dist >= 1000
                               else f"{int(round(dist))} m")
        nav["arrived"] = arrived
        # remaining distance along the whole route
        remain_m = dist
        for i in range(si, len(pts) - 1):
            remain_m += _seg_len(pts[i], pts[i + 1])

        # scale OSRM's own duration by how much route is left — this is
        # far better than assuming an average speed
        with route_lock:
            total_km = route_meta.get("km", 0.0)
            total_sec = route_meta.get("sec", 0.0)
        total_m = total_km * 1000.0
        if total_sec > 0 and total_m > 0:
            secs = int(total_sec * max(0.0, min(1.0, remain_m / total_m)))
        else:
            secs = int(remain_m / (35 * 1000 / 3600))
        nav["duration_s"] = secs
        nav["eta_seconds"] = secs


_idx_cache = {}


def _nearest_route_index(pts, lat, lon):
    key = (round(lat, 5), round(lon, 5), len(pts))
    hit = _idx_cache.get(key)
    if hit is not None:
        return hit
    best, bd = 0, float("inf")
    for i, p in enumerate(pts):
        d = abs(p[0] - lat) + abs(p[1] - lon)
        if d < bd:
            bd, best = d, i
    if len(_idx_cache) > 512:
        _idx_cache.clear()
    _idx_cache[key] = best
    return best


def clear_guidance(reason=""):
    """Clear the turn instruction only. Route and trail are kept."""
    with nav_lock:
        if not nav["instruction"] and not nav["distance_raw"]:
            return
        nav["instruction"] = ""
        nav["road"] = ""
        nav["distance_m"] = None
        nav["distance_raw"] = ""
        nav["maneuver"] = ""
        nav["arrived"] = False
    if reason:
        print(f"Guidance cleared: {reason}")


def clear_navigation(reason=""):
    """Drop turn guidance, the trail AND the route — the trip has ended."""
    with nav_lock:
        nav["instruction"] = ""
        nav["road"] = ""
        nav["distance_m"] = None
        nav["distance_raw"] = ""
        nav["maneuver"] = ""
        nav["duration_s"] = None
        nav["eta_seconds"] = None
        nav["arrived"] = False
    with trail_lock:
        trail.clear()
    with route_lock:
        had = len(route_pts)
        route_pts.clear()
        route_steps.clear()
        route_meta["dest"] = ""
        route_meta["km"] = 0.0
        route_meta["sec"] = 0.0
    globals()["STEP_MODE"] = False
    if reason:
        print(f"Navigation cleared: {reason} (route pts dropped: {had})")


def nav_watchdog():
    """
    If turn updates stop arriving, navigation has ended (Maps closed or the
    route was cancelled). Clear the display rather than leaving a stale turn
    on screen. Position updates alone do not count.
    """
    global last_nav_ms
    while True:
        time.sleep(2)
        with nav_lock:
            has_instr = bool(nav["instruction"])
        if has_instr and last_nav_ms and (time.time() - last_nav_ms) > NAV_STALE_SEC:
            clear_guidance("no updates from phone")
            last_nav_ms = 0.0


def handle_message(msg, source="WIFI"):
    global theme_mode, active_transport, _rx_region
    with transport_lock:
        active_transport = source
    mtype = msg.get("type")

    if mtype == "region_begin":
        _rx_region = {"expect": int(msg.get("bytes", 0)), "buf": bytearray()}
        with nav_lock:
            nav["region_msg"] = "Receiving map 0%"
        print(f"OSM: incoming region {_rx_region['expect']} bytes")
        return

    if mtype == "region_chunk":
        if _rx_region is not None:
            try:
                _rx_region["buf"] += base64.b64decode(msg.get("d", ""))
                got, exp = len(_rx_region["buf"]), max(1, _rx_region["expect"])
                pct = int(got * 100 / exp)
                with nav_lock:
                    nav["region_msg"] = f"Receiving map {pct}%"
            except Exception as e:
                print(f"region chunk: {e}")
        return

    if mtype == "region_end":
        if _rx_region is not None:
            blob = bytes(_rx_region["buf"])
            _rx_region = None
            ok = save_region_bytes(blob)
            with nav_lock:
                nav["region_msg"] = "Map loaded" if ok else "Map failed"
        return

    if mtype == "route":
        try:
            pts = msg.get("pts") or []
            # pts arrive as flat [lat,lon,lat,lon,...] in 1e5 fixed point
            out = []
            for i in range(0, len(pts) - 1, 2):
                out.append((pts[i] / 1e5, pts[i + 1] / 1e5))
            with route_lock:
                route_pts.clear()
                route_pts.extend(out)
                route_meta["dest"] = msg.get("dest", "")
                route_meta["km"] = float(msg.get("km", 0) or 0)
                route_meta["sec"] = float(msg.get("sec", 0) or 0)
            print(f"ROUTE: {len(out)} pts -> {route_meta['dest']} "
                  f"({route_meta['km']:.1f} km)")
            with _gap_lock:
                _gap_state["t"] = 0.0
                _gap_state["lat"] = None
                _gap_state["lon"] = None
            # only pull in the first stretch of the buffer here — the rest
            # streams in as the rider advances (see maybe_stream_buffer)
            threading.Thread(
                target=lambda: report_map_gaps(source, ahead_km=BUFFER_AHEAD_KM,
                                               behind_km=BUFFER_BEHIND_KM),
                daemon=True).start()
        except Exception as e:
            print(f"route parse: {e}")
        return

    if mtype == "time":
        try:
            with time_lock:
                phone_time["epoch"] = float(msg.get("epoch", 0))
                phone_time["tz"] = msg.get("tz")
                phone_time["at"] = time.time()
            print(f"Clock synced from phone: {now_dt().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"time msg: {e}")
        return

    if mtype == "mapmode":
        m = str(msg.get("mode", "")).lower()
        if m in ("minimal", "detailed"):
            globals()["map_detail"] = m
            print(f"Map mode -> {m}")
        return

    if mtype == "end_nav":
        end_navigation()
        return

    if mtype == "clear_map":
        clear_map_storage()
        return

    if mtype == "gaps_req":
        threading.Thread(
            target=lambda: report_map_gaps(source, ahead_km=BUFFER_AHEAD_KM,
                                           behind_km=BUFFER_BEHIND_KM),
            daemon=True).start()
        return

    if mtype == "metrics_req":
        send_metrics(source)
        return

    if mtype == "steps":
        global STEP_MODE
        try:
            raw = msg.get("steps") or []
            out = []
            for s in raw:
                out.append({
                    "lat": s.get("lat", 0) / 1e5,
                    "lon": s.get("lon", 0) / 1e5,
                    "m": s.get("m", "straight"),
                    "t": s.get("t", ""),
                    "r": s.get("r", ""),
                })
            with route_lock:
                route_steps.clear()
                route_steps.extend(out)
            STEP_MODE = bool(out)
            print(f"STEPS: {len(out)} maneuvers — standalone guidance on")
        except Exception as e:
            print(f"steps parse: {e}")
        return

    if mtype == "route_clear":
        with route_lock:
            route_pts.clear()
            route_steps.clear()
            route_meta["dest"] = ""
        globals()["STEP_MODE"] = False
        print("ROUTE: cleared")
        return

    if mtype == "ping":
        with nav_lock:
            nav["connected"] = True
        return

    if mtype == "nav_idle":
        clear_guidance("turns paused")
        return

    if mtype == "nav_stop":
        clear_navigation("phone ended navigation")
        return

    if mtype == "theme":
        mode = str(msg.get("mode", "")).lower()
        if mode in ("auto", "day", "night"):
            with theme_lock:
                theme_mode = mode
            print(f"Theme set from phone -> {mode}")
        return

    if mtype == "pos":
        lat, lon = msg.get("lat"), msg.get("lon")
        with nav_lock:
            nav["lat"] = lat
            nav["lon"] = lon
            nav["bearing"] = msg.get("bearing")
            nav["speed_kph"] = msg.get("speed_kph")
            nav["connected"] = True
        if lat is not None and lon is not None:
            with trail_lock:
                if not trail or moved_enough(trail[-1], (lat, lon)):
                    trail.append((lat, lon))
                    if len(trail) > TRAIL_MAX:
                        del trail[0:len(trail) - TRAIL_MAX]
            # standalone turn-by-turn straight from the OSRM steps
            if STEP_MODE:
                try:
                    update_step_guidance(lat, lon)
                    globals()["last_nav_ms"] = time.time()
                except Exception as e:
                    print(f"step guidance: {e}")
            try:
                maybe_stream_buffer(lat, lon, source)
            except Exception as e:
                print(f"stream buffer: {e}")
    else:
        global last_nav_ms
        last_nav_ms = time.time()
        with nav_lock:
            nav.update(msg)
            nav["connected"] = True
        print(f"NAV: {msg.get('instruction','')} | {msg.get('distance_raw','')}")




# ══════════════════════════════════════════════════════════════════════════════
#  DISCOVERY  — let the phone find the Pi on any shared network
# ══════════════════════════════════════════════════════════════════════════════
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "9998"))
DISCOVERY_MAGIC = b"MOTONAV_DISCOVER"


def discovery_server():
    """
    Answer UDP probes so the phone can locate us without a typed IP.
    Works on a phone hotspot, home Wi-Fi, or any shared LAN.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    try:
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except Exception as e:
        print(f"Discovery: bind failed {e}")
        return
    print(f"Discovery: listening on UDP :{DISCOVERY_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(256)
            if DISCOVERY_MAGIC in data:
                ips = local_ips()
                # answer with the address on the interface the probe came in on
                best = ips[0] if ips else ""
                for ip in ips:
                    if ip.rsplit(".", 1)[0] == addr[0].rsplit(".", 1)[0]:
                        best = ip
                        break
                reply = f"MOTONAV|{best}|{TCP_PORT}|{socket.gethostname()}".encode()
                sock.sendto(reply, addr)
                print(f"Discovery: answered {addr[0]} with {best}:{TCP_PORT}")
        except Exception as e:
            print(f"Discovery error: {e}")
            time.sleep(2)


def rfcomm_server():
    """Bluetooth serial listener. Runs alongside the TCP server."""
    global active_transport
    if bluetooth is None:
        print("Bluetooth: PyBluez not installed (sudo apt-get install python3-bluez)")
        return

    channel = int(os.environ.get("RFCOMM_CHANNEL", "22"))
    try:
        subprocess.run(["sdptool", "add", f"--channel={channel}", "SP"],
                       capture_output=True, timeout=8)
    except Exception:
        pass

    while True:
        srv = cli = None
        try:
            srv = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            srv.bind(("", channel))
            srv.listen(1)
            print(f"Bluetooth listening on RFCOMM channel {channel}")
            cli, info = srv.accept()
            print(f"Phone connected via Bluetooth: {info}")
            register_reply("BT", lambda s: cli.send(s.encode("utf-8")))
            with transport_lock:
                active_transport = "BT"
            with nav_lock:
                nav["connected"] = True
            buf = b""
            while True:
                data = cli.recv(1024)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        handle_message(json.loads(line.decode("utf-8")), "BT")
                    except Exception as e:
                        print(f"BT bad line: {e}")
        except Exception as e:
            print(f"Bluetooth error: {e}")
        finally:
            for s in (cli, srv):
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
            with transport_lock:
                if active_transport == "BT":
                    active_transport = None
            with nav_lock:
                nav["connected"] = False
        time.sleep(3)


def _tcp_client_thread(conn, addr):
    """Handle one phone connection. Several may exist at once."""
    global _tcp_clients
    with transport_lock:
        active_transport = "WIFI"
    with nav_lock:
        nav["connected"] = True
    print(f"Phone connected via Wi-Fi: {addr[0]}")
    register_reply("WIFI", lambda s: conn.sendall(s.encode("utf-8")))
    buf = b""
    try:
        conn.settimeout(60)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        while True:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue                     # idle is normal
            if not data:
                break                        # peer closed
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    handle_message(json.loads(line.decode("utf-8")), "WIFI")
                except Exception as e:
                    print(f"bad line: {e}")
    except Exception as e:
        print(f"client {addr[0]}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        unregister_reply("WIFI")
        with _tcp_lock:
            _tcp_clients.discard(threading.current_thread())
            remaining = len(_tcp_clients)
        print(f"Phone disconnected: {addr[0]} ({remaining} still connected)")
        if remaining == 0:
            with transport_lock:
                if active_transport == "WIFI":
                    pass
            with nav_lock:
                nav["connected"] = False
            clear_navigation("all phones disconnected")


_tcp_clients = set()
_tcp_lock = threading.Lock()


def tcp_server():
    """Accept phone connections. Multiple clients are allowed."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", TCP_PORT))
    srv.listen(4)
    ips = local_ips()
    print(f"TCP listening on :{TCP_PORT}   (Pi IP: {', '.join(ips) or 'unknown'})")
    while True:
        try:
            conn, addr = srv.accept()
            t = threading.Thread(target=_tcp_client_thread, args=(conn, addr),
                                 daemon=True)
            with _tcp_lock:
                _tcp_clients.add(t)
            t.start()
        except Exception as e:
            print(f"accept error: {e}")
            time.sleep(1)


def local_ips():
    out = []
    try:
        import subprocess
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        out = [x for x in r.stdout.split() if x and not x.startswith("127.")]
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  DEMO FEEDER
# ══════════════════════════════════════════════════════════════════════════════
# A closed loop that actually turns where the announced instructions say it
# will, with a small fabricated street grid behind it, so --demo shows real
# turn-by-turn navigation instead of an arrow drifting over a blank grid.
DEMO_LEGS = [
    # (instruction, road, leg_len_m, distance_raw, maneuver)
    ("Continue straight", "Outer Ring Road", 1200, "1.2 km", "straight"),
    ("Turn left onto MG Road", "MG Road", 350, "350 m", "turn-left"),
    ("Turn right onto Brigade Road", "Brigade Road", 80, "80 m", "turn-right"),
    ("Keep slight right", "Hosur Road", 600, "600 m", "slight-right"),
    ("Take the roundabout", "Residency Road", 500, "500 m", "roundabout"),
    ("Make a U-turn", "Lavelle Road", 120, "120 m", "u-turn-left"),
    ("You have arrived", "Destination", 0, "0 m", "destination"),
]

DEMO_TURN_DEG = {
    "straight": 0, "turn-left": -75, "turn-right": 75,
    "slight-left": -25, "slight-right": 25,
    "roundabout": 80, "u-turn-left": 175, "destination": 0,
}

DEMO_SPEED_MPS = 11.0        # ~40 km/h, a plausible urban pace
DEMO_TICK_S = 0.2
DEMO_ARRIVE_PAUSE_S = 3.0


def _demo_walk(lat, lon, heading_deg, dist_m):
    """Project forward dist_m along heading_deg (planar approx, fine locally)."""
    lat2 = lat + (dist_m * math.cos(math.radians(heading_deg))) / tilestore.M_PER_DEG
    lon2 = lon + (dist_m * math.sin(math.radians(heading_deg))) / (
        tilestore.M_PER_DEG * math.cos(math.radians(lat)))
    return lat2, lon2


def _build_demo_route(start_lat, start_lon, start_heading):
    """
    Walk DEMO_LEGS leg by leg into a real polyline that turns exactly where
    each instruction says it will. Returns (pts, legs, total_m):
      pts     -- the full loop polyline, for route_pts
      legs    -- per-leg metadata, cum0 = cumulative distance at leg start
      total_m -- total loop length
    """
    lat, lon, heading = start_lat, start_lon, start_heading
    pts = [(lat, lon)]
    legs = []
    cum = 0.0
    for ins, road, length_m, dr, man in DEMO_LEGS:
        turn = DEMO_TURN_DEG.get(man, 0)
        if length_m > 0:
            if man == "roundabout" and turn:
                n = 6                      # bend gradually so it reads as a curve
                for _ in range(n):
                    lat, lon = _demo_walk(lat, lon, heading, length_m / n)
                    heading = (heading + turn / n) % 360
                    pts.append((lat, lon))
            else:
                lat, lon = _demo_walk(lat, lon, heading, length_m)
                pts.append((lat, lon))
                heading = (heading + turn) % 360
        else:
            heading = (heading + turn) % 360
        legs.append({"cum0": cum, "len": length_m, "ins": ins,
                     "road": road, "dr": dr, "man": man})
        cum += length_m
    return pts, legs, cum


def _demo_chunk_ways(cls, line_pts, max_seg_m=250):
    """
    Break a polyline into short (<=max_seg_m) 2-point ways so each segment's
    tile bucket matches where it's actually drawn (tilestore.store_ways
    indexes a way by its first point's tile only).
    """
    out = []
    for a, b in zip(line_pts, line_pts[1:]):
        length = _seg_len(a, b)
        n = max(1, math.ceil(length / max_seg_m))
        for i in range(n):
            f0, f1 = i / n, (i + 1) / n
            p0 = (a[0] + (b[0] - a[0]) * f0, a[1] + (b[1] - a[1]) * f0)
            p1 = (a[0] + (b[0] - a[0]) * f1, a[1] + (b[1] - a[1]) * f1)
            out.append((cls, [p0, p1]))
    return out


def _build_demo_grid(pts, spacing_m=140, pad_m=250):
    """
    Fabricate a small Manhattan-style street grid around the route's
    bounding box -- purely visual texture, never real map data. The route
    itself becomes a class-3 "primary" road; cross-streets are class-6
    "residential".
    """
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    lat0 = sum(lats) / len(lats)
    pad_lat = pad_m / tilestore.M_PER_DEG
    pad_lon = pad_m / (tilestore.M_PER_DEG * math.cos(math.radians(lat0)))
    south, north = min(lats) - pad_lat, max(lats) + pad_lat
    west, east = min(lons) - pad_lon, max(lons) + pad_lon

    ways = _demo_chunk_ways(3, pts)        # the route itself, as the main road

    dlat_step = spacing_m / tilestore.M_PER_DEG
    dlon_step = spacing_m / (tilestore.M_PER_DEG * math.cos(math.radians(lat0)))
    for i in range(math.ceil((north - south) / dlat_step) + 1):
        la = south + i * dlat_step
        ways.extend(_demo_chunk_ways(6, [(la, west), (la, east)]))
    for j in range(math.ceil((east - west) / dlon_step) + 1):
        lo = west + j * dlon_step
        ways.extend(_demo_chunk_ways(6, [(south, lo), (north, lo)]))
    return ways


def _demo_pos_at(pts, dist_m):
    """Interpolate (lat, lon, bearing) at arclength dist_m along pts."""
    acc = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = _seg_len(a, b)
        if dist_m <= acc + seg or i == len(pts) - 2:
            frac = 0.0 if seg <= 0 else min(1.0, max(0.0, (dist_m - acc) / seg))
            lat = a[0] + (b[0] - a[0]) * frac
            lon = a[1] + (b[1] - a[1]) * frac
            bearing = math.degrees(math.atan2(
                (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2)),
                (b[0] - a[0]))) % 360
            return lat, lon, bearing
        acc += seg
    return pts[-1][0], pts[-1][1], 0.0


def _demo_leg_at(legs, cursor):
    for leg in legs:
        if cursor < leg["cum0"] + leg["len"] or leg is legs[-1]:
            return leg
    return legs[-1]


def demo_feeder():
    pts, legs, total_m = _build_demo_route(12.9716, 77.5946, 45.0)

    with route_lock:
        route_pts.clear()
        route_pts.extend(pts)
        route_meta["dest"] = "Demo Loop"
        route_meta["km"] = total_m / 1000.0
        route_meta["sec"] = total_m / DEMO_SPEED_MPS

    if tiles is not None and tilestore is not None:
        try:
            tiles.store_ways(_build_demo_grid(pts))
        except Exception as e:
            print(f"demo tiles: {e}")

    cursor = 0.0
    time.sleep(0.5)
    while True:
        leg = _demo_leg_at(legs, cursor)
        arrived = leg is legs[-1]
        remaining = 0.0 if arrived else max(0.0, leg["cum0"] + leg["len"] - cursor)
        left_s = max(0.0, (total_m - cursor) / DEMO_SPEED_MPS)
        lat, lon, bearing = _demo_pos_at(pts, cursor)

        with nav_lock:
            nav.update({
                "instruction": leg["ins"], "road": leg["road"],
                "distance_m": remaining, "distance_raw": leg["dr"],
                "maneuver": leg["man"], "duration_s": left_s,
                "eta_seconds": left_s, "arrived": arrived,
                "lat": lat, "lon": lon, "bearing": bearing,
                "speed_kph": 0 if arrived else round(DEMO_SPEED_MPS * 3.6),
                "connected": True,
            })
        with trail_lock:
            if not trail or moved_enough(trail[-1], (lat, lon)):
                trail.append((lat, lon))
                if len(trail) > TRAIL_MAX:
                    del trail[0:len(trail) - TRAIL_MAX]

        if arrived:
            time.sleep(DEMO_ARRIVE_PAUSE_S)
            cursor = 0.0
            with trail_lock:
                trail.clear()
            continue

        cursor = min(total_m, cursor + DEMO_SPEED_MPS * DEMO_TICK_S)
        time.sleep(DEMO_TICK_S)




# ══════════════════════════════════════════════════════════════════════════════
#  WIFI MANAGER (nmcli)
# ══════════════════════════════════════════════════════════════════════════════
import subprocess

wifi_state = {
    "ssid": None,        # currently connected SSID
    "ip": None,
    "signal": None,
    "networks": [],      # [{ssid, signal, saved, active}]
    "busy": False,
    "msg": "",
}
wifi_lock = threading.Lock()
WIFI_CHIP = None         # tap target in top bar
wifi_panel_open = False
WIFI_ROWS = []           # [(rect, ssid)] tap targets in the panel
WIFI_BTN_RESCAN = None
WIFI_BTN_CLOSE = None


def _run(cmd, timeout=12):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def wifi_refresh(rescan=False):
    """Update current connection + list of visible networks."""
    with wifi_lock:
        wifi_state["busy"] = True
        wifi_state["msg"] = "Scanning..." if rescan else "Refreshing..."

    # current connection
    rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"])
    cur_ssid, cur_sig = None, None
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "yes":
                cur_ssid, cur_sig = parts[1], parts[2]
                break

    # saved profiles
    saved = set()
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    if rc == 0:
        for line in out.splitlines():
            p = line.split(":")
            if len(p) >= 2 and "wireless" in p[1]:
                saved.add(p[0])

    # visible networks
    cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"]
    if rescan:
        cmd.insert(4, "--rescan")
        cmd.insert(5, "yes")
    rc, out, _ = _run(cmd, timeout=25)
    nets, seen = [], set()
    if rc == 0:
        for line in out.splitlines():
            p = line.split(":")
            if not p or not p[0]:
                continue
            ssid = p[0]
            if ssid in seen:
                continue
            seen.add(ssid)
            try:
                sig = int(p[1]) if len(p) > 1 and p[1] else 0
            except ValueError:
                sig = 0
            nets.append({"ssid": ssid, "signal": sig,
                         "saved": ssid in saved, "active": ssid == cur_ssid})
    nets.sort(key=lambda n: (not n["active"], not n["saved"], -n["signal"]))

    # ip
    ip = None
    rc, out, _ = _run(["hostname", "-I"], timeout=5)
    if rc == 0:
        ips = [i for i in out.split() if not i.startswith("127.")]
        ip = ips[0] if ips else None

    with wifi_lock:
        wifi_state["ssid"] = cur_ssid
        wifi_state["signal"] = cur_sig
        wifi_state["ip"] = ip
        wifi_state["networks"] = nets[:8]
        wifi_state["busy"] = False
        wifi_state["msg"] = ""


def wifi_connect(ssid):
    """Connect to a SAVED network (no password entry needed)."""
    with wifi_lock:
        wifi_state["busy"] = True
        wifi_state["msg"] = f"Connecting to {ssid[:14]}..."
    rc, out, err = _run(["nmcli", "connection", "up", "id", ssid], timeout=30)
    if rc != 0:
        rc, out, err = _run(["nmcli", "dev", "wifi", "connect", ssid], timeout=30)
    with wifi_lock:
        wifi_state["msg"] = "Connected" if rc == 0 else (err or "Failed")[:34]
        wifi_state["busy"] = False
    wifi_refresh()


def wifi_worker(action, arg=None):
    threading.Thread(
        target=(lambda: wifi_connect(arg)) if action == "connect"
        else (lambda: wifi_refresh(rescan=(action == "rescan"))),
        daemon=True).start()


def wifi_poller():
    while True:
        try:
            wifi_refresh()
        except Exception:
            pass
        time.sleep(20)


def signal_bars(sig):
    if sig is None:
        return "----"
    try:
        s = int(sig)
    except (TypeError, ValueError):
        return "----"
    n = 1 if s < 30 else 2 if s < 55 else 3 if s < 75 else 4
    return "|" * n + "." * (4 - n)








# ══════════════════════════════════════════════════════════════════════════════
#  OFFLINE OSM BASEMAP
# ══════════════════════════════════════════════════════════════════════════════
try:
    import tilestore
except Exception as _e:
    print(f"tilestore unavailable: {_e}")
    tilestore = None

if DEMO:
    # never let the demo's fabricated streets touch a real rider's cache
    import tempfile
    MAP_DIR = tempfile.mkdtemp(prefix="motonav_demo_")
else:
    MAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")
tiles = tilestore.TileStore(MAP_DIR) if tilestore else None

REGION_FILE = "region.mnosm"
_rx_region = None


def region_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), REGION_FILE)


def load_region():
    if tiles is None:
        return False
    s = tiles.stats()
    if s["tiles"]:
        print(f"Maps: {s['tiles']} tiles, {s['ways']} roads, "
              f"{s['bytes']//1024} KB on disk")
        return True
    print("Maps: no tiles yet — import a .osm.pbf or let the app fill gaps")
    return False


def save_region_bytes(blob):
    """
    A .mnosm blob arrived from the phone (gap fill). Split it into tiles and
    append — existing tiles are never overwritten, only added to.
    """
    if tiles is None:
        return False
    try:
        if len(blob) < 10 or blob[:6] != b"MNOSM1":
            print("Maps: bad blob header")
            return False
        n_ways = struct.unpack_from("<I", blob, 6 + 32)[0]
        off = 6 + 32 + 4
        ways = []
        for _ in range(n_ways):
            if off + 3 > len(blob):
                break
            cls, npts = struct.unpack_from("<BH", blob, off)
            off += 3
            need = npts * 8
            if off + need > len(blob):
                break
            pts = []
            for i in range(npts):
                a, b = struct.unpack_from("<ii", blob, off + i * 8)
                pts.append((a / 1e6, b / 1e6))
            off += need
            if npts >= 2:
                ways.append((cls, pts))
        touched = tiles.store_ways(ways)
        print(f"Maps: +{len(ways)} roads into {len(touched)} tiles "
              f"({len(blob)//1024} KB)")
        with nav_lock:
            nav["region_msg"] = f"+{len(ways)} roads"
        return True
    except Exception as e:
        print(f"Maps: append failed {e}")
        return False




# ══════════════════════════════════════════════════════════════════════════════
#  LIFECYCLE / METRICS  (driven from the phone app)
# ══════════════════════════════════════════════════════════════════════════════
_reply_socks = {}          # transport -> callable(str)
_reply_lock = threading.Lock()


def register_reply(transport, fn):
    with _reply_lock:
        _reply_socks[transport] = fn


def unregister_reply(transport):
    with _reply_lock:
        _reply_socks.pop(transport, None)


def reply(transport, payload):
    """Send a JSON line back to the phone."""
    with _reply_lock:
        fn = _reply_socks.get(transport) or (
            next(iter(_reply_socks.values())) if _reply_socks else None)
    if not fn:
        return
    try:
        fn(json.dumps(payload) + "\n")
    except Exception as e:
        print(f"reply failed: {e}")


def _dir_size(path):
    total = 0
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total



# ── streaming buffer ─────────────────────────────────────────────────────
# Rather than pull every tile the whole trip needs the moment a route is
# set, we only ever ask the phone for a stretch around the rider: a bit
# behind (in case of a U-turn) and a good distance ahead. As the rider
# advances, the window slides forward and pulls in fresh ground — the
# buffer stays topped up but the Pi (and the phone's radio) is never asked
# to move the whole map in one go.
BUFFER_AHEAD_KM = 15.0     # how far up the road tiles are kept ready
BUFFER_BEHIND_KM = 1.5     # small margin behind the rider
GAP_CHECK_MIN_S = 20.0     # don't re-check coverage more often than this
GAP_CHECK_MOVE_M = 800.0   # ...unless the rider has moved at least this far

_gap_state = {"t": 0.0, "lat": None, "lon": None}
_gap_lock = threading.Lock()


def _nearest_index(pts, lat, lon):
    best_i, best_d2 = 0, None
    for i, (la, lo) in enumerate(pts):
        dlat = (la - lat) * tilestore.M_PER_DEG
        dlon = (lo - lon) * tilestore.M_PER_DEG * math.cos(math.radians(lat))
        d2 = dlat * dlat + dlon * dlon
        if best_d2 is None or d2 < best_d2:
            best_d2, best_i = d2, i
    return best_i


def _route_window(pts, lat, lon, ahead_km, behind_km):
    """
    Slice the route to the stretch around the rider — behind_km back,
    ahead_km ahead, measured along the route from the closest point —
    so gap checks only ever cover the buffer, not the whole trip.
    """
    if len(pts) < 2 or lat is None or lon is None:
        return None
    i0 = _nearest_index(pts, lat, lon)

    def walk(step, budget_m):
        out = [pts[i0]]
        dist, i = 0.0, i0
        while 0 <= i + step < len(pts) and dist < budget_m:
            nxt = pts[i + step]
            dist += math.hypot(
                (nxt[0] - pts[i][0]) * tilestore.M_PER_DEG,
                (nxt[1] - pts[i][1]) * tilestore.M_PER_DEG * math.cos(math.radians(nxt[0])))
            out.append(nxt)
            i += step
        return out

    back = walk(-1, behind_km * 1000.0)
    fwd = walk(1, ahead_km * 1000.0)
    return list(reversed(back))[:-1] + fwd


def report_map_gaps(transport="WIFI", ahead_km=None, behind_km=BUFFER_BEHIND_KM):
    """
    Work out which tiles are missing and ask the phone to fetch just those.
    This is what keeps downloads small: we never re-request ground we
    already hold.

    With ahead_km set and a live GPS fix, only the buffer window around the
    rider's current position is checked, so a long trip fills progressively
    as it's driven instead of all at once. Without a fix yet, the whole
    route is checked, same as before.
    """
    if tiles is None or tilestore is None:
        return
    with route_lock:
        pts = list(route_pts)
    if len(pts) < 2:
        return

    window = None
    if ahead_km:
        with nav_lock:
            lat, lon = nav.get("lat"), nav.get("lon")
        window = _route_window(pts, lat, lon, ahead_km, behind_km)
    use_pts = window if window else pts
    label = "buffer" if window else "route"

    keys = tilestore.keys_along(use_pts, radius_m=1500)
    missing = tiles.missing(keys)
    if not missing:
        print(f"Maps: {label} fully covered ({len(keys)} tiles)")
        reply(transport, {"type": "map_gaps", "boxes": [], "have": len(keys)})
        return

    # merge adjacent missing tiles into bigger boxes so the phone makes
    # a handful of large requests instead of dozens of tiny ones
    boxes = []
    remaining = set(missing)
    while remaining:
        y, x = sorted(remaining)[0]
        # grow east
        x2 = x
        while (y, x2 + 1) in remaining:
            x2 += 1
        # grow north while the whole row is missing
        y2 = y
        while all((y2 + 1, xx) in remaining for xx in range(x, x2 + 1)):
            y2 += 1
        for yy in range(y, y2 + 1):
            for xx in range(x, x2 + 1):
                remaining.discard((yy, xx))
        s, w, _, _ = tilestore.tile_bounds((y, x))
        _, _, n, e = tilestore.tile_bounds((y2, x2))
        boxes.append({"s": round(s, 4), "w": round(w, 4),
                      "n": round(n, 4), "e": round(e, 4)})

    print(f"Maps: {len(missing)}/{len(keys)} {label} tiles missing -> "
          f"{len(boxes)} fill request(s)")
    with nav_lock:
        nav["region_msg"] = f"fetching {len(missing)} tiles"
    reply(transport, {"type": "map_gaps", "boxes": boxes,
                      "missing": len(missing), "have": len(keys) - len(missing)})


def maybe_stream_buffer(lat, lon, transport="WIFI"):
    """
    Called on every GPS fix while a route is active. Keeps the tile buffer
    ahead of the rider topped up by re-checking coverage every so often (or
    sooner if the rider has moved far enough that the last check is stale)
    and asking the phone to stream in whatever the buffer window is still
    missing. This is the "as and when needed" half of map delivery — small,
    frequent, forward-looking requests instead of one big download at the
    start of a trip.
    """
    if tiles is None or tilestore is None or lat is None or lon is None:
        return
    with route_lock:
        has_route = len(route_pts) >= 2
    if not has_route:
        return

    now = time.time()
    with _gap_lock:
        last_t = _gap_state["t"]
        last_lat, last_lon = _gap_state["lat"], _gap_state["lon"]
        stale = (now - last_t) >= GAP_CHECK_MIN_S
        moved = last_lat is None or moved_enough(
            (last_lat, last_lon), (lat, lon), GAP_CHECK_MOVE_M)
        if not (stale and moved):
            return
        _gap_state["t"] = now
        _gap_state["lat"] = lat
        _gap_state["lon"] = lon

    threading.Thread(
        target=lambda: report_map_gaps(transport, ahead_km=BUFFER_AHEAD_KM,
                                       behind_km=BUFFER_BEHIND_KM),
        daemon=True).start()


def send_metrics(transport="WIFI"):
    """Report storage / state so the phone can display Pi metrics."""
    here = os.path.dirname(os.path.abspath(__file__))
    rpath = region_path()
    region_bytes = tiles.stats()["bytes"] if tiles else 0

    try:
        st = os.statvfs(here)
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize
    except Exception:
        disk_total = disk_free = 0

    # uptime + load are cheap and genuinely useful on a bike computer
    up = 0.0
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except Exception:
        pass
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        load1 = 0.0
    temp = 0.0
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = int(f.read().strip()) / 1000.0
    except Exception:
        pass

    with route_lock:
        n_route = len(route_pts)
        n_steps = len(route_steps)
        dest = route_meta.get("dest", "")
    roads = tiles.info() if tiles else "none"

    payload = {
        "type": "metrics",
        "region_bytes": region_bytes,
        "region_info": roads,
        "tiles": (tiles.stats()["tiles"] if tiles else 0),
        "tile_ways": (tiles.stats()["ways"] if tiles else 0),
        "disk_total": disk_total,
        "disk_free": disk_free,
        "route_pts": n_route,
        "steps": n_steps,
        "dest": dest,
        "map_mode": map_detail,
        "uptime_s": int(up),
        "load1": round(load1, 2),
        "cpu_c": round(temp, 1),
    }
    print(f"METRICS -> region {region_bytes//1024} KB, route {n_route} pts")
    reply(transport, payload)


def clear_map_storage():
    """Delete the downloaded region file and drop it from memory."""
    n = tiles.clear() if tiles else 0
    with nav_lock:
        nav["region_msg"] = "Map cleared"
    print(f"Map storage cleared ({n//1024} KB)")


def end_navigation():
    """
    Reset guidance, route, steps and trail. Imported map tiles are KEPT —
    re-downloading a whole pbf import would be absurd. Use clear_map to
    wipe tiles deliberately.
    """
    clear_navigation("ended from phone")
    map_recenter()
    with nav_lock:
        nav["region_msg"] = ""
    globals()["map_detail"] = "detailed"
    print("Navigation ended — back to idle")


# ══════════════════════════════════════════════════════════════════════════════
#  ICONS
# ══════════════════════════════════════════════════════════════════════════════
ICON_DIR = "icons"
_icon_cache = {}


def icon(name, size):
    """Load and cache a PNG icon scaled to `size` px square."""
    key = (name, size)
    if key in _icon_cache:
        return _icon_cache[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_DIR, name + ".png")
    surf = None
    try:
        if os.path.exists(path):
            img = pygame.image.load(path).convert_alpha()
            surf = pygame.transform.smoothscale(img, (size, size))
    except Exception as e:
        print(f"icon {name}: {e}")
    _icon_cache[key] = surf
    return surf


def blit_icon(screen, name, rect, fallback_draw=None):
    """Draw an icon centred in rect; fall back to a vector glyph if missing."""
    x, y, w, h = rect
    size = int(min(w, h) * 0.78)
    im = icon(name, size)
    if im is not None:
        screen.blit(im, (x + (w - size) // 2, y + (h - size) // 2))
        return True
    if fallback_draw:
        fallback_draw()
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  SCREEN ROTATION
# ══════════════════════════════════════════════════════════════════════════════
screen_rotation = 0          # 0, 90, 180, 270 (degrees, clockwise)
rot_lock = threading.Lock()
ROT_CHIP = None

ROT_CONF = "rotation.conf"


def load_rotation():
    global screen_rotation
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ROT_CONF)
    try:
        if os.path.exists(p):
            with open(p) as f:
                v = int(f.read().strip())
            if v in (0, 90, 180, 270):
                screen_rotation = v
                print(f"Rotation: {v}deg (saved)")
    except Exception:
        pass


def save_rotation(v):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ROT_CONF)
    try:
        with open(p, "w") as f:
            f.write(str(v))
    except Exception as e:
        print(f"rotation save failed: {e}")


def cycle_rotation():
    global screen_rotation
    with rot_lock:
        screen_rotation = (screen_rotation + 90) % 360
        v = screen_rotation
    save_rotation(v)
    print(f"Rotation -> {v}deg")
    return v


def rotate_touch(sx, sy):
    """
    Map a touch point from the PANEL's native frame into the ROTATED
    frame the UI was drawn in, so taps line up after rotating.
    """
    with rot_lock:
        r = screen_rotation
    W, H = SCREEN_W, SCREEN_H
    if r == 0:
        return sx, sy
    if r == 90:
        # UI drawn portrait then rotated -90 to fit the panel
        return sy, (W - 1 - sx)
    if r == 180:
        return (W - 1 - sx), (H - 1 - sy)
    # 270
    return (H - 1 - sy), sx


def frame_size():
    """Surface size to render into for the current rotation."""
    with rot_lock:
        r = screen_rotation
    return (SCREEN_H, SCREEN_W) if r in (90, 270) else (SCREEN_W, SCREEN_H)


# ══════════════════════════════════════════════════════════════════════════════
#  BLUETOOTH MANAGER (bluetoothctl)
# ══════════════════════════════════════════════════════════════════════════════
bt_state = {
    "powered": False,
    "discoverable": False,
    "devices": [],       # [{mac, name, paired, connected, trusted}]
    "busy": False,
    "msg": "",
}
bt_lock = threading.Lock()
bt_panel_open = False
BT_ROWS = []
BT_BTN_SCAN = None
BT_BTN_CLOSE = None
BT_BTN_POWER = None
BT_BTN_VIS = None
BT_BTN_FIX = None


def _btctl(*cmds, timeout=20):
    """Feed a sequence of commands to bluetoothctl on stdin."""
    payload = "\n".join(cmds) + "\nquit\n"
    try:
        r = subprocess.run(["bluetoothctl"], input=payload, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)



# ── Persistent pairing agent ────────────────────────────────────────────────
# bluetoothctl exits as soon as our command finishes, taking the pairing
# agent with it — which makes BlueZ reject any incoming pair request.
# Keep one long-lived agent alive for the life of the app.
_bt_agent_proc = None
_bt_agent_lock = threading.Lock()
BT_AGENT_CAP = "DisplayYesNo"   # phones need passkey confirmation


def bt_agent_alive():
    with _bt_agent_lock:
        p = _bt_agent_proc
    return p is not None and p.poll() is None


def bt_start_agent(capability=None):
    """
    Start a persistent pairing agent.

    Capability matters: phones normally do Secure Simple Pairing with a
    numeric passkey, so the agent must be able to CONFIRM it. Pure
    NoInputNoOutput makes BlueZ report "incorrect PIN or passkey".
    DisplayYesNo / KeyboardDisplay can confirm, so we prefer those.
    """
    global _bt_agent_proc
    if bt_agent_alive():
        return True

    cap = capability or BT_AGENT_CAP

    # bt-agent daemon (bluez-tools) — most reliable, auto-confirms
    try:
        rc, out, _ = _run(["which", "bt-agent"], timeout=5)
        if rc == 0 and out.strip():
            p = subprocess.Popen(
                ["bt-agent", "-c", cap, "-p", "/dev/null"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.6)
            if p.poll() is None:
                with _bt_agent_lock:
                    _bt_agent_proc = p
                print(f"Bluetooth: agent started (bt-agent, {cap})")
                return True
    except Exception as e:
        print(f"bt-agent: {e}")

    # fallback: keep bluetoothctl open and answer every prompt
    try:
        p = subprocess.Popen(
            ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        p.stdin.write(f"agent {cap}\ndefault-agent\npairable on\n")
        p.stdin.flush()
        with _bt_agent_lock:
            _bt_agent_proc = p

        def pump():
            try:
                for line in p.stdout:
                    low = line.lower()
                    ans = None
                    if "confirm passkey" in low or "yes/no" in low:
                        ans = "yes"
                    elif "authorize service" in low or "authorize?" in low:
                        ans = "yes"
                    elif "enter pin code" in low or "enter passkey" in low:
                        ans = "0000"
                    elif "request confirmation" in low:
                        ans = "yes"
                    if ans:
                        try:
                            p.stdin.write(ans + "\n")
                            p.stdin.flush()
                            print(f"Bluetooth: auto-answered '{ans}'")
                        except Exception:
                            pass
            except Exception:
                pass

        threading.Thread(target=pump, daemon=True).start()
        print(f"Bluetooth: agent started (bluetoothctl, {cap})")
        return True
    except Exception as e:
        print(f"agent start failed: {e}")
        return False


def bt_stop_agent():
    global _bt_agent_proc
    with _bt_agent_lock:
        p = _bt_agent_proc
        _bt_agent_proc = None
    if p:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass


def bt_cycle_agent_cap():
    """Rotate the agent capability — use when pairing keeps failing."""
    global BT_AGENT_CAP
    order = ["DisplayYesNo", "KeyboardDisplay", "NoInputNoOutput"]
    BT_AGENT_CAP = order[(order.index(BT_AGENT_CAP) + 1) % len(order)] \
        if BT_AGENT_CAP in order else order[0]
    bt_stop_agent()
    bt_start_agent(BT_AGENT_CAP)
    with bt_lock:
        bt_state["msg"] = f"Agent: {BT_AGENT_CAP}"
    print(f"Bluetooth: agent capability -> {BT_AGENT_CAP}")
    return BT_AGENT_CAP


def bt_fix_persistent():
    """
    One-time repairs that normally need a terminal:
      - clear the rfkill soft block (and make it stick across reboots)
      - set DiscoverableTimeout / PairableTimeout to 0 (never hide)
      - make sure bluetoothd is enabled and running
    Safe to run repeatedly.
    """
    changed = []

    # 1. rfkill unblock now
    _run(["rfkill", "unblock", "bluetooth"], timeout=8)
    _run(["rfkill", "unblock", "all"], timeout=8)

    # 2. persist the unblock via a tiny boot service
    unit = "/etc/systemd/system/motonav-rfkill.service"
    if not os.path.exists(unit):
        try:
            with open(unit, "w") as f:
                f.write(
                    "[Unit]\nDescription=Unblock Bluetooth for MotoNav\n"
                    "Before=bluetooth.service\n\n"
                    "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                    "ExecStart=/usr/sbin/rfkill unblock bluetooth\n\n"
                    "[Install]\nWantedBy=multi-user.target\n")
            _run(["systemctl", "daemon-reload"], timeout=15)
            _run(["systemctl", "enable", "motonav-rfkill.service"], timeout=15)
            changed.append("boot-unblock")
        except Exception as e:
            print(f"rfkill unit: {e}")

    # 3. never auto-hide
    conf = "/etc/bluetooth/main.conf"
    try:
        if os.path.exists(conf):
            with open(conf) as f:
                txt = f.read()
            orig = txt
            for key in ("DiscoverableTimeout", "PairableTimeout"):
                if re.search(rf"(?m)^\s*#?\s*{key}\s*=.*$", txt):
                    txt = re.sub(rf"(?m)^\s*#?\s*{key}\s*=.*$", f"{key} = 0", txt)
                else:
                    txt = re.sub(r"(?m)^\[General\]\s*$",
                                 f"[General]\n{key} = 0", txt, count=1)
            if txt != orig:
                with open(conf, "w") as f:
                    f.write(txt)
                _run(["systemctl", "restart", "bluetooth"], timeout=25)
                time.sleep(2)
                changed.append("timeouts")
    except Exception as e:
        print(f"main.conf: {e}")

    # 4. service enabled
    _run(["systemctl", "enable", "bluetooth"], timeout=15)
    _run(["systemctl", "start", "bluetooth"], timeout=15)

    # 5. bt-agent gives the most reliable persistent agent
    rc, out, _ = _run(["which", "bt-agent"], timeout=5)
    if rc != 0 or not out.strip():
        _run(["apt-get", "install", "-y", "bluez-tools"], timeout=120)
        changed.append("bluez-tools")

    return changed


def bt_ensure_on(repair=True):
    """Unblock, power up, start the agent and make the Pi visible."""
    if repair:
        bt_fix_persistent()
    _btctl("power on", "pairable on", "discoverable on", timeout=12)
    bt_start_agent()


def bt_set_power(on):
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = "Powering " + ("on..." if on else "off...")
    if on:
        _run(["rfkill", "unblock", "bluetooth"], timeout=8)
        _btctl("power on", "agent NoInputNoOutput", "default-agent",
               "pairable on", timeout=12)
    else:
        _btctl("power off", timeout=10)
    bt_refresh()
    with bt_lock:
        bt_state["msg"] = ""
        bt_state["busy"] = False


def bt_set_visible(on):
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = ("Making visible..." if on else "Hiding...")
    _btctl("power on", "discoverable " + ("on" if on else "off"),
           "pairable " + ("on" if on else "off"), timeout=12)
    bt_refresh()
    with bt_lock:
        bt_state["msg"] = ("Visible — pair from your phone" if on else "Hidden")
        bt_state["busy"] = False


def bt_refresh():
    """List known/visible devices and their pair/connect state."""
    with bt_lock:
        bt_state["busy"] = True

    rc, out, _ = _run(["bluetoothctl", "show"], timeout=8)
    powered = "Powered: yes" in out
    discoverable = "Discoverable: yes" in out

    rc, out, _ = _run(["bluetoothctl", "devices"], timeout=10)
    devs = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            devs.append({"mac": parts[1], "name": parts[2],
                         "paired": False, "connected": False, "trusted": False})

    # enrich with paired/connected flags
    rc, pout, _ = _run(["bluetoothctl", "paired-devices"], timeout=10)
    paired_macs = set()
    for line in pout.splitlines():
        p = line.strip().split(" ", 2)
        if len(p) >= 2 and p[0] == "Device":
            paired_macs.add(p[1])

    for d in devs:
        d["paired"] = d["mac"] in paired_macs
        if d["paired"]:
            rc, info, _ = _run(["bluetoothctl", "info", d["mac"]], timeout=8)
            d["connected"] = "Connected: yes" in info
            d["trusted"] = "Trusted: yes" in info

    devs.sort(key=lambda x: (not x["connected"], not x["paired"], x["name"].lower()))

    with bt_lock:
        bt_state["powered"] = powered
        bt_state["discoverable"] = discoverable
        bt_state["devices"] = devs[:8]
        bt_state["busy"] = False
        bt_state["msg"] = "" if devs else "No devices - tap SCAN"


def bt_scan():
    """Power on, run a discovery sweep, then refresh the list."""
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = "Scanning 12s..."
    _btctl("power on", "agent NoInputNoOutput", "default-agent",
           "pairable on", "discoverable on", "scan on", timeout=6)
    time.sleep(12)
    _run(["bluetoothctl", "scan", "off"], timeout=6)
    bt_refresh()
    with bt_lock:
        bt_state["msg"] = ""


def bt_pair(mac, name=""):
    """Pair + trust + connect. Uses NoInputNoOutput so no PIN entry is needed."""
    short = (name or mac)[:14]
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = f"Pairing {short}..."
    bt_start_agent()
    _btctl("power on", "pairable on", "discoverable on", timeout=8)

    # a stale/half-finished bond is a common cause of "incorrect PIN"
    _run(["bluetoothctl", "remove", mac], timeout=12)
    time.sleep(1.0)

    rc, out, err = _run(["bluetoothctl", "pair", mac], timeout=45)
    ok = ("Pairing successful" in out) or ("already" in (out + err).lower())

    if ok:
        with bt_lock:
            bt_state["msg"] = f"Trusting {short}..."
        _run(["bluetoothctl", "trust", mac], timeout=15)
        _run(["bluetoothctl", "connect", mac], timeout=25)
        msg = "Paired - now connect from the phone app"
    else:
        line = ""
        for l in out.splitlines():
            if "Failed" in l or "org.bluez" in l:
                line = l.strip()
                break
        msg = (line or "Pairing failed - confirm on the phone")[:36]

    with bt_lock:
        bt_state["msg"] = msg
        bt_state["busy"] = False
    bt_refresh()


def bt_remove(mac):
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = "Removing..."
    _run(["bluetoothctl", "remove", mac], timeout=15)
    bt_refresh()
    with bt_lock:
        bt_state["msg"] = "Removed"
        bt_state["busy"] = False


def bt_forget_all():
    """Remove every bond — clears the half-paired state that breaks re-pairing."""
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = "Forgetting all devices..."
    rc, out, _ = _run(["bluetoothctl", "paired-devices"], timeout=12)
    n = 0
    for line in out.splitlines():
        p = line.strip().split(" ", 2)
        if len(p) >= 2 and p[0] == "Device":
            _run(["bluetoothctl", "remove", p[1]], timeout=12)
            n += 1
    bt_refresh()
    with bt_lock:
        bt_state["msg"] = f"Forgot {n} — now pair from the phone"
        bt_state["busy"] = False



def bt_deep_reset():
    """
    Nuclear option for "incorrect key" pairing failures.

    A stale bond on the Pi is the usual cause: the phone was un-paired but
    BlueZ still holds the old link key on disk, so the fresh key the phone
    offers no longer matches. bluetoothctl remove alone often leaves the
    on-disk record, so we delete it and restart bluetoothd.
    """
    with bt_lock:
        bt_state["busy"] = True
        bt_state["msg"] = "Deep reset: clearing keys..."

    bt_stop_agent()

    # 1. remove every known bond through the normal API
    rc, out, _ = _run(["bluetoothctl", "paired-devices"], timeout=12)
    for line in out.splitlines():
        p = line.strip().split(" ", 2)
        if len(p) >= 2 and p[0] == "Device":
            _run(["bluetoothctl", "remove", p[1]], timeout=10)

    # 2. delete the on-disk key store for this adapter
    removed = 0
    try:
        base = "/var/lib/bluetooth"
        if os.path.isdir(base):
            for adapter in os.listdir(base):
                adir = os.path.join(base, adapter)
                if not os.path.isdir(adir):
                    continue
                for entry in os.listdir(adir):
                    edir = os.path.join(adir, entry)
                    # device dirs are named by MAC (AA:BB:CC:DD:EE:FF)
                    if os.path.isdir(edir) and entry.count(":") == 5:
                        try:
                            for f in os.listdir(edir):
                                os.remove(os.path.join(edir, f))
                            os.rmdir(edir)
                            removed += 1
                        except Exception as e:
                            print(f"key rm {entry}: {e}")
    except Exception as e:
        print(f"key store: {e}")

    with bt_lock:
        bt_state["msg"] = "Restarting Bluetooth..."

    # 3. restart the daemon so it forgets cached keys
    _run(["systemctl", "restart", "bluetooth"], timeout=30)
    time.sleep(3)

    # 4. bring it back up, visible, with a confirming agent
    _run(["rfkill", "unblock", "bluetooth"], timeout=8)
    _btctl("power on", "pairable on", "discoverable on", timeout=12)
    bt_start_agent()

    bt_refresh()
    with bt_lock:
        bt_state["msg"] = f"Cleared {removed} key(s) — pair from the phone now"
        bt_state["busy"] = False
    print(f"Bluetooth: deep reset done, removed {removed} key dirs")


def bt_worker(action, mac=None, name=""):
    fn = {"scan": bt_scan,
          "refresh": bt_refresh,
          "fix": (lambda: (bt_ensure_on(True), bt_refresh())),
          "agent": (lambda: (bt_cycle_agent_cap(), bt_refresh())),
          "forget_all": (lambda: bt_forget_all()),
          "deep_reset": (lambda: bt_deep_reset()),
          "power_on": (lambda: bt_set_power(True)),
          "power_off": (lambda: bt_set_power(False)),
          "visible_on": (lambda: bt_set_visible(True)),
          "visible_off": (lambda: bt_set_visible(False)),
          "pair": (lambda: bt_pair(mac, name)),
          "remove": (lambda: bt_remove(mac))}[action]
    threading.Thread(target=fn, daemon=True).start()


def draw_bt_panel(screen, fonts, sc):
    """Bluetooth device list. Tap a device to pair; tap a paired one to forget."""
    global BT_ROWS, BT_BTN_SCAN, BT_BTN_CLOSE
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H

    pad = int(14 * sc)
    px, py = pad, pad
    pw, ph = W - pad * 2, H - pad * 2
    pygame.draw.rect(screen, C["PANEL"], (px, py, pw, ph), border_radius=6)
    pygame.draw.rect(screen, C["MAPEDGE"], (px, py, pw, ph), 2, border_radius=6)

    with bt_lock:
        devs = list(bt_state["devices"])
        msg = bt_state["msg"]
        busy = bt_state["busy"]
        powered = bt_state["powered"]
        discoverable = bt_state.get("discoverable", False)

    text(screen, "BLUETOOTH", f_sm, C["TEXT"], px + 10, py + 7)
    text(screen, f"agent: {BT_AGENT_CAP}", f_lbl, C["MUTED"],
         px + int(112 * sc), py + int(11 * sc))
    if powered:
        st = "VISIBLE" if discoverable else "on"
        if discoverable and not bt_agent_alive():
            st = "NO AGENT"
        stc = C["GREEN"] if bt_agent_alive() else C["WARN"]
    else:
        st = "OFF"
        stc = C["RED"]
    text(screen, st, f_lbl, stc, px + pw - 10, py + 10, "right")
    pygame.draw.line(screen, C["BORDER"], (px + 8, py + int(26 * sc)),
                     (px + pw - 8, py + int(26 * sc)), 1)

    BT_ROWS = []
    row_h = int(24 * sc)
    ry0 = py + int(30 * sc)
    max_rows = max(1, (ph - int(72 * sc)) // row_h)
    for i, dv in enumerate(devs[:max_rows]):
        r = (px + 8, ry0 + i * row_h, pw - 16, row_h - 2)
        if dv["connected"]:
            pygame.draw.rect(screen, C["ROUTEGLOW"], r, border_radius=3)
        elif dv["paired"]:
            pygame.draw.rect(screen, C["PANEL2"], r, border_radius=3)
        col = C["TEXT"] if (dv["paired"] or dv["connected"]) else C["MUTED"]
        text(screen, dv["name"][:24], f_lbl, col, r[0] + 8, r[1] + int(6 * sc))
        tag = "CONNECTED" if dv["connected"] else ("PAIRED" if dv["paired"] else "TAP TO PAIR")
        tcol = C["GREEN"] if dv["connected"] else (C["ROUTE"] if dv["paired"] else C["MUTED"])
        text(screen, tag, f_lbl, tcol, r[0] + r[2] - 6, r[1] + int(6 * sc), "right")
        BT_ROWS.append((r, dv["mac"], dv["name"], dv["paired"]))

    if not devs:
        if not powered:
            text(screen, "Bluetooth is OFF", f_lbl, C["RED"],
                 px + pw // 2, ry0 + int(14 * sc), "center")
            text(screen, "Tap PWR OFF below to turn it on", f_lbl, C["MUTED"],
                 px + pw // 2, ry0 + int(30 * sc), "center")
        else:
            text(screen, "No devices — tap SCAN", f_lbl, C["MUTED"],
                 px + pw // 2, ry0 + int(14 * sc), "center")
            text(screen, "Pi is visible — pair from your phone", f_lbl, C["MUTED"],
                 px + pw // 2, ry0 + int(30 * sc), "center")

    if msg:
        text(screen, msg, f_lbl, C["MUTED"] if busy else C["WARN"],
             px + pw // 2, py + ph - int(40 * sc), "center")

    global BT_BTN_POWER, BT_BTN_VIS
    bh = int(24 * sc)
    by_ = py + ph - bh - int(8 * sc)
    gap = int(6 * sc)
    global BT_BTN_FIX
    bw = (pw - 20 - gap * 4) // 5
    x0 = px + 10
    BT_BTN_POWER = (x0, by_, bw, bh)
    BT_BTN_VIS   = (x0 + (bw + gap), by_, bw, bh)
    BT_BTN_SCAN  = (x0 + (bw + gap) * 2, by_, bw, bh)
    BT_BTN_FIX   = (x0 + (bw + gap) * 3, by_, bw, bh)
    BT_BTN_CLOSE = (x0 + (bw + gap) * 4, by_, bw, bh)

    pwr_label = "ON" if powered else "OFF"
    pwr_col = C["GREEN"] if powered else C["RED"]
    vis_label = "VISIBLE" if discoverable else "HIDDEN"
    vis_col = C["GREEN"] if discoverable else C["MUTED"]

    for rect, label, col, filled in (
            (BT_BTN_POWER, "PWR " + pwr_label, pwr_col, powered),
            (BT_BTN_VIS, vis_label, vis_col, discoverable),
            (BT_BTN_SCAN, "SCAN", C["ROUTE"], False),
            (BT_BTN_FIX, "RESET", C["WARN"], False),
            (BT_BTN_CLOSE, "CLOSE", C["MUTED"], False)):
        pygame.draw.rect(screen, col if filled else C["PANEL2"], rect, border_radius=3)
        pygame.draw.rect(screen, col, rect, 1, border_radius=3)
        text(screen, label, f_lbl, C["PANEL"] if filled else col,
             rect[0] + rect[2] // 2, rect[1] + int(7 * sc), "center")


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS MENU + IN-APP TOUCH CALIBRATION
# ══════════════════════════════════════════════════════════════════════════════
settings_open = False
SET_CHIP = None
SET_ROWS = []            # [(rect, key)]
SET_BTN_CLOSE = None

cal_active = False
cal_step = 0
cal_samples = []
CAL_TARGETS = [("TOP-LEFT", 0.10, 0.12), ("TOP-RIGHT", 0.90, 0.12),
               ("BOTTOM-LEFT", 0.10, 0.88), ("BOTTOM-RIGHT", 0.90, 0.88)]
cal_msg = ""


def save_touch_cal(swap, inv_x, inv_y, x_lo, x_hi, y_lo, y_hi):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch_cal.conf")
    try:
        with open(path, "w") as f:
            f.write(f"swap={int(swap)}\ninvert_x={int(inv_x)}\ninvert_y={int(inv_y)}\n")
            f.write(f"x_min={int(x_lo)}\nx_max={int(x_hi)}\n")
            f.write(f"y_min={int(y_lo)}\ny_max={int(y_hi)}\n")
        return True
    except Exception as e:
        print(f"cal save failed: {e}")
        return False


def apply_calibration():
    """Work out swap/invert/ranges from the 4 corner samples and apply live."""
    global TOUCH_SWAP_XY, TOUCH_INVERT_X, TOUCH_INVERT_Y
    global TOUCH_X_MIN, TOUCH_X_MAX, TOUCH_Y_MIN, TOUCH_Y_MAX, cal_msg
    if len(cal_samples) < 4:
        cal_msg = "Calibration incomplete"
        return False
    tl, tr, bl, br = cal_samples[0], cal_samples[1], cal_samples[2], cal_samples[3]

    dx_h = abs((tr[0] - tl[0]) + (br[0] - bl[0])) / 2.0
    dy_h = abs((tr[1] - tl[1]) + (br[1] - bl[1])) / 2.0
    swap = dy_h > dx_h

    if swap:
        left_v, right_v = (tl[1] + bl[1]) / 2.0, (tr[1] + br[1]) / 2.0
        top_v, bot_v = (tl[0] + tr[0]) / 2.0, (bl[0] + br[0]) / 2.0
    else:
        left_v, right_v = (tl[0] + bl[0]) / 2.0, (tr[0] + br[0]) / 2.0
        top_v, bot_v = (tl[1] + tr[1]) / 2.0, (bl[1] + br[1]) / 2.0

    inv_x = left_v > right_v
    inv_y = top_v > bot_v
    x_lo, x_hi = sorted([left_v, right_v])
    y_lo, y_hi = sorted([top_v, bot_v])

    # the taps were at 10%/90%, so extrapolate to full screen edges
    sx = (x_hi - x_lo) / 0.8
    x_lo, x_hi = x_lo - sx * 0.1, x_hi + sx * 0.1
    sy = (y_hi - y_lo) / 0.76
    y_lo, y_hi = y_lo - sy * 0.12, y_hi + sy * 0.12

    TOUCH_SWAP_XY, TOUCH_INVERT_X, TOUCH_INVERT_Y = swap, inv_x, inv_y
    TOUCH_X_MIN, TOUCH_X_MAX = int(x_lo), int(x_hi)
    TOUCH_Y_MIN, TOUCH_Y_MAX = int(y_lo), int(y_hi)
    ok = save_touch_cal(swap, inv_x, inv_y, x_lo, x_hi, y_lo, y_hi)
    cal_msg = "Saved — touch calibrated" if ok else "Applied (save failed)"
    print(f"Calibration: swap={swap} invX={inv_x} invY={inv_y} "
          f"x={int(x_lo)}..{int(x_hi)} y={int(y_lo)}..{int(y_hi)}")
    return True


def draw_calibration(screen, fonts, sc):
    """Full-screen crosshair targets."""
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H
    screen.fill(C["PANEL"])

    if cal_step >= len(CAL_TARGETS):
        text(screen, cal_msg or "Done", f_sm, C["GREEN"], W // 2, H // 2 - int(10 * sc), "center")
        text(screen, "Tap anywhere to finish", f_lbl, C["MUTED"], W // 2, H // 2 + int(14 * sc), "center")
        return

    label, fx, fy = CAL_TARGETS[cal_step]
    tx_, ty_ = int(W * fx), int(H * fy)

    text(screen, "TOUCH CALIBRATION", f_sm, C["TEXT"], W // 2, int(24 * sc), "center")
    text(screen, f"Tap the {label} crosshair", f_lbl, C["MUTED"], W // 2, int(46 * sc), "center")
    text(screen, f"{cal_step + 1} of {len(CAL_TARGETS)}", f_lbl, C["MUTED"],
         W // 2, H - int(26 * sc), "center")

    r_ = int(16 * sc)
    pygame.draw.circle(screen, C["ROUTE"], (tx_, ty_), r_, 2)
    pygame.draw.circle(screen, C["ROUTE"], (tx_, ty_), 3)
    pygame.draw.line(screen, C["ROUTE"], (tx_ - r_ - 8, ty_), (tx_ - 5, ty_), 2)
    pygame.draw.line(screen, C["ROUTE"], (tx_ + 5, ty_), (tx_ + r_ + 8, ty_), 2)
    pygame.draw.line(screen, C["ROUTE"], (tx_, ty_ - r_ - 8), (tx_, ty_ - 5), 2)
    pygame.draw.line(screen, C["ROUTE"], (tx_, ty_ + 5), (tx_, ty_ + r_ + 8), 2)


def draw_settings(screen, fonts, sc):
    """Settings menu overlay."""
    global SET_ROWS, SET_BTN_CLOSE
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H

    veil = pygame.Surface((W, H), pygame.SRCALPHA)
    veil.fill((0, 0, 0, 150))
    screen.blit(veil, (0, 0))

    bw, bh = int(290 * sc), int(252 * sc)
    bx, by_ = (W - bw) // 2, (H - bh) // 2
    pygame.draw.rect(screen, C["PANEL"], (bx, by_, bw, bh), border_radius=6)
    pygame.draw.rect(screen, C["MAPEDGE"], (bx, by_, bw, bh), 2, border_radius=6)

    text(screen, "SETTINGS", f_sm, C["TEXT"], bx + int(12 * sc), by_ + int(10 * sc))
    pygame.draw.line(screen, C["BORDER"], (bx + 8, by_ + int(30 * sc)),
                     (bx + bw - 8, by_ + int(30 * sc)), 1)

    with wifi_lock:
        w_ssid = wifi_state["ssid"]
        w_ip = wifi_state["ip"]
    with theme_lock:
        tmode = {"auto": "AUTO", "day": "DAY", "night": "NIGHT"}[theme_mode]

    with bt_lock:
        bt_conn = next((d["name"] for d in bt_state["devices"] if d["connected"]), None)
        bt_paired = sum(1 for d in bt_state["devices"] if d["paired"])
    bt_val = bt_conn if bt_conn else (f"{bt_paired} paired" if bt_paired else "not paired")

    map_val = tiles.info() if tiles else "none"

    rows = [
        ("map", "Offline map", map_val),
        ("wifi", "Wi-Fi", w_ssid if w_ssid else "not connected"),
        ("bt", "Bluetooth", bt_val),
        ("theme", "Theme", tmode),
        ("cal", "Calibrate touch", "tap to start"),
    ]
    SET_ROWS = []
    rh_ = int(34 * sc)
    y0 = by_ + int(36 * sc)
    for i, (key, label, value) in enumerate(rows):
        r = (bx + 8, y0 + i * rh_, bw - 16, rh_ - int(4 * sc))
        pygame.draw.rect(screen, C["PANEL2"], r, border_radius=4)
        pygame.draw.rect(screen, C["BORDER"], r, 1, border_radius=4)
        text(screen, label, f_lbl, C["TEXT"], r[0] + int(10 * sc), r[1] + int(9 * sc))
        text(screen, str(value)[:18], f_lbl, C["ROUTE"],
             r[0] + r[2] - int(10 * sc), r[1] + int(9 * sc), "right")
        SET_ROWS.append((r, key))

    bwid, bhei = int(80 * sc), int(26 * sc)
    SET_BTN_CLOSE = (bx + bw - bwid - int(10 * sc), by_ + bh - bhei - int(10 * sc), bwid, bhei)
    pygame.draw.rect(screen, C["PANEL2"], SET_BTN_CLOSE, border_radius=4)
    pygame.draw.rect(screen, C["MUTED"], SET_BTN_CLOSE, 1, border_radius=4)
    text(screen, "CLOSE", f_lbl, C["MUTED"],
         SET_BTN_CLOSE[0] + SET_BTN_CLOSE[2] // 2, SET_BTN_CLOSE[1] + int(8 * sc), "center")
    if w_ip:
        text(screen, f"IP {w_ip}", f_lbl, C["MUTED"], bx + int(12 * sc),
             by_ + bh - int(22 * sc))


# ══════════════════════════════════════════════════════════════════════════════
#  POWER (shutdown / reboot)
# ══════════════════════════════════════════════════════════════════════════════
power_confirm = False       # confirmation dialog visible
power_msg = ""              # shown while shutting down
PWR_CHIP = None
PWR_BTN_OFF = None
PWR_BTN_REBOOT = None
PWR_BTN_CANCEL = None


def do_shutdown():
    global power_msg
    power_msg = "SHUTTING DOWN..."
    print("Shutdown requested")
    time.sleep(1.2)          # let the message render
    _run(["sync"], timeout=10)
    for cmd in (["systemctl", "poweroff"], ["shutdown", "-h", "now"], ["poweroff"]):
        rc, _, _ = _run(cmd, timeout=10)
        if rc == 0:
            return
    power_msg = "SHUTDOWN FAILED (need sudo?)"


def do_reboot():
    global power_msg
    power_msg = "REBOOTING..."
    print("Reboot requested")
    time.sleep(1.2)
    _run(["sync"], timeout=10)
    for cmd in (["systemctl", "reboot"], ["shutdown", "-r", "now"], ["reboot"]):
        rc, _, _ = _run(cmd, timeout=10)
        if rc == 0:
            return
    power_msg = "REBOOT FAILED (need sudo?)"


def draw_power_dialog(screen, fonts, sc):
    """Confirmation dialog so a stray tap can't kill navigation."""
    global PWR_BTN_OFF, PWR_BTN_REBOOT, PWR_BTN_CANCEL
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H

    veil = pygame.Surface((W, H), pygame.SRCALPHA)
    veil.fill((0, 0, 0, 165))
    screen.blit(veil, (0, 0))

    bw, bh = int(300 * sc), int(150 * sc)
    bx, by_ = (W - bw) // 2, (H - bh) // 2
    pygame.draw.rect(screen, C["PANEL"], (bx, by_, bw, bh), border_radius=6)
    pygame.draw.rect(screen, C["RED"], (bx, by_, bw, bh), 2, border_radius=6)

    if power_msg:
        text(screen, power_msg, f_sm, C["RED"], W // 2, by_ + bh // 2 - int(8 * sc), "center")
        return

    text(screen, "POWER", f_sm, C["TEXT"], W // 2, by_ + int(14 * sc), "center")
    text(screen, "Shut down the Pi?", f_lbl, C["MUTED"], W // 2, by_ + int(38 * sc), "center")
    text(screen, "Always power off before unplugging", f_lbl, C["MUTED"],
         W // 2, by_ + int(52 * sc), "center")

    btn_w, btn_h = int(84 * sc), int(30 * sc)
    gap = int(10 * sc)
    total = btn_w * 3 + gap * 2
    x0 = (W - total) // 2
    y0 = by_ + bh - btn_h - int(14 * sc)

    PWR_BTN_OFF = (x0, y0, btn_w, btn_h)
    PWR_BTN_REBOOT = (x0 + btn_w + gap, y0, btn_w, btn_h)
    PWR_BTN_CANCEL = (x0 + (btn_w + gap) * 2, y0, btn_w, btn_h)

    for rect, label, col, filled in (
            (PWR_BTN_OFF, "SHUT DOWN", C["RED"], True),
            (PWR_BTN_REBOOT, "REBOOT", C["WARN"], False),
            (PWR_BTN_CANCEL, "CANCEL", C["MUTED"], False)):
        pygame.draw.rect(screen, col if filled else C["PANEL2"], rect, border_radius=4)
        pygame.draw.rect(screen, col, rect, 1, border_radius=4)
        text(screen, label, f_lbl, C["PANEL"] if filled else col,
             rect[0] + rect[2] // 2, rect[1] + int(9 * sc), "center")


# ══════════════════════════════════════════════════════════════════════════════
#  ON-SCREEN KEYBOARD (for WiFi passwords)
# ══════════════════════════════════════════════════════════════════════════════
kb_open = False
kb_target_ssid = ""
kb_buffer = ""
kb_shift = False
kb_page = 0            # 0 = letters, 1 = numbers/symbols
KB_KEYS = []           # [(rect, label, action)]
KB_SHOW = False        # reveal password text
KB_EYE = None          # show/hide toggle rect

KB_ROWS_LOWER = [
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
]
KB_ROWS_SYM = [
    "1234567890",
    "@#$%&*-+()",
    "!\"':;/?,.",
]


def kb_layout(sc):
    """Build key rects for the current page. Returns list of (rect,label,action)."""
    W, H = SCREEN_W, SCREEN_H
    keys = []
    pad = int(3 * sc)
    kb_h = int(112 * sc)
    top = H - kb_h
    rows = KB_ROWS_SYM if kb_page else KB_ROWS_LOWER
    row_h = int(24 * sc)

    for ri, row in enumerate(rows):
        n = len(row)
        kw = (W - pad * (n + 1)) // n
        x0 = (W - (kw * n + pad * (n - 1))) // 2
        y = top + pad + ri * (row_h + pad)
        for ci, ch in enumerate(row):
            label = ch.upper() if (kb_shift and not kb_page) else ch
            keys.append(((x0 + ci * (kw + pad), y, kw, row_h), label, ("char", label)))

    # bottom function row
    y = top + pad + 3 * (row_h + pad)
    fw = (W - pad * 6) // 5
    specs = [
        ("SHIFT" if not kb_page else "abc", ("shift",) if not kb_page else ("page",)),
        ("123" if not kb_page else "ABC", ("page",)),
        ("SPACE", ("char", " ")),
        ("DEL", ("del",)),
        ("JOIN", ("ok",)),
    ]
    for i, (label, action) in enumerate(specs):
        keys.append(((pad + i * (fw + pad), y, fw, row_h), label, action))
    return keys


def draw_keyboard(screen, fonts, sc):
    """Draw password field + keyboard."""
    global KB_KEYS
    f_big, f_med, f_sm, f_unit, f_tile, f_lbl = fonts
    W, H = SCREEN_W, SCREEN_H

    # dim the background
    veil = pygame.Surface((W, H), pygame.SRCALPHA)
    veil.fill((0, 0, 0, 150))
    screen.blit(veil, (0, 0))

    kb_h = int(112 * sc)
    top = H - kb_h

    # ── password field above the keyboard ──
    fh = int(30 * sc)
    fy = top - fh - int(26 * sc)
    pygame.draw.rect(screen, C["PANEL"], (int(8 * sc), fy, W - int(16 * sc), fh), border_radius=4)
    pygame.draw.rect(screen, C["ROUTE"], (int(8 * sc), fy, W - int(16 * sc), fh), 1, border_radius=4)
    text(screen, f"PASSWORD FOR {kb_target_ssid[:18]}", f_lbl, C["MUTED"],
         int(10 * sc), fy - int(15 * sc))
    shown = kb_buffer if KB_SHOW else "*" * len(kb_buffer)
    if not shown:
        shown = "_"
    text(screen, shown[-26:], f_sm, C["TEXT"], int(14 * sc), fy + int(7 * sc))
    # eye toggle
    global KB_EYE
    KB_EYE = (W - int(52 * sc), fy + int(4 * sc), int(42 * sc), fh - int(8 * sc))
    pygame.draw.rect(screen, C["PANEL2"], KB_EYE, border_radius=3)
    pygame.draw.rect(screen, C["MUTED"], KB_EYE, 1, border_radius=3)
    text(screen, "HIDE" if KB_SHOW else "SHOW", f_lbl, C["MUTED"],
         KB_EYE[0] + KB_EYE[2] // 2, KB_EYE[1] + int(6 * sc), "center")

    with wifi_lock:
        msg = wifi_state["msg"]
    if msg:
        text(screen, msg, f_lbl, C["WARN"], W // 2, fy - int(15 * sc), "center")

    # ── keyboard plate ──
    pygame.draw.rect(screen, C["PANEL"], (0, top, W, kb_h))
    pygame.draw.line(screen, C["BORDER"], (0, top), (W, top), 1)

    KB_KEYS = kb_layout(sc)
    for rect, label, action in KB_KEYS:
        kind = action[0]
        if kind == "ok":
            bg, fg = C["ROUTE"], C["PANEL"]
        elif kind in ("del", "shift", "page"):
            bg, fg = C["PANEL2"], C["TEXT"]
        else:
            bg, fg = C["PANEL2"], C["TEXT"]
        pygame.draw.rect(screen, bg, rect, border_radius=3)
        pygame.draw.rect(screen, C["BORDER"], rect, 1, border_radius=3)
        text(screen, label, f_lbl, fg, rect[0] + rect[2] // 2,
             rect[1] + rect[3] // 2 - int(6 * sc), "center")


def wifi_connect_pw(ssid, password):
    """Join a network using a typed password."""
    with wifi_lock:
        wifi_state["busy"] = True
        wifi_state["msg"] = f"Joining {ssid[:14]}..."
    rc, out, err = _run(
        ["nmcli", "dev", "wifi", "connect", ssid, "password", password], timeout=40)
    with wifi_lock:
        wifi_state["busy"] = False
        wifi_state["msg"] = "Connected" if rc == 0 else (err or "Wrong password?")[:34]
    wifi_refresh()
    return rc == 0


# ══════════════════════════════════════════════════════════════════════════════
#  TOUCH (read /dev/input/event* directly — SDL dummy driver has no input)
# ══════════════════════════════════════════════════════════════════════════════
touch_tap = []          # appended (x, y) on each tap
touch_raw = []          # appended (raw_x, raw_y) on each tap (for calibration)
touch_events = []       # ("down"|"move"|"up", x, y)
touch_lock = threading.Lock()

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
BTN_TOUCH = 0x14a


def _find_touch_devices():
    devs = []
    try:
        with open("/proc/bus/input/devices") as f:
            block = {}
            for line in f:
                line = line.strip()
                if line.startswith("N: Name="):
                    block["name"] = line.split("=", 1)[1].strip('"')
                elif line.startswith("H: Handlers="):
                    block["handlers"] = line.split("=", 1)[1]
                elif not line:
                    if block.get("handlers") and "event" in block["handlers"]:
                        nm = block.get("name", "").lower()
                        if any(k in nm for k in ("touch", "ads7846", "ft6", "stmpe", "goodix")):
                            for h in block["handlers"].split():
                                if h.startswith("event"):
                                    devs.append("/dev/input/" + h)
                    block = {}
    except Exception:
        pass
    return devs


# Calibration (overridden by touch_cal.conf if present)
TOUCH_SWAP_XY = True        # rotate=90 display -> raw axes are swapped
TOUCH_INVERT_X = False
TOUCH_INVERT_Y = True
TOUCH_X_MIN, TOUCH_X_MAX = 200, 3900
TOUCH_Y_MIN, TOUCH_Y_MAX = 200, 3900


def load_touch_cal():
    """Load touch_cal.conf written by touchcal.py, if it exists."""
    global TOUCH_SWAP_XY, TOUCH_INVERT_X, TOUCH_INVERT_Y
    global TOUCH_X_MIN, TOUCH_X_MAX, TOUCH_Y_MIN, TOUCH_Y_MAX
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "touch_cal.conf")
    if not os.path.exists(path):
        print("Touch: no touch_cal.conf (using defaults) — run: sudo python3 touchcal.py")
        return
    try:
        vals = {}
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    vals[k] = int(v)
        TOUCH_SWAP_XY = bool(vals.get("swap", TOUCH_SWAP_XY))
        TOUCH_INVERT_X = bool(vals.get("invert_x", TOUCH_INVERT_X))
        TOUCH_INVERT_Y = bool(vals.get("invert_y", TOUCH_INVERT_Y))
        TOUCH_X_MIN = vals.get("x_min", TOUCH_X_MIN)
        TOUCH_X_MAX = vals.get("x_max", TOUCH_X_MAX)
        TOUCH_Y_MIN = vals.get("y_min", TOUCH_Y_MIN)
        TOUCH_Y_MAX = vals.get("y_max", TOUCH_Y_MAX)
        print(f"Touch: loaded calibration (swap={TOUCH_SWAP_XY}, "
              f"invX={TOUCH_INVERT_X}, invY={TOUCH_INVERT_Y})")
    except Exception as e:
        print(f"Touch: bad touch_cal.conf ({e}) — using defaults")


def map_touch(raw_x, raw_y):
    """Convert raw controller coords to screen pixels."""
    rx, ry = (raw_y, raw_x) if TOUCH_SWAP_XY else (raw_x, raw_y)
    span_x = max(1, TOUCH_X_MAX - TOUCH_X_MIN)
    span_y = max(1, TOUCH_Y_MAX - TOUCH_Y_MIN)
    fx = (rx - TOUCH_X_MIN) / span_x
    fy = (ry - TOUCH_Y_MIN) / span_y
    if TOUCH_INVERT_X:
        fx = 1.0 - fx
    if TOUCH_INVERT_Y:
        fy = 1.0 - fy
    sx = int(fx * SCREEN_W)
    sy = int(fy * SCREEN_H)
    return (max(0, min(SCREEN_W - 1, sx)), max(0, min(SCREEN_H - 1, sy)))


def touch_thread():
    """Read raw touch events; convert to screen coords and record taps."""
    load_touch_cal()
    devs = _find_touch_devices()
    if not devs:
        print("Touch: no touchscreen device found")
        return
    dev = devs[0]
    print(f"Touch: reading {dev}")
    fmt = "llHHi"
    sz = struct.calcsize(fmt)
    raw_x = raw_y = None
    down_x = down_y = None
    moved = False
    debug = "--touchdebug" in ARGS
    try:
        with open(dev, "rb") as f:
            while True:
                data = f.read(sz)
                if not data or len(data) < sz:
                    continue
                _, _, etype, code, value = struct.unpack(fmt, data)
                if etype == EV_ABS:
                    if code in (ABS_X, ABS_MT_POSITION_X):
                        raw_x = value
                    elif code in (ABS_Y, ABS_MT_POSITION_Y):
                        raw_y = value
                elif etype == EV_KEY and code == BTN_TOUCH:
                    if value == 1:
                        down_x, down_y = raw_x, raw_y
                        if raw_x is not None:
                            sx, sy = rotate_touch(*map_touch(raw_x, raw_y))
                            with touch_lock:
                                touch_events.append(("down", sx, sy))
                        moved = False
                    else:
                        if raw_x is not None and raw_y is not None:
                            sx, sy = map_touch(raw_x, raw_y)
                            sx, sy = rotate_touch(sx, sy)
                            with touch_lock:
                                touch_events.append(("up", sx, sy))
                                # a tap is a release that didn't drag far
                                if not moved:
                                    touch_tap.append((sx, sy))
                                    touch_raw.append((down_x if down_x else raw_x,
                                                      down_y if down_y else raw_y))
                            if debug:
                                kind = "drag" if moved else "tap"
                                print(f"TOUCH {kind} raw=({raw_x},{raw_y}) -> ({sx},{sy})")
                        raw_x = raw_y = None
                        down_x = down_y = None
                elif etype == EV_SYN:
                    # emit motion while the finger is down
                    if down_x is not None and raw_x is not None and raw_y is not None:
                        sx, sy = rotate_touch(*map_touch(raw_x, raw_y))
                        dx0, dy0 = rotate_touch(*map_touch(down_x, down_y))
                        if abs(sx - dx0) > 6 or abs(sy - dy0) > 6:
                            moved = True
                        with touch_lock:
                            touch_events.append(("move", sx, sy))
    except PermissionError:
        print("Touch: permission denied (run with sudo)")
    except Exception as e:
        print(f"Touch: stopped ({e})")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def make_fonts():
    sc = SCREEN_H / 320.0

    def fs(n):
        return max(8, int(n * sc))

    names = ["dejavusansmono", "dejavusans", "freesans", None]
    for nm in names:
        try:
            if nm:
                return (
                    pygame.font.SysFont(nm, fs(52), bold=True),
                    pygame.font.SysFont(nm, fs(24), bold=True),
                    pygame.font.SysFont(nm, fs(15), bold=True),
                    pygame.font.SysFont(nm, fs(16), bold=True),
                    pygame.font.SysFont(nm, fs(15), bold=True),
                    pygame.font.SysFont(nm, fs(10), bold=True),
                )
            return (
                pygame.font.Font(None, fs(60)), pygame.font.Font(None, fs(30)),
                pygame.font.Font(None, fs(20)), pygame.font.Font(None, fs(21)),
                pygame.font.Font(None, fs(20)), pygame.font.Font(None, fs(14)),
            )
        except Exception:
            continue


def init_screen():
    """
    Render offscreen (SDL 'dummy') and push pixels to the framebuffer
    ourselves. SDL2 on Bookworm has no working fbcon/kmsdrm for SPI panels.
    """
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.quit()
    pygame.display.init()
    pygame.display.set_mode((1, 1))
    fb = FBWriter(FB_DEV)
    surface = pygame.Surface((SCREEN_W, SCREEN_H))
    return surface, fb


def main():
    global theme_mode, wifi_panel_open
    global kb_open, kb_target_ssid, kb_buffer, kb_shift, kb_page, KB_SHOW
    global power_confirm, settings_open, cal_active, cal_step, bt_panel_open
    print("=" * 46)
    print("  MotoNav — starting")
    print(f"  Framebuffer : {FB_DEV}")
    print(f"  Resolution  : {SCREEN_W}x{SCREEN_H}")
    print(f"  Mode        : {'DEMO' if DEMO else 'LIVE (waiting for phone)'}")
    print("=" * 46)

    pygame.init()
    pygame.font.init()
    load_rotation()
    load_region()
    screen, fb = init_screen()
    fonts = make_fonts()
    clock = pygame.time.Clock()

    if DEMO:
        threading.Thread(target=demo_feeder, daemon=True).start()
    else:
        threading.Thread(target=tcp_server, daemon=True).start()
        threading.Thread(target=rfcomm_server, daemon=True).start()
        threading.Thread(target=discovery_server, daemon=True).start()
        threading.Thread(target=nav_watchdog, daemon=True).start()
        threading.Thread(
            target=lambda: (_run(["rfkill", "unblock", "bluetooth"], timeout=8),
                            _btctl("power on", "pairable on", timeout=10),
                            bt_start_agent()),
            daemon=True).start()
    threading.Thread(target=touch_thread, daemon=True).start()
    threading.Thread(target=wifi_poller, daemon=True).start()
    threading.Thread(target=map_pan_watchdog, daemon=True).start()

    flash = 0
    drag_from = None
    running = True

    if CALIBRATE:
        cal_active = True
        cal_step = 0
        cal_samples.clear()
        with touch_lock:
            touch_raw.clear()
        print("Calibration mode — tap the four crosshairs on the panel")
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_s:
                settings_open = not settings_open
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_r:
                cycle_rotation()
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_b:
                bt_panel_open = not bt_panel_open
                if bt_panel_open:
                    bt_worker("fix")
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_w:
                wifi_panel_open = not wifi_panel_open
                if wifi_panel_open:
                    wifi_worker("refresh")
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                if ev.type == pygame.FINGERDOWN:
                    mx, my = int(ev.x * SCREEN_W), int(ev.y * SCREEN_H)
                else:
                    mx, my = ev.pos
                if SET_CHIP:
                    x0, y0, w0, h0 = SET_CHIP
                    if x0 <= mx <= x0 + w0 and y0 <= my <= y0 + h0:
                        settings_open = True

        # drag / pan on the map
        with touch_lock:
            evs = list(touch_events); touch_events.clear()
        for (kind, ex, ey) in evs:
            r = MAP_RECT
            inside = r and (r[0] <= ex <= r[0] + r[2] and r[1] <= ey <= r[1] + r[3])
            if kind == "down":
                drag_from = (ex, ey) if (inside and not any(
                    (settings_open, wifi_panel_open, bt_panel_open,
                     kb_open, power_confirm, cal_active))) else None
            elif kind == "move" and drag_from:
                ddx = ex - drag_from[0]
                ddy = ey - drag_from[1]
                if abs(ddx) > 1 or abs(ddy) > 1:
                    with pan_lock:
                        z = map_pan["zoom"]
                    map_drag(ddx, ddy, 1.3 / max(0.25, z))
                    drag_from = (ex, ey)
            elif kind == "up":
                drag_from = None

        # touch taps from evdev
        with touch_lock:
            taps = list(touch_tap); touch_tap.clear()
        for (mx, my) in taps:
            def _hit(r):
                return r and r[0] <= mx <= r[0] + r[2] and r[1] <= my <= r[1] + r[3]

            if cal_active:
                # use the RAW sample that accompanied this tap
                with touch_lock:
                    raw = touch_raw.pop(0) if touch_raw else None
                if cal_step < len(CAL_TARGETS):
                    if raw:
                        cal_samples.append(raw)
                        cal_step += 1
                        if cal_step >= len(CAL_TARGETS):
                            apply_calibration()
                else:
                    cal_active = False
                    cal_step = 0
                    cal_samples.clear()
                continue

            if settings_open:
                if _hit(SET_BTN_CLOSE):
                    settings_open = False
                    continue
                hit_row = False
                for (rr2, key) in SET_ROWS:
                    if _hit(rr2):
                        hit_row = True
                        if key == "map":
                            with nav_lock:
                                nav["region_msg"] = (
                                    tiles.info() if tiles else "no map")
                        elif key == "wifi":
                            settings_open = False
                            wifi_panel_open = True
                            wifi_worker("refresh")
                        elif key == "bt":
                            settings_open = False
                            bt_panel_open = True
                            bt_worker("fix")     # unblock + power + visible
                        elif key == "theme":
                            with theme_lock:
                                theme_mode = {"auto": "day", "day": "night",
                                              "night": "auto"}[theme_mode]
                            print(f"Theme -> {theme_mode}")
                        elif key == "cal":
                            settings_open = False
                            cal_active = True
                            cal_step = 0
                            cal_samples.clear()
                            with touch_lock:
                                touch_raw.clear()
                        break
                if not hit_row:
                    pass
                continue

            if power_confirm:
                if power_msg:
                    continue          # already shutting down; ignore taps
                if _hit(PWR_BTN_OFF):
                    threading.Thread(target=do_shutdown, daemon=True).start()
                elif _hit(PWR_BTN_REBOOT):
                    threading.Thread(target=do_reboot, daemon=True).start()
                elif _hit(PWR_BTN_CANCEL):
                    power_confirm = False
                continue

            if kb_open:
                if _hit(globals().get("KB_EYE")):
                    KB_SHOW = not KB_SHOW
                    continue
                hit_key = False
                for rect, label, action in KB_KEYS:
                    if _hit(rect):
                        hit_key = True
                        kind = action[0]
                        if kind == "char":
                            kb_buffer += action[1]
                            if kb_shift:
                                kb_shift = False
                        elif kind == "del":
                            kb_buffer = kb_buffer[:-1]
                        elif kind == "shift":
                            kb_shift = not kb_shift
                        elif kind == "page":
                            kb_page = 0 if kb_page else 1
                            kb_shift = False
                        elif kind == "ok":
                            ssid, pw = kb_target_ssid, kb_buffer
                            kb_open = False
                            kb_buffer = ""
                            threading.Thread(
                                target=lambda: wifi_connect_pw(ssid, pw),
                                daemon=True).start()
                        break
                if not hit_key:
                    # tap outside the keyboard closes it
                    if my < SCREEN_H - int(112 * (SCREEN_H / 320.0)) - int(60 * (SCREEN_H / 320.0)):
                        kb_open = False
                continue

            if bt_panel_open:
                with bt_lock:
                    _pw = bt_state["powered"]
                    _vis = bt_state.get("discoverable", False)
                if _hit(BT_BTN_CLOSE):
                    bt_panel_open = False
                elif _hit(BT_BTN_POWER):
                    bt_worker("power_off" if _pw else "power_on")
                elif _hit(BT_BTN_VIS):
                    bt_worker("visible_off" if _vis else "visible_on")
                elif _hit(BT_BTN_FIX):
                    bt_worker("deep_reset")
                elif _hit(BT_BTN_SCAN):
                    bt_worker("scan")
                else:
                    for (rr3, mac, dname, paired) in BT_ROWS:
                        if _hit(rr3):
                            if paired:
                                bt_worker("remove", mac)
                            else:
                                bt_worker("pair", mac, dname)
                            break
                continue

            if wifi_panel_open:
                if _hit(WIFI_BTN_CLOSE):
                    wifi_panel_open = False
                elif _hit(WIFI_BTN_RESCAN):
                    wifi_worker("rescan")
                else:
                    for (rr_, ssid, joinable) in WIFI_ROWS:
                        if _hit(rr_):
                            if joinable:
                                print(f"WiFi -> connecting {ssid}")
                                wifi_worker("connect", ssid)
                            else:
                                kb_open = True
                                kb_target_ssid = ssid
                                kb_buffer = ""
                                kb_shift = False
                                kb_page = 0
                                with wifi_lock:
                                    wifi_state["msg"] = ""
                            break
                continue

            if _hit(PWR_CHIP):
                power_confirm = True
                continue

            if _hit(MAP_BTN_ZIN):
                map_zoom(1.5)
                continue
            if _hit(MAP_BTN_ZOUT):
                map_zoom(1 / 1.5)
                continue
            if MAP_BTN_RECENTER and _hit(MAP_BTN_RECENTER):
                map_recenter()
                continue

            if _hit(ROT_CHIP):
                cycle_rotation()
                continue

            if _hit(SET_CHIP):
                settings_open = True
                continue

        with nav_lock:
            d = dict(nav)
        flash = (flash + 1) % 28
        want = frame_size()
        if screen.get_size() != want:
            screen = pygame.Surface(want)
        build_frame(screen, fonts, d, flash)
        fb.blit(screen)
        clock.tick(FPS)

    fb.close()
    pygame.quit()
    print("MotoNav stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        try:
            pygame.quit()
        except Exception:
            pass

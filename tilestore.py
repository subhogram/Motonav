#!/usr/bin/env python3
"""
tilestore.py — tiled offline road store for MotoNav.

Why tiles: a whole-city or whole-state road file cannot live in a Pi Zero W's
RAM. Roads are split into fixed geographic cells; only the handful of cells
under the map viewport are ever loaded, and they are dropped again when the
rider moves away.

LAYOUT
    maps/
      index.json                 what we have, and where
      t_<lat>_<lon>.mnt          one tile, TILE_DEG square

TILE FILE  (little-endian)
    magic     6s   b"MNTIL1"
    n_ways    u4
    per way:
      cls     u1        1 motorway .. 7 service
      n_pts   u2
      pts     n_pts * (i4 lat_e6, i4 lon_e6)

Tiles are append-only: a second download covering the same cell adds its ways
to the existing file, so gap-filling never rewrites what is already there.

PI ZERO W NOTES
    The renderer touches this module once per frame, so everything on the
    read path is built to keep the single ARMv6 core free:
      * ways are simplified (Douglas-Peucker) when they are *written*, so the
        draw loop has far fewer points to project,
      * each way carries a precomputed bounding box so the map can throw it
        away without projecting a single point,
      * tiles decode in bulk struct reads rather than per-point unpacks,
      * tiles load on a background thread — ways_near() never blocks the
        frame on disk I/O, it just skips what is not resident yet,
      * the last ways_near() answer is memoised, so standing still or
        creeping along costs nothing at all.
"""

import json
import math
import os
import queue
import struct
import threading
import time

MAGIC = b"MNTIL1"
TILE_DEG = 0.05                 # ~5.5 km cell
MAX_LOADED = 12                 # tiles kept in RAM at once (Pi Zero W: 512 MB)
M_PER_DEG = 111320.0
SIMPLIFY_M = 2.0                # geometry tolerance applied when storing ways

CLASS_STYLE = {
    1: (7, 9, True), 2: (6, 8, True), 3: (5, 7, True), 4: (4, 6, True),
    5: (3, 5, False), 6: (2, 4, False), 7: (2, 3, False),
}

_HEAD = struct.Struct("<BH")
_pt_structs = {}


def _pt_struct(n_ints):
    s = _pt_structs.get(n_ints)
    if s is None:
        if len(_pt_structs) > 512:
            _pt_structs.clear()
        s = struct.Struct("<%di" % n_ints)
        _pt_structs[n_ints] = s
    return s


def tile_key(lat, lon):
    return (int(math.floor(lat / TILE_DEG)), int(math.floor(lon / TILE_DEG)))


def tile_name(key):
    return f"t_{key[0]}_{key[1]}.mnt"


def tile_bounds(key):
    s = key[0] * TILE_DEG
    w = key[1] * TILE_DEG
    return (s, w, s + TILE_DEG, w + TILE_DEG)


def keys_for_bbox(south, west, north, east):
    out = []
    y0, y1 = int(math.floor(south / TILE_DEG)), int(math.floor(north / TILE_DEG))
    x0, x1 = int(math.floor(west / TILE_DEG)), int(math.floor(east / TILE_DEG))
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            out.append((y, x))
    return out


def keys_along(points, radius_m=1500, step_m=None):
    """
    Tiles a route passes through, padded by radius.

    Consecutive route points are metres apart while the pad is over a
    kilometre, so testing every one of them is pure waste. Points closer
    together than `step_m` (default: a third of the pad) are skipped — the
    pad around the kept ones already covers the ground between.
    """
    if not points:
        return []
    if step_m is None:
        step_m = max(100.0, radius_m / 3.0)
    seen = set()
    pad_lat = radius_m / M_PER_DEG
    step2 = step_m * step_m
    last = None
    pts = list(points)
    for i, (la, lo) in enumerate(pts):
        if last is not None and i != len(pts) - 1:
            dla = (la - last[0]) * M_PER_DEG
            dlo = (lo - last[1]) * M_PER_DEG * math.cos(math.radians(la))
            if dla * dla + dlo * dlo < step2:
                continue
        last = (la, lo)
        pad_lon = radius_m / max(1.0, M_PER_DEG * math.cos(math.radians(la)))
        for k in keys_for_bbox(la - pad_lat, lo - pad_lon, la + pad_lat, lo + pad_lon):
            seen.add(k)
    return sorted(seen)


def simplify(pts, eps_m=SIMPLIFY_M):
    """
    Douglas-Peucker in metres (equirectangular — exact enough over a tile).

    Run once, when a way is stored. OSM geometry carries points every few
    metres; on a 1.3 m/px minimap most of them land on the same pixel, so
    dropping them costs nothing visually and saves the draw loop a great
    deal of per-point Python maths.
    """
    n = len(pts)
    if n < 3 or eps_m <= 0:
        return pts
    kx = M_PER_DEG * math.cos(math.radians(pts[0][0]))
    xs = [p[1] * kx for p in pts]
    ys = [p[0] * M_PER_DEG for p in pts]
    keep = bytearray(n)
    keep[0] = keep[n - 1] = 1
    eps2 = eps_m * eps_m
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = xs[i0], ys[i0]
        dx, dy = xs[i1] - x0, ys[i1] - y0
        den = dx * dx + dy * dy
        worst, wi = -1.0, -1
        for i in range(i0 + 1, i1):
            px, py = xs[i] - x0, ys[i] - y0
            if den > 0.0:
                t = (px * dx + py * dy) / den
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                ex, ey = px - t * dx, py - t * dy
            else:
                ex, ey = px, py
            d2 = ex * ex + ey * ey
            if d2 > worst:
                worst, wi = d2, i
        if worst > eps2 and wi > 0:
            keep[wi] = 1
            stack.append((i0, wi))
            stack.append((wi, i1))
    return [pts[i] for i in range(n) if keep[i]]


def encode_ways(ways, eps_m=SIMPLIFY_M):
    """ways = [(cls, [(lat, lon), ...])] -> tile bytes (no header)."""
    buf = bytearray()
    n = 0
    for cls, pts in ways:
        if len(pts) < 2:
            continue
        pts = simplify(pts, eps_m)
        if len(pts) < 2 or len(pts) > 65535:
            continue
        buf += _HEAD.pack(cls, len(pts))
        flat = []
        for la, lo in pts:
            flat.append(int(round(la * 1e6)))
            flat.append(int(round(lo * 1e6)))
        buf += _pt_struct(len(flat)).pack(*flat)
        n += 1
    return bytes(buf), n


class TileStore:
    """Disk-backed tiles with a small LRU of decoded ways."""

    def __init__(self, base_dir):
        self.dir = base_dir
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self._cache = {}            # key -> [way, ...]
        self._order = []            # LRU
        self._gen = 0               # bumped whenever tiles change on disk
        self._near = None           # memoised ways_near answer
        self._loadq = queue.Queue()
        self._loading = set()
        self._loader = None
        self.index = self._read_index()

    # ── index ────────────────────────────────────────────────────────────
    def _index_path(self):
        return os.path.join(self.dir, "index.json")

    def _read_index(self):
        try:
            with open(self._index_path()) as f:
                raw = json.load(f)
            return {tuple(map(int, k.split(","))): v for k, v in raw.items()}
        except Exception:
            return {}

    def _write_index(self):
        try:
            raw = {f"{k[0]},{k[1]}": v for k, v in self.index.items()}
            tmp = self._index_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(raw, f)
            os.replace(tmp, self._index_path())
        except Exception as e:
            print(f"tile index write: {e}")

    # ── writing ──────────────────────────────────────────────────────────
    def append_tile(self, key, blob, n_ways):
        """Append encoded ways to a tile, creating it if needed."""
        path = os.path.join(self.dir, tile_name(key))
        with self.lock:
            try:
                if os.path.exists(path):
                    with open(path, "r+b") as f:
                        head = f.read(6)
                        if head != MAGIC:
                            f.seek(0)
                            f.write(MAGIC + struct.pack("<I", n_ways) + blob)
                            total = n_ways
                        else:
                            cur = struct.unpack("<I", f.read(4))[0]
                            total = cur + n_ways
                            f.seek(6)
                            f.write(struct.pack("<I", total))
                            f.seek(0, os.SEEK_END)
                            f.write(blob)
                else:
                    with open(path, "wb") as f:
                        f.write(MAGIC + struct.pack("<I", n_ways) + blob)
                    total = n_ways

                meta = self.index.get(key, {"ways": 0, "bytes": 0, "at": 0})
                meta["ways"] = total
                meta["bytes"] = os.path.getsize(path)
                meta["at"] = int(time.time())
                self.index[key] = meta
                self._write_index()
                self._cache.pop(key, None)
                self._gen += 1
                self._near = None
                return True
            except Exception as e:
                print(f"append tile {key}: {e}")
                return False

    def store_ways(self, ways):
        """Split ways by tile and append each. Returns tiles touched."""
        buckets = {}
        for cls, pts in ways:
            if len(pts) < 2:
                continue
            # a way belongs to every tile it crosses
            for (la, lo) in pts:
                buckets.setdefault(tile_key(la, lo), []).append((cls, pts))
                break     # index by first point; long ways still render via neighbours
        touched = []
        for key, ws in buckets.items():
            blob, n = encode_ways(ws)
            if n and self.append_tile(key, blob, n):
                touched.append(key)
        return touched

    # ── reading ──────────────────────────────────────────────────────────
    def has(self, key):
        return key in self.index and self.index[key].get("ways", 0) > 0

    def missing(self, keys):
        return [k for k in keys if not self.has(k)]

    def _decode(self, blob):
        """Decode a tile blob into [(cls, pts, s, w, n, e), ...]."""
        ways = []
        if len(blob) <= 10 or blob[:6] != MAGIC:
            return ways
        n = struct.unpack_from("<I", blob, 6)[0]
        off, total = 10, len(blob)
        for _ in range(n):
            if off + 3 > total:
                break
            cls, npts = _HEAD.unpack_from(blob, off)
            off += 3
            need = npts * 8
            if off + need > total:
                break
            if npts >= 2:
                # one bulk unpack per way beats 2*npts unpack_from calls
                v = _pt_struct(npts * 2).unpack_from(blob, off)
                la_i, lo_i = v[0::2], v[1::2]
                pts = [(la_i[i] / 1e6, lo_i[i] / 1e6) for i in range(npts)]
                ways.append((cls, pts,
                             min(la_i) / 1e6, min(lo_i) / 1e6,
                             max(la_i) / 1e6, max(lo_i) / 1e6))
            off += need
        return ways

    def load_tile(self, key):
        """Decode one tile, with an LRU so RAM stays bounded."""
        with self.lock:
            hit = self._cache.get(key)
            if hit is not None:
                try:
                    self._order.remove(key)
                except ValueError:
                    pass
                self._order.append(key)
                return hit

        path = os.path.join(self.dir, tile_name(key))
        ways = []
        try:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    ways = self._decode(f.read())
        except Exception as e:
            print(f"load tile {key}: {e}")

        with self.lock:
            self._cache[key] = ways
            self._order.append(key)
            while len(self._order) > MAX_LOADED:
                old = self._order.pop(0)
                if old != key:
                    self._cache.pop(old, None)
            self._near = None
        return ways

    # ── background loading ───────────────────────────────────────────────
    def _loader_loop(self):
        while True:
            key = self._loadq.get()
            try:
                self.load_tile(key)
            except Exception as e:
                print(f"tile prefetch {key}: {e}")
            finally:
                with self.lock:
                    self._loading.discard(key)

    def prefetch(self, key):
        """Queue a tile for decoding off the render thread."""
        with self.lock:
            if key in self._cache or key in self._loading:
                return
            self._loading.add(key)
            if self._loader is None:
                self._loader = threading.Thread(target=self._loader_loop,
                                                daemon=True)
                self._loader.start()
        self._loadq.put(key)

    def ways_near(self, lat, lon, radius_m=800, blocking=False):
        """
        Ways from the tiles covering a radius — this is what the map draws.

        Called once per frame, so by default it only returns tiles that are
        already decoded and hands anything else to the loader thread; a tile
        pops in a frame or two later instead of stalling the whole UI while
        it is read and parsed. The answer is memoised until the viewport
        moves to a different set of tiles or a tile changes underneath us.
        """
        if lat is None or lon is None:
            return []
        pad_lat = radius_m / M_PER_DEG
        pad_lon = radius_m / max(1.0, M_PER_DEG * math.cos(math.radians(lat)))
        keys = tuple(k for k in keys_for_bbox(lat - pad_lat, lon - pad_lon,
                                              lat + pad_lat, lon + pad_lon)
                     if self.has(k))

        with self.lock:
            near = self._near
            if near is not None and near[0] == keys and near[1] == self._gen:
                return near[2]
            resident = {k: self._cache.get(k) for k in keys}

        out = []
        complete = True
        for k in keys:
            ways = resident.get(k)
            if ways is None:
                if blocking:
                    ways = self.load_tile(k)
                else:
                    self.prefetch(k)
                    complete = False
                    continue
            out.extend(ways)

        with self.lock:
            # only memoise a complete answer; a partial one must be retried
            # next frame so freshly loaded tiles actually show up
            if complete:
                self._near = (keys, self._gen, out)
            # keep the LRU honest about what the viewport is using
            for k in keys:
                if k in self._cache:
                    try:
                        self._order.remove(k)
                    except ValueError:
                        pass
                    self._order.append(k)
        return out

    def covers(self, lat, lon):
        return self.has(tile_key(lat, lon))

    # ── housekeeping ─────────────────────────────────────────────────────
    def stats(self):
        n_tiles = len(self.index)
        n_ways = sum(m.get("ways", 0) for m in self.index.values())
        n_bytes = sum(m.get("bytes", 0) for m in self.index.values())
        return {"tiles": n_tiles, "ways": n_ways, "bytes": n_bytes,
                "loaded": len(self._cache)}

    def info(self):
        s = self.stats()
        if not s["tiles"]:
            return "no tiles"
        return f"{s['tiles']} tiles, {s['ways']} roads"

    def clear(self):
        freed = 0
        try:
            for f in os.listdir(self.dir):
                p = os.path.join(self.dir, f)
                if os.path.isfile(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
        except Exception as e:
            print(f"tile clear: {e}")
        with self.lock:
            self.index = {}
            self._cache = {}
            self._order = []
            self._gen += 1
            self._near = None
        return freed

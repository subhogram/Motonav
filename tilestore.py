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
"""

import json
import math
import os
import struct
import threading
import time

MAGIC = b"MNTIL1"
TILE_DEG = 0.05                 # ~5.5 km cell
MAX_LOADED = 24                 # tiles kept in RAM at once
M_PER_DEG = 111320.0

CLASS_STYLE = {
    1: (7, 9, True), 2: (6, 8, True), 3: (5, 7, True), 4: (4, 6, True),
    5: (3, 5, False), 6: (2, 4, False), 7: (2, 3, False),
}


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


def keys_along(points, radius_m=1500):
    """Tiles a route passes through, padded by radius."""
    seen = set()
    pad_lat = radius_m / M_PER_DEG
    for (la, lo) in points:
        pad_lon = radius_m / max(1.0, M_PER_DEG * math.cos(math.radians(la)))
        for k in keys_for_bbox(la - pad_lat, lo - pad_lon, la + pad_lat, lo + pad_lon):
            seen.add(k)
    return sorted(seen)


def encode_ways(ways):
    """ways = [(cls, [(lat, lon), ...])] -> tile bytes (no header)."""
    buf = bytearray()
    n = 0
    for cls, pts in ways:
        if len(pts) < 2 or len(pts) > 65535:
            continue
        buf += struct.pack("<BH", cls, len(pts))
        for la, lo in pts:
            buf += struct.pack("<ii", int(round(la * 1e6)), int(round(lo * 1e6)))
        n += 1
    return bytes(buf), n


class TileStore:
    """Disk-backed tiles with a small LRU of decoded ways."""

    def __init__(self, base_dir):
        self.dir = base_dir
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self._cache = {}            # key -> [(cls, pts), ...]
        self._order = []            # LRU
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
                    blob = f.read()
                if len(blob) > 10 and blob[:6] == MAGIC:
                    n = struct.unpack_from("<I", blob, 6)[0]
                    off = 10
                    for _ in range(n):
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
        except Exception as e:
            print(f"load tile {key}: {e}")

        with self.lock:
            self._cache[key] = ways
            self._order.append(key)
            while len(self._order) > MAX_LOADED:
                old = self._order.pop(0)
                if old != key:
                    self._cache.pop(old, None)
        return ways

    def ways_near(self, lat, lon, radius_m=800):
        """Ways from the tiles covering a radius — this is what the map draws."""
        if lat is None:
            return []
        pad_lat = radius_m / M_PER_DEG
        pad_lon = radius_m / max(1.0, M_PER_DEG * math.cos(math.radians(lat)))
        out = []
        for key in keys_for_bbox(lat - pad_lat, lon - pad_lon,
                                 lat + pad_lat, lon + pad_lon):
            if self.has(key):
                out.extend(self.load_tile(key))
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
        return freed

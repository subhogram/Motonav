#!/usr/bin/env python3
"""
pbfimport.py — turn an .osm.pbf extract into MotoNav tiles.

Get an extract from  https://download.openstreetmap.fr/extracts/
(e.g. asia/india/karnataka.osm.pbf), copy it to the Pi, then:

    python3 pbfimport.py karnataka.osm.pbf

It streams the file, keeps roads plus lakes, parks and rivers, and writes
them into maps/ as tiles.
Nothing is held in RAM beyond one block at a time, so it runs on a Zero W —
slowly, but it only has to happen once per extract.

    --highways major|normal|full    how much road detail to keep (default normal)
    --no-areas                      roads only: skip lakes, parks and rivers
    --bbox S,W,N,E                  only import inside this box
    --out DIR                       tile directory (default ./maps)

Land cover comes from ways only, so lakes and forests mapped as multipolygon
relations are missed. Ordinary closed-way lakes, parks and rivers — nearly
everything that reads on a 480x320 panel — come through.

DEPENDENCIES
    pip3 install protobuf   (only needed for the import, not for navigating)
"""

import argparse
import os
import struct
import sys
import time
import zlib

# tilestore lives next to this script; make that true however it was invoked
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tilestore

# ── road filtering ────────────────────────────────────────────────────────
CLASS_OF = {
    "motorway": 1, "motorway_link": 1,
    "trunk": 2, "trunk_link": 2,
    "primary": 3, "primary_link": 3,
    "secondary": 4, "secondary_link": 4,
    "tertiary": 5, "tertiary_link": 5,
    "unclassified": 6, "residential": 6, "living_street": 6,
    "service": 7, "road": 7,
}

LEVELS = {
    "major":  {1, 2, 3},
    "normal": {1, 2, 3, 4, 5},
    "full":   {1, 2, 3, 4, 5, 6, 7},
}

# ── land cover ────────────────────────────────────────────────────────────
# Lakes, parks and rivers, which give the map some sense of place for very
# little drawing cost. The detail levels above gate roads only; land cover is
# either on or off (--no-areas).
WATER_LANDUSE = {"reservoir", "basin"}
GREEN_LANDUSE = {"forest", "grass", "meadow", "village_green",
                 "recreation_ground"}
GREEN_LEISURE = {"park", "garden", "nature_reserve", "golf_course"}
WATERWAY_KINDS = {"river", "canal", "stream"}

# the only tag keys worth decoding out of a way — everything else is skipped
TAG_KEYS = frozenset(("highway", "natural", "landuse", "leisure", "waterway"))


def class_of_tags(tags, keep, areas=True):
    """
    OSM tags -> tile class, or None to skip the way.

    `tags` is a plain dict of the way's tags. Roads win over land cover: a
    road bridging a river is a road, and it is tagged both ways.
    """
    hw = tags.get("highway")
    if hw:
        cls = CLASS_OF.get(hw)
        return cls if cls in keep else None
    if not areas:
        return None
    if tags.get("natural") == "water":
        return tilestore.CLS_WATER
    landuse = tags.get("landuse")
    if landuse in WATER_LANDUSE:
        return tilestore.CLS_WATER
    if tags.get("waterway") in WATERWAY_KINDS:
        return tilestore.CLS_WATERWAY
    if landuse in GREEN_LANDUSE or tags.get("leisure") in GREEN_LEISURE:
        return tilestore.CLS_GREEN
    return None


# ── minimal PBF reader (no external OSM library needed) ───────────────────
def _read_varint(buf, pos):
    shift = 0
    val = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _parse_fields(buf):
    """Yield (field_number, wire_type, value_or_bytes)."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        fnum, wtype = key >> 3, key & 7
        if wtype == 0:
            val, pos = _read_varint(buf, pos)
            yield fnum, wtype, val
        elif wtype == 2:
            ln, pos = _read_varint(buf, pos)
            yield fnum, wtype, buf[pos:pos + ln]
            pos += ln
        elif wtype == 5:
            yield fnum, wtype, buf[pos:pos + 4]
            pos += 4
        elif wtype == 1:
            yield fnum, wtype, buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"wire type {wtype}")


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


def _packed_varints(b):
    out = []
    pos = 0
    while pos < len(b):
        v, pos = _read_varint(b, pos)
        out.append(v)
    return out


def blocks(path):
    """Yield (blob_type, decompressed_bytes) for each block in the file."""
    with open(path, "rb") as f:
        while True:
            hdr_len_raw = f.read(4)
            if len(hdr_len_raw) < 4:
                return
            hdr_len = struct.unpack(">I", hdr_len_raw)[0]
            header = f.read(hdr_len)
            btype, dsize = None, 0
            for fnum, wt, val in _parse_fields(header):
                if fnum == 1 and wt == 2:
                    btype = val.decode("ascii", "ignore")
                elif fnum == 3 and wt == 0:
                    dsize = val
            body = f.read(dsize)
            raw = None
            for fnum, wt, val in _parse_fields(body):
                if fnum == 1 and wt == 2:
                    raw = val
                elif fnum == 3 and wt == 2:
                    raw = zlib.decompress(val)
            if raw is not None:
                yield btype, raw


def import_pbf(path, out_dir, level="normal", bbox=None, progress=None,
               areas=True):
    """Stream a .osm.pbf into tiles. Returns (ways_written, tiles_touched)."""
    keep = LEVELS.get(level, LEVELS["normal"])
    store = tilestore.TileStore(out_dir)

    nodes = {}                 # id -> (lat, lon)   only for kept ways' refs
    pending = []               # ways awaiting node resolution
    ways_out = 0
    tiles = set()
    t0 = time.time()
    n_blocks = 0

    def in_box(la, lo):
        if not bbox:
            return True
        s, w, n, e = bbox
        return s <= la <= n and w <= lo <= e

    # Pass 1: nodes.  Pass 2: ways.  We stream twice to stay memory-light.
    for pass_no in (1, 2):
        if progress:
            progress(f"pass {pass_no}/2 …")
        for btype, raw in blocks(path):
            if btype != "OSMData":
                continue
            n_blocks += 1
            gran, lat_off, lon_off = 100, 0, 0
            strings = []
            groups = []
            for fnum, wt, val in _parse_fields(raw):
                if fnum == 1 and wt == 2:
                    strings = [b for _, _, b in _parse_fields(val)]
                elif fnum == 2 and wt == 2:
                    groups.append(val)
                elif fnum == 17 and wt == 0:
                    gran = val
                elif fnum == 19 and wt == 0:
                    lat_off = val
                elif fnum == 20 and wt == 0:
                    lon_off = val

            def s(i):
                try:
                    return strings[i].decode("utf-8", "ignore")
                except Exception:
                    return ""

            for g in groups:
                for fnum, wt, val in _parse_fields(g):
                    # dense nodes
                    if pass_no == 1 and fnum == 2 and wt == 2:
                        ids, lats, lons = [], [], []
                        for f2, w2, v2 in _parse_fields(val):
                            if f2 == 1:
                                ids = _packed_varints(v2)
                            elif f2 == 8:
                                lats = _packed_varints(v2)
                            elif f2 == 9:
                                lons = _packed_varints(v2)
                        cid = clat = clon = 0
                        for i in range(min(len(ids), len(lats), len(lons))):
                            cid += _zigzag(ids[i])
                            clat += _zigzag(lats[i])
                            clon += _zigzag(lons[i])
                            la = (lat_off + gran * clat) / 1e9
                            lo = (lon_off + gran * clon) / 1e9
                            if in_box(la, lo):
                                nodes[cid] = (la, lo)
                    # ways
                    elif pass_no == 2 and fnum == 3 and wt == 2:
                        refs, keys, vals = [], [], []
                        for f2, w2, v2 in _parse_fields(val):
                            if f2 == 2:
                                keys = _packed_varints(v2)
                            elif f2 == 3:
                                vals = _packed_varints(v2)
                            elif f2 == 8:
                                refs = _packed_varints(v2)
                        tags = {}
                        for k, v in zip(keys, vals):
                            kn = s(k)
                            if kn in TAG_KEYS:
                                tags[kn] = s(v)
                        if not tags:
                            continue
                        cls = class_of_tags(tags, keep, areas)
                        if cls is None:
                            continue
                        pts = []
                        cur = 0
                        for r in refs:
                            cur += _zigzag(r)
                            p = nodes.get(cur)
                            if p:
                                pts.append(p)
                        if len(pts) >= 2:
                            pending.append((cls, pts))
                            if len(pending) >= 2000:
                                tiles.update(store.store_ways(pending))
                                ways_out += len(pending)
                                pending = []
                                if progress:
                                    progress(f"{ways_out} ways, {len(tiles)} tiles")
        if pass_no == 1 and progress:
            progress(f"{len(nodes)} nodes indexed")

    if pending:
        tiles.update(store.store_ways(pending))
        ways_out += len(pending)

    dt = time.time() - t0
    if progress:
        progress(f"done: {ways_out} ways into {len(tiles)} tiles in {dt:.0f}s")
    return ways_out, len(tiles)


def main():
    ap = argparse.ArgumentParser(description="Import an .osm.pbf into MotoNav tiles")
    ap.add_argument("pbf")
    ap.add_argument("--out", default=None, help="tile dir (default ./maps)")
    ap.add_argument("--highways", default="normal", choices=list(LEVELS))
    ap.add_argument("--bbox", default=None, help="S,W,N,E to limit the import")
    ap.add_argument("--no-areas", dest="areas", action="store_false",
                    help="roads only: skip lakes, parks and rivers")
    a = ap.parse_args()

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")
    bbox = None
    if a.bbox:
        try:
            bbox = tuple(float(x) for x in a.bbox.split(","))
            if len(bbox) != 4:
                raise ValueError
        except Exception:
            print("--bbox must be S,W,N,E")
            return 1

    if not os.path.exists(a.pbf):
        print(f"not found: {a.pbf}")
        return 1

    size_mb = os.path.getsize(a.pbf) / 1e6
    print(f"Importing {a.pbf} ({size_mb:.0f} MB), detail={a.highways}")
    print("This is a one-time job and is slow on a Pi Zero W — leave it running.")
    import_pbf(a.pbf, out, a.highways, bbox,
               progress=lambda m: print("  " + m), areas=a.areas)
    return 0


if __name__ == "__main__":
    sys.exit(main())

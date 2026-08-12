package com.motonav;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Map library on the PHONE, stored as tiles on the same 0.05° grid the Pi
 * uses. Because both sides speak tiles, we can send a route exactly the
 * cells it needs instead of a whole region — which is what makes a big
 * offline download useful on a Zero W.
 *
 *   files/tiles/t_<y>_<x>.mnt      one tile (same format as the Pi)
 *   files/tiles/regions.json       what the user has downloaded
 */
public class TileCache {

    private static final String TAG = "MotoNavTiles";
    private static final String DIR = "tiles";
    private static final String REGIONS = "regions.json";

    public static final double TILE_DEG = 0.05;
    private static final byte[] MAGIC = {'M', 'N', 'T', 'I', 'L', '1'};

    public static class Region {
        public String name;
        public double south, west, north, east;
        public int tiles, ways;
        public long bytes, savedAt;
    }

    // ── grid helpers (must match tilestore.py) ───────────────────────────
    public static int tileY(double lat) { return (int) Math.floor(lat / TILE_DEG); }
    public static int tileX(double lon) { return (int) Math.floor(lon / TILE_DEG); }
    public static String tileName(int y, int x) { return "t_" + y + "_" + x + ".mnt"; }

    public static Set<String> keysForBBox(double s, double w, double n, double e) {
        Set<String> out = new HashSet<>();
        for (int y = tileY(s); y <= tileY(n); y++)
            for (int x = tileX(w); x <= tileX(e); x++)
                out.add(y + "," + x);
        return out;
    }

    /** Tiles a route passes through, padded by radiusM. */
    public static List<String> keysAlong(List<double[]> route, double radiusM) {
        Set<String> seen = new HashSet<>();
        double padLat = radiusM / 111320.0;
        for (double[] p : route) {
            double padLon = radiusM / (111320.0 * Math.cos(Math.toRadians(p[0])));
            seen.addAll(keysForBBox(p[0] - padLat, p[1] - padLon,
                                    p[0] + padLat, p[1] + padLon));
        }
        List<String> out = new ArrayList<>(seen);
        Collections.sort(out);
        return out;
    }

    private static File dir(Context c) {
        File d = new File(c.getApplicationContext().getFilesDir(), DIR);
        if (!d.exists()) d.mkdirs();
        return d;
    }

    // ── writing ──────────────────────────────────────────────────────────
    /**
     * Store ways into tiles. `ways` entries are {classByte, lat/lon pairs}.
     * Appends to existing tiles rather than replacing them.
     */
    public static int storeWays(Context c, List<Object[]> ways) {
        java.util.HashMap<String, List<Object[]>> buckets = new java.util.HashMap<>();
        for (Object[] w : ways) {
            double[] pts = (double[]) w[1];
            if (pts.length < 4) continue;
            String key = tileY(pts[0]) + "," + tileX(pts[1]);
            List<Object[]> b = buckets.get(key);
            if (b == null) { b = new ArrayList<>(); buckets.put(key, b); }
            b.add(w);
        }
        int touched = 0;
        for (java.util.Map.Entry<String, List<Object[]>> en : buckets.entrySet()) {
            if (appendTile(c, en.getKey(), en.getValue())) touched++;
        }
        return touched;
    }

    private static boolean appendTile(Context c, String key, List<Object[]> ways) {
        try {
            String[] p = key.split(",");
            File f = new File(dir(c), tileName(Integer.parseInt(p[0]), Integer.parseInt(p[1])));

            // encode the new ways
            int size = 0;
            for (Object[] w : ways) size += 3 + ((double[]) w[1]).length / 2 * 8;
            ByteBuffer bb = ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN);
            int n = 0;
            for (Object[] w : ways) {
                int cls = (Integer) w[0];
                double[] pts = (double[]) w[1];
                int npts = pts.length / 2;
                if (npts < 2 || npts > 65535) continue;
                bb.put((byte) cls);
                bb.putShort((short) npts);
                for (int i = 0; i < npts; i++) {
                    bb.putInt((int) Math.round(pts[i * 2] * 1e6));
                    bb.putInt((int) Math.round(pts[i * 2 + 1] * 1e6));
                }
                n++;
            }
            byte[] blob = new byte[bb.position()];
            System.arraycopy(bb.array(), 0, blob, 0, bb.position());
            if (n == 0) return false;

            if (f.exists()) {
                RandomAccessFile r = new RandomAccessFile(f, "rw");
                byte[] head = new byte[6];
                r.readFully(head);
                boolean ok = true;
                for (int i = 0; i < 6; i++) if (head[i] != MAGIC[i]) ok = false;
                if (ok) {
                    byte[] cnt = new byte[4];
                    r.readFully(cnt);
                    int cur = ByteBuffer.wrap(cnt).order(ByteOrder.LITTLE_ENDIAN).getInt();
                    r.seek(6);
                    r.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN)
                            .putInt(cur + n).array());
                    r.seek(r.length());
                    r.write(blob);
                } else {
                    r.setLength(0);
                    r.write(MAGIC);
                    r.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN)
                            .putInt(n).array());
                    r.write(blob);
                }
                r.close();
            } else {
                FileOutputStream fo = new FileOutputStream(f);
                fo.write(MAGIC);
                fo.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN)
                        .putInt(n).array());
                fo.write(blob);
                fo.close();
            }
            return true;
        } catch (Exception e) {
            Log.e(TAG, "appendTile " + key, e);
            return false;
        }
    }

    // ── reading / slicing ────────────────────────────────────────────────
    public static boolean has(Context c, String key) {
        String[] p = key.split(",");
        File f = new File(dir(c), tileName(Integer.parseInt(p[0]), Integer.parseInt(p[1])));
        return f.exists() && f.length() > 10;
    }

    public static List<String> haveOf(Context c, List<String> keys) {
        List<String> out = new ArrayList<>();
        for (String k : keys) if (has(c, k)) out.add(k);
        return out;
    }

    public static List<String> missingOf(Context c, List<String> keys) {
        List<String> out = new ArrayList<>();
        for (String k : keys) if (!has(c, k)) out.add(k);
        return out;
    }

    /**
     * Build a .mnosm blob containing just these tiles — this is the slice
     * we hand the Pi at the start of a route.
     */
    public static byte[] sliceBlob(Context c, List<String> keys) {
        try {
            List<byte[]> chunks = new ArrayList<>();
            int total = 0;
            double s = 90, w = 180, n = -90, e = -180;

            for (String key : keys) {
                if (!has(c, key)) continue;
                String[] p = key.split(",");
                int ty = Integer.parseInt(p[0]), tx = Integer.parseInt(p[1]);
                File f = new File(dir(c), tileName(ty, tx));
                RandomAccessFile r = new RandomAccessFile(f, "r");
                byte[] all = new byte[(int) r.length()];
                r.readFully(all);
                r.close();
                if (all.length < 10) continue;
                ByteBuffer bb = ByteBuffer.wrap(all).order(ByteOrder.LITTLE_ENDIAN);
                bb.position(6);
                int cnt = bb.getInt();
                byte[] body = new byte[all.length - 10];
                System.arraycopy(all, 10, body, 0, body.length);
                chunks.add(body);
                total += cnt;
                s = Math.min(s, ty * TILE_DEG);
                w = Math.min(w, tx * TILE_DEG);
                n = Math.max(n, (ty + 1) * TILE_DEG);
                e = Math.max(e, (tx + 1) * TILE_DEG);
            }
            if (total == 0) return null;

            int size = 6 + 32 + 4;
            for (byte[] b : chunks) size += b.length;
            ByteBuffer out = ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN);
            out.put("MNOSM1".getBytes("US-ASCII"));
            out.putDouble(s); out.putDouble(w); out.putDouble(n); out.putDouble(e);
            out.putInt(total);
            for (byte[] b : chunks) out.put(b);
            Log.i(TAG, "slice: " + keys.size() + " tiles, " + total + " ways, "
                     + size / 1024 + " KB");
            return out.array();
        } catch (Exception ex) {
            Log.e(TAG, "sliceBlob", ex);
            return null;
        }
    }

    /** Send a slice to the Pi in base64 chunks, reporting progress. */
    public static boolean sendSlice(Context c, List<String> keys,
                                    RegionDownloader.Progress cb) {
        byte[] blob = sliceBlob(c, keys);
        if (blob == null) return false;
        try {
            Link.send(c, "{\"type\":\"region_begin\",\"bytes\":" + blob.length + "}");
            final int CHUNK = 8192;
            int sent = 0;
            while (sent < blob.length) {
                int len = Math.min(CHUNK, blob.length - sent);
                String b64 = android.util.Base64.encodeToString(
                    blob, sent, len, android.util.Base64.NO_WRAP);
                Link.send(c, "{\"type\":\"region_chunk\",\"d\":\"" + b64 + "\"}");
                sent += len;
                if (cb != null)
                    cb.onStatus("Sending map " + (sent * 100 / blob.length) + "%");
                try { Thread.sleep(5); } catch (InterruptedException ignored) {}
            }
            Link.send(c, "{\"type\":\"region_end\"}");
            return true;
        } catch (Exception e) {
            Log.e(TAG, "sendSlice", e);
            return false;
        }
    }

    // ── region bookkeeping ───────────────────────────────────────────────
    public static List<Region> regions(Context c) {
        List<Region> out = new ArrayList<>();
        File f = new File(dir(c), REGIONS);
        if (!f.exists()) return out;
        try {
            RandomAccessFile r = new RandomAccessFile(f, "r");
            byte[] b = new byte[(int) r.length()];
            r.readFully(b); r.close();
            JSONArray arr = new JSONArray(new String(b, "UTF-8"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                Region g = new Region();
                g.name = o.optString("name");
                g.south = o.optDouble("s"); g.west = o.optDouble("w");
                g.north = o.optDouble("n"); g.east = o.optDouble("e");
                g.tiles = o.optInt("tiles"); g.ways = o.optInt("ways");
                g.bytes = o.optLong("bytes"); g.savedAt = o.optLong("at");
                out.add(g);
            }
        } catch (Exception e) {
            Log.w(TAG, "regions read: " + e.getMessage());
        }
        return out;
    }

    public static void addRegion(Context c, String name, double s, double w,
                                 double n, double e, int tiles, int ways, long bytes) {
        try {
            List<Region> all = regions(c);
            Region g = new Region();
            g.name = name; g.south = s; g.west = w; g.north = n; g.east = e;
            g.tiles = tiles; g.ways = ways; g.bytes = bytes;
            g.savedAt = System.currentTimeMillis();
            all.add(g);
            JSONArray arr = new JSONArray();
            for (Region x : all) {
                JSONObject o = new JSONObject();
                o.put("name", x.name); o.put("s", x.south); o.put("w", x.west);
                o.put("n", x.north); o.put("e", x.east);
                o.put("tiles", x.tiles); o.put("ways", x.ways);
                o.put("bytes", x.bytes); o.put("at", x.savedAt);
                arr.put(o);
            }
            FileOutputStream fo = new FileOutputStream(new File(dir(c), REGIONS));
            fo.write(arr.toString().getBytes("UTF-8"));
            fo.close();
        } catch (Exception ex) {
            Log.w(TAG, "addRegion: " + ex.getMessage());
        }
    }

    public static long totalBytes(Context c) {
        long t = 0;
        File[] fs = dir(c).listFiles();
        if (fs != null) for (File f : fs) t += f.length();
        return t;
    }

    public static int tileCount(Context c) {
        int n = 0;
        File[] fs = dir(c).listFiles();
        if (fs != null) for (File f : fs) if (f.getName().endsWith(".mnt")) n++;
        return n;
    }

    public static long clearAll(Context c) {
        long freed = 0;
        File[] fs = dir(c).listFiles();
        if (fs != null) for (File f : fs) { freed += f.length(); f.delete(); }
        return freed;
    }
}

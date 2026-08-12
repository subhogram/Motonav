package com.motonav;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Keeps downloaded map blobs on the PHONE so they can be re-sent to the Pi
 * without another download — the key to working offline.
 *
 * Each entry is a .mnosm blob plus a small JSON sidecar describing it.
 * Stored under  <app files>/maps/
 */
public class MapCache {

    private static final String TAG = "MotoNavCache";
    private static final String DIR = "maps";
    private static final String INDEX = "index.json";

    public static class Entry {
        public String id;          // file name without extension
        public String name;        // "Bengaluru" or "Route: Mysuru"
        public String kind;        // "city" | "route"
        public long bytes;
        public long savedAt;
        public double south, west, north, east;

        public boolean covers(double lat, double lon) {
            return lat >= south && lat <= north && lon >= west && lon <= east;
        }
    }

    private static File dir(Context c) {
        File d = new File(c.getApplicationContext().getFilesDir(), DIR);
        if (!d.exists()) d.mkdirs();
        return d;
    }

    // ── index ────────────────────────────────────────────────────────────
    public static List<Entry> list(Context c) {
        List<Entry> out = new ArrayList<>();
        File idx = new File(dir(c), INDEX);
        if (!idx.exists()) return out;
        try {
            RandomAccessFile f = new RandomAccessFile(idx, "r");
            byte[] b = new byte[(int) f.length()];
            f.readFully(b);
            f.close();
            JSONArray arr = new JSONArray(new String(b, "UTF-8"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                Entry e = new Entry();
                e.id = o.getString("id");
                e.name = o.optString("name", e.id);
                e.kind = o.optString("kind", "city");
                e.bytes = o.optLong("bytes", 0);
                e.savedAt = o.optLong("savedAt", 0);
                e.south = o.optDouble("s", 0); e.west = o.optDouble("w", 0);
                e.north = o.optDouble("n", 0); e.east = o.optDouble("e", 0);
                if (new File(dir(c), e.id + ".mnosm").exists()) out.add(e);
            }
        } catch (Exception e) {
            Log.w(TAG, "index read: " + e.getMessage());
        }
        Collections.sort(out, (a, b) -> Long.compare(b.savedAt, a.savedAt));
        return out;
    }

    private static void writeIndex(Context c, List<Entry> entries) {
        try {
            JSONArray arr = new JSONArray();
            for (Entry e : entries) {
                JSONObject o = new JSONObject();
                o.put("id", e.id); o.put("name", e.name); o.put("kind", e.kind);
                o.put("bytes", e.bytes); o.put("savedAt", e.savedAt);
                o.put("s", e.south); o.put("w", e.west);
                o.put("n", e.north); o.put("e", e.east);
                arr.put(o);
            }
            FileOutputStream fo = new FileOutputStream(new File(dir(c), INDEX));
            fo.write(arr.toString().getBytes("UTF-8"));
            fo.close();
        } catch (Exception e) {
            Log.w(TAG, "index write: " + e.getMessage());
        }
    }

    // ── store / fetch ────────────────────────────────────────────────────
    public static Entry save(Context c, String name, String kind, byte[] blob,
                             double s, double w, double n, double e) {
        try {
            String id = kind + "_" + Math.abs(name.hashCode()) + "_"
                      + (System.currentTimeMillis() / 1000);
            FileOutputStream fo = new FileOutputStream(new File(dir(c), id + ".mnosm"));
            fo.write(blob);
            fo.close();

            Entry en = new Entry();
            en.id = id; en.name = name; en.kind = kind;
            en.bytes = blob.length; en.savedAt = System.currentTimeMillis();
            en.south = s; en.west = w; en.north = n; en.east = e;

            List<Entry> all = list(c);
            all.add(en);
            writeIndex(c, all);
            Log.i(TAG, "cached " + name + " (" + blob.length / 1024 + " KB)");
            return en;
        } catch (Exception ex) {
            Log.e(TAG, "save", ex);
            return null;
        }
    }

    public static byte[] load(Context c, String id) {
        try {
            File f = new File(dir(c), id + ".mnosm");
            if (!f.exists()) return null;
            RandomAccessFile r = new RandomAccessFile(f, "r");
            byte[] b = new byte[(int) r.length()];
            r.readFully(b);
            r.close();
            return b;
        } catch (Exception e) {
            Log.w(TAG, "load: " + e.getMessage());
            return null;
        }
    }

    /** Best cached map covering a point — prefers the most recent route map. */
    public static Entry findCovering(Context c, double lat, double lon) {
        Entry best = null;
        for (Entry e : list(c)) {
            if (!e.covers(lat, lon)) continue;
            if (best == null) { best = e; continue; }
            boolean betterKind = "route".equals(e.kind) && !"route".equals(best.kind);
            if (betterKind || e.savedAt > best.savedAt) best = e;
        }
        return best;
    }

    // ── housekeeping ─────────────────────────────────────────────────────
    public static long totalBytes(Context c) {
        long t = 0;
        for (Entry e : list(c)) t += e.bytes;
        return t;
    }

    public static int count(Context c) { return list(c).size(); }

    public static void delete(Context c, String id) {
        new File(dir(c), id + ".mnosm").delete();
        List<Entry> all = list(c);
        for (int i = all.size() - 1; i >= 0; i--)
            if (all.get(i).id.equals(id)) all.remove(i);
        writeIndex(c, all);
    }

    public static long clearAll(Context c) {
        long freed = 0;
        File d = dir(c);
        File[] fs = d.listFiles();
        if (fs != null) {
            for (File f : fs) {
                freed += f.length();
                f.delete();
            }
        }
        Log.i(TAG, "cache cleared (" + freed / 1024 + " KB)");
        return freed;
    }

    /** Push a cached map to the Pi in base64 chunks. */
    public static boolean sendToPi(Context c, String id, RegionDownloader.Progress cb) {
        byte[] blob = load(c, id);
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
                if (cb != null && sent % (128 * 1024) < CHUNK)
                    cb.onStatus("Restoring map " + (sent * 100 / blob.length) + "%");
                try { Thread.sleep(6); } catch (InterruptedException ignored) {}
            }
            Link.send(c, "{\"type\":\"region_end\"}");
            return true;
        } catch (Exception e) {
            Log.e(TAG, "sendToPi", e);
            return false;
        }
    }
}

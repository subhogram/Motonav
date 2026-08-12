package com.motonav;

import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;

/**
 * Downloads OSM road geometry for a region from the free Overpass API,
 * converts it to MotoNav's compact .mnosm format, and streams it to the Pi.
 *
 * No API key. No Google. Overpass is a public OSM service.
 */
public class RegionDownloader {

    private static final String TAG = "MotoNavOSM";

    // Public Overpass endpoints (tried in order)
    private static final String[] ENDPOINTS = {
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    };

    /** How much road detail to fetch — big regions need less. */
    public enum Detail {
        MAJOR("motorway|trunk|primary|motorway_link|trunk_link|primary_link",
              "Major roads", Integer.MAX_VALUE),
        NORMAL("motorway|trunk|primary|secondary|tertiary|motorway_link|" +
               "trunk_link|primary_link|secondary_link|tertiary_link",
               "Main + secondary", 30000),
        FULL("motorway|trunk|primary|secondary|tertiary|unclassified|residential|" +
             "living_street|motorway_link|trunk_link|primary_link|secondary_link|" +
             "tertiary_link", "Every street", 1500);

        public final String filter;
        public final String label;
        public final int maxAreaKm2;      // above this, suggest a coarser level
        Detail(String f, String l, int m) { filter = f; label = l; maxAreaKm2 = m; }
    }

    /** Suggest a sensible detail level for a region's size. */
    public static Detail suggestDetail(double areaKm2) {
        if (areaKm2 <= Detail.FULL.maxAreaKm2) return Detail.FULL;
        if (areaKm2 <= Detail.NORMAL.maxAreaKm2) return Detail.NORMAL;
        return Detail.MAJOR;
    }

    /**
     * Rough download size in MB. Road density varies enormously between a
     * city and open country, so treat this as an order-of-magnitude guide.
     */
    public static double estimateMb(double areaKm2, Detail d) {
        double perKm2 = (d == Detail.FULL)   ? 0.020     // dense urban streets
                      : (d == Detail.NORMAL) ? 0.0020
                                             : 0.00003;  // trunk network only
        return Math.max(0.05, areaKm2 * perKm2);
    }

    private static final String HIGHWAYS =
        "motorway|trunk|primary|secondary|tertiary|unclassified|residential|" +
        "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|living_street";

    public interface Progress {
        void onStatus(String msg);
        void onDone(boolean ok, String msg);
    }

    private static int classOf(String hw) {
        if (hw == null) return 7;
        if (hw.startsWith("motorway")) return 1;
        if (hw.startsWith("trunk")) return 2;
        if (hw.startsWith("primary")) return 3;
        if (hw.startsWith("secondary")) return 4;
        if (hw.startsWith("tertiary")) return 5;
        if (hw.equals("residential") || hw.equals("living_street")) return 6;
        return 7;
    }


    /**
     * Download only a corridor around a planned route — typically a few MB
     * instead of tens, and no pre-planning needed.
     *
     * Uses Overpass's polyline "around:" filter, which selects everything
     * within `radiusM` of the line. The route is sampled down so the query
     * stays small, and long routes are split into chunks.
     */
    public static void runCorridor(final android.content.Context ctx,
                                   final List<double[]> route,
                                   final int radiusM,
                                   final Detail detail,
                                   final Progress cb) {
        new Thread(() -> {
            try {
                if (route == null || route.size() < 2) {
                    cb.onDone(false, "No route to download around");
                    return;
                }

                // sample the line so consecutive points are ~2 km apart;
                // the around: radius covers everything between them
                List<double[]> samples = sampleEvery(route, Math.max(1200, radiusM * 0.4));
                if (samples.size() < 2) samples = route;

                final int PER_CHUNK = 40;          // points per Overpass query
                int chunks = (int) Math.ceil(samples.size() / (double) PER_CHUNK);
                List<byte[]> allWays = new ArrayList<>();
                int wayCount = 0;

                double minLat = 90, minLon = 180, maxLat = -90, maxLon = -180;
                for (double[] p : route) {
                    minLat = Math.min(minLat, p[0]); maxLat = Math.max(maxLat, p[0]);
                    minLon = Math.min(minLon, p[1]); maxLon = Math.max(maxLon, p[1]);
                }
                double padLat = radiusM / 111320.0;
                double padLon = radiusM / (111320.0 * Math.cos(Math.toRadians(minLat)));
                minLat -= padLat; maxLat += padLat;
                minLon -= padLon; maxLon += padLon;

                for (int ci = 0; ci < chunks; ci++) {
                    int from = ci * PER_CHUNK;
                    int to = Math.min(samples.size(), from + PER_CHUNK + 1);  // overlap 1
                    StringBuilder line = new StringBuilder();
                    for (int i = from; i < to; i++) {
                        if (line.length() > 0) line.append(',');
                        line.append(String.format(java.util.Locale.US, "%.5f,%.5f",
                            samples.get(i)[0], samples.get(i)[1]));
                    }

                    cb.onStatus("Corridor " + (ci + 1) + "/" + chunks + " — requesting…");
                    String q = "[out:json][timeout:180];"
                        + "way[\"highway\"~\"^(" + detail.filter + ")$\"]"
                        + "(around:" + radiusM + "," + line + ");"
                        + "out geom;";

                    String json = null;
                    for (String ep : ENDPOINTS) {
                        try {
                            json = post(ep, "data="
                                + java.net.URLEncoder.encode(q, "UTF-8"), cb);
                            if (json != null && json.length() > 60) break;
                        } catch (Exception e) {
                            Log.w(TAG, "corridor " + (ci + 1) + ": " + e.getMessage());
                        }
                    }
                    if (json == null) {
                        cb.onStatus("Chunk " + (ci + 1) + " failed — continuing");
                        continue;
                    }
                    wayCount += collectWays(json, allWays);
                    cb.onStatus("Corridor " + (ci + 1) + "/" + chunks
                                + " — " + wayCount + " roads");
                    try { Thread.sleep(1000); } catch (InterruptedException ignored) {}
                }

                if (wayCount == 0) {
                    cb.onDone(false, "No roads found along the route");
                    return;
                }

                byte[] blob = pack(allWays, wayCount, minLat, minLon, maxLat, maxLon);
                MapCache.save(ctx, "Route corridor", "route", blob,
                              minLat, minLon, maxLat, maxLon);
                // also file it into the tile library so a repeat trip is offline
                try {
                    java.util.List<Object[]> tw = blobToWays(blob);
                    TileCache.storeWays(ctx, tw);
                } catch (Exception ignored) {}
                cb.onStatus("Sending " + (blob.length / 1024) + " KB to the Pi…");
                boolean ok = sendToPi(ctx, blob, cb);
                cb.onDone(ok, ok ? ("Route map: " + wayCount + " roads, "
                                    + (blob.length / 1024) + " KB")
                                 : "Send failed — check the Pi connection");
            } catch (Exception e) {
                Log.e(TAG, "corridor", e);
                cb.onDone(false, "Error: " + e.getMessage());
            }
        }, "corridor-dl").start();
    }

    /** Thin a polyline so consecutive points are at least `minM` apart. */
    private static List<double[]> sampleEvery(List<double[]> pts, double minM) {
        List<double[]> out = new ArrayList<>();
        double[] prev = null;
        for (int i = 0; i < pts.size(); i++) {
            double[] p = pts.get(i);
            boolean last = (i == pts.size() - 1);
            if (prev == null || last) { out.add(p); prev = p; continue; }
            double d = Math.hypot((p[0] - prev[0]) * 111320.0,
                (p[1] - prev[1]) * 111320.0 * Math.cos(Math.toRadians(p[0])));
            if (d >= minM) { out.add(p); prev = p; }
        }
        return out;
    }


    /**
     * Download a region and SAVE IT ON THE PHONE as tiles. Nothing is sent
     * to the Pi here — slices are handed over when navigation starts.
     */
    public static void downloadRegion(final android.content.Context ctx,
                                      final String name,
                                      final double south, final double west,
                                      final double north, final double east,
                                      final Detail detail,
                                      final Progress2 cb) {
        new Thread(() -> {
            try {
                double maxSpan = (detail == Detail.FULL) ? 0.20
                               : (detail == Detail.NORMAL) ? 0.60 : 1.50;
                int cols = (int) Math.max(1, Math.ceil((east - west) / maxSpan));
                int rows = (int) Math.max(1, Math.ceil((north - south) / maxSpan));
                int total = cols * rows;
                if (total > 120) {
                    cb.done(false, "Area too large — pick a smaller region "
                                 + "or a coarser detail level");
                    return;
                }

                int wayCount = 0, tilesTouched = 0, tileNo = 0;
                for (int r = 0; r < rows; r++) {
                    for (int c = 0; c < cols; c++) {
                        tileNo++;
                        double s0 = south + (north - south) * r / rows;
                        double n0 = south + (north - south) * (r + 1) / rows;
                        double w0 = west + (east - west) * c / cols;
                        double e0 = west + (east - west) * (c + 1) / cols;

                        com.motonav.Progress.within(
                            com.motonav.Progress.Phase.DOWNLOADING,
                            (tileNo - 1) / (double) total,
                            "area " + tileNo + " of " + total);

                        String q = "[out:json][timeout:180];"
                            + "way[\"highway\"~\"^(" + detail.filter + ")$\"]"
                            + "(" + s0 + "," + w0 + "," + n0 + "," + e0 + ");"
                            + "out geom;";
                        final int currentTile = tileNo;
                        String json = null;
                        for (String ep : ENDPOINTS) {
                            try {
                                json = post(ep, "data="
                                    + java.net.URLEncoder.encode(q, "UTF-8"),
                                    new Progress() {
                                        @Override public void onStatus(String m) {
                                            com.motonav.Progress.within(
                                                com.motonav.Progress.Phase.DOWNLOADING,
                                                (currentTile - 1) / (double) total, m);
                                        }
                                        @Override public void onDone(boolean o, String m) {}
                                    });
                                if (json != null && json.length() > 60) break;
                            } catch (Exception e) {
                                Log.w(TAG, "area " + tileNo + ": " + e.getMessage());
                            }
                        }
                        if (json == null) continue;

                        com.motonav.Progress.within(
                            com.motonav.Progress.Phase.SAVING,
                            tileNo / (double) total, "saving area " + tileNo);

                        List<Object[]> ways = jsonToWays(json);
                        wayCount += ways.size();
                        tilesTouched += TileCache.storeWays(ctx, ways);
                        try { Thread.sleep(900); } catch (InterruptedException ignored) {}
                    }
                }

                if (wayCount == 0) {
                    cb.done(false, "No roads found in " + name);
                    return;
                }
                long bytes = TileCache.totalBytes(ctx);
                TileCache.addRegion(ctx, name, south, west, north, east,
                                    tilesTouched, wayCount, bytes);
                com.motonav.Progress.done(name + " saved");
                cb.done(true, name + ": " + wayCount + " roads saved on the phone");
            } catch (Exception e) {
                Log.e(TAG, "downloadRegion", e);
                com.motonav.Progress.fail(e.getMessage());
                cb.done(false, "Error: " + e.getMessage());
            }
        }, "region-save").start();
    }

    /** Decode a .mnosm blob back into ways so it can be filed as tiles. */
    static java.util.List<Object[]> blobToWays(byte[] blob) {
        java.util.List<Object[]> out = new ArrayList<>();
        java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(blob)
            .order(java.nio.ByteOrder.LITTLE_ENDIAN);
        bb.position(6 + 32);
        int n = bb.getInt();
        for (int i = 0; i < n && bb.remaining() > 3; i++) {
            int cls = bb.get() & 0xFF;
            int npts = bb.getShort() & 0xFFFF;
            if (bb.remaining() < npts * 8) break;
            double[] flat = new double[npts * 2];
            for (int k = 0; k < npts; k++) {
                flat[k * 2] = bb.getInt() / 1e6;
                flat[k * 2 + 1] = bb.getInt() / 1e6;
            }
            out.add(new Object[]{cls, flat});
        }
        return out;
    }

    public interface Progress2 { void done(boolean ok, String msg); }

    /** Radius used when deciding which tiles a route needs. */
    public static double corridorRadius() { return Router.CORRIDOR_M; }

    /** Overpass JSON -> way list for TileCache. */
    static List<Object[]> jsonToWays(String json) throws Exception {
        List<Object[]> out = new ArrayList<>();
        JSONObject root = new JSONObject(json);
        JSONArray els = root.optJSONArray("elements");
        if (els == null) return out;
        for (int i = 0; i < els.length(); i++) {
            JSONObject el = els.getJSONObject(i);
            if (!"way".equals(el.optString("type"))) continue;
            JSONArray geom = el.optJSONArray("geometry");
            if (geom == null || geom.length() < 2) continue;
            JSONObject tags = el.optJSONObject("tags");
            int cls = classOf(tags == null ? null : tags.optString("highway", null));

            List<double[]> pts = new ArrayList<>();
            double plat = 0, plon = 0;
            for (int k = 0; k < geom.length(); k++) {
                JSONObject g = geom.getJSONObject(k);
                double la = g.optDouble("lat"), lo = g.optDouble("lon");
                if (!pts.isEmpty()) {
                    double dm = Math.hypot((la - plat) * 111320.0,
                        (lo - plon) * 111320.0 * Math.cos(Math.toRadians(la)));
                    if (dm < 8 && k != geom.length() - 1) continue;
                }
                pts.add(new double[]{la, lo});
                plat = la; plon = lo;
            }
            if (pts.size() < 2) continue;
            double[] flat = new double[pts.size() * 2];
            for (int k = 0; k < pts.size(); k++) {
                flat[k * 2] = pts.get(k)[0];
                flat[k * 2 + 1] = pts.get(k)[1];
            }
            out.add(new Object[]{cls, flat});
        }
        return out;
    }

    /** Blocking version of runBBox — used by the gap filler, which sequences areas. */
    public static void runBBoxSync(android.content.Context ctx, String name,
                                   double south, double west,
                                   double north, double east,
                                   Detail detail, Progress cb) {
        final Object lock = new Object();
        final boolean[] done = {false};
        runBBox(ctx, name, south, west, north, east, detail, new Progress() {
            @Override public void onStatus(String m) { cb.onStatus(m); }
            @Override public void onDone(boolean ok, String m) {
                cb.onDone(ok, m);
                synchronized (lock) { done[0] = true; lock.notifyAll(); }
            }
        });
        synchronized (lock) {
            long deadline = System.currentTimeMillis() + 300000;
            while (!done[0] && System.currentTimeMillis() < deadline) {
                try { lock.wait(2000); } catch (InterruptedException e) { break; }
            }
        }
    }

    /**
     * Download a named region by bounding box, tiling if it's large.
     * This is what the region browser calls.
     */
    public static void runBBox(final android.content.Context ctx,
                               final String regionName,
                               final double south, final double west,
                               final double north, final double east,
                               final Detail detail, final Progress cb) {
        new Thread(() -> {
            try {
                // split into tiles so no single Overpass query is huge
                double maxSpan = (detail == Detail.FULL) ? 0.20
                               : (detail == Detail.NORMAL) ? 0.60 : 1.50;
                int cols = (int) Math.max(1, Math.ceil((east - west) / maxSpan));
                int rows = (int) Math.max(1, Math.ceil((north - south) / maxSpan));
                int tiles = cols * rows;
                if (tiles > 60) {
                    cb.onDone(false, "Region too large — pick a smaller area "
                                   + "or a coarser detail level");
                    return;
                }

                List<byte[]> allWays = new ArrayList<>();
                int wayCount = 0;

                for (int r = 0; r < rows; r++) {
                    for (int c = 0; c < cols; c++) {
                        int idx = r * cols + c + 1;
                        double s0 = south + (north - south) * r / rows;
                        double n0 = south + (north - south) * (r + 1) / rows;
                        double w0 = west + (east - west) * c / cols;
                        double e0 = west + (east - west) * (c + 1) / cols;

                        cb.onStatus("Tile " + idx + "/" + tiles + " — requesting…");
                        String q = "[out:json][timeout:180];"
                            + "way[\"highway\"~\"^(" + detail.filter + ")$\"]"
                            + "(" + s0 + "," + w0 + "," + n0 + "," + e0 + ");"
                            + "out geom;";

                        String json = null;
                        for (String ep : ENDPOINTS) {
                            try {
                                json = post(ep, "data="
                                    + java.net.URLEncoder.encode(q, "UTF-8"), cb);
                                if (json != null && json.length() > 60) break;
                            } catch (Exception e) {
                                Log.w(TAG, "tile " + idx + " " + ep + ": " + e.getMessage());
                            }
                        }
                        if (json == null) {
                            cb.onStatus("Tile " + idx + " failed — continuing");
                            continue;
                        }
                        int got = collectWays(json, allWays);
                        wayCount += got;
                        cb.onStatus("Tile " + idx + "/" + tiles + " — "
                                    + wayCount + " roads so far");
                        // be gentle with the shared service
                        try { Thread.sleep(1200); } catch (InterruptedException ignored) {}
                    }
                }

                if (wayCount == 0) {
                    cb.onDone(false, "No roads found in " + regionName);
                    return;
                }

                byte[] blob = pack(allWays, wayCount, south, west, north, east);
                MapCache.save(ctx, regionName, "city", blob, south, west, north, east);
                cb.onStatus("Sending " + (blob.length / 1024) + " KB to the Pi…");
                boolean ok = sendToPi(ctx, blob, cb);
                cb.onDone(ok, ok ? (regionName + ": " + wayCount + " roads, "
                                    + (blob.length / 1024) + " KB")
                                 : "Send failed — check the Pi connection");
            } catch (Exception e) {
                Log.e(TAG, "runBBox", e);
                cb.onDone(false, "Error: " + e.getMessage());
            }
        }, "region-bbox").start();
    }

    /** Run the whole download → convert → send pipeline. Call off the UI thread. */
    public static void run(final android.content.Context ctx,
                           final double lat, final double lon,
                           final double radiusKm, final Progress cb) {
        new Thread(() -> {
            try {
                cb.onStatus("Requesting map data…");
                double dLat = radiusKm / 111.32;
                double dLon = radiusKm / (111.32 * Math.cos(Math.toRadians(lat)));
                double south = lat - dLat, north = lat + dLat;
                double west = lon - dLon, east = lon + dLon;

                String q = "[out:json][timeout:180];"
                    + "way[\"highway\"~\"^(" + HIGHWAYS + ")$\"]"
                    + "(" + south + "," + west + "," + north + "," + east + ");"
                    + "out geom;";

                String json = null;
                for (String ep : ENDPOINTS) {
                    try {
                        json = post(ep, "data=" + java.net.URLEncoder.encode(q, "UTF-8"), cb);
                        if (json != null && json.length() > 100) break;
                    } catch (Exception e) {
                        Log.w(TAG, "endpoint failed " + ep + ": " + e.getMessage());
                    }
                }
                if (json == null) { cb.onDone(false, "Download failed"); return; }

                cb.onStatus("Converting…");
                byte[] blob = convert(json, south, west, north, east);
                if (blob == null || blob.length < 32) {
                    cb.onDone(false, "No roads found in that area");
                    return;
                }

                cb.onStatus("Sending to Pi (" + (blob.length / 1024) + " KB)…");
                boolean ok = sendToPi(ctx, blob, cb);
                cb.onDone(ok, ok ? ("Map sent — " + (blob.length / 1024) + " KB")
                                 : "Send failed — check the Pi connection");
            } catch (Exception e) {
                Log.e(TAG, "region", e);
                cb.onDone(false, "Error: " + e.getMessage());
            }
        }, "region-dl").start();
    }

    private static String post(String endpoint, String body, Progress cb) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(endpoint).openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(20000);
        c.setReadTimeout(200000);
        c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
        c.getOutputStream().write(body.getBytes("UTF-8"));

        int code = c.getResponseCode();
        if (code != 200) throw new Exception("HTTP " + code);

        InputStream in = c.getInputStream();
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n, total = 0;
        while ((n = in.read(buf)) > 0) {
            bo.write(buf, 0, n);
            total += n;
            if (total % (256 * 1024) < 16384) {
                cb.onStatus("Downloading… " + (total / 1024) + " KB");
            }
        }
        in.close();
        return bo.toString("UTF-8");
    }

    /** Overpass JSON -> .mnosm binary (single bbox) */
    private static byte[] convert(String json, double s, double w, double n, double e)
            throws Exception {
        List<byte[]> ways = new ArrayList<>();
        int count = collectWays(json, ways);
        if (count == 0) return null;
        return pack(ways, count, s, w, n, e);
    }

    /** Append encoded ways from one Overpass response. Returns how many. */
    private static int collectWays(String json, List<byte[]> ways) throws Exception {
        JSONObject root = new JSONObject(json);
        JSONArray els = root.optJSONArray("elements");
        if (els == null) return 0;
        int count = 0;

        for (int i = 0; i < els.length(); i++) {
            JSONObject el = els.getJSONObject(i);
            if (!"way".equals(el.optString("type"))) continue;
            JSONArray geom = el.optJSONArray("geometry");
            if (geom == null || geom.length() < 2) continue;

            JSONObject tags = el.optJSONObject("tags");
            int cls = classOf(tags == null ? null : tags.optString("highway", null));

            // simplify: drop points closer than ~8 m apart
            List<double[]> pts = new ArrayList<>();
            double plat = 0, plon = 0;
            for (int k = 0; k < geom.length(); k++) {
                JSONObject g = geom.getJSONObject(k);
                double la = g.optDouble("lat"), lo = g.optDouble("lon");
                if (!pts.isEmpty()) {
                    double dm = Math.hypot((la - plat) * 111320.0,
                        (lo - plon) * 111320.0 * Math.cos(Math.toRadians(la)));
                    boolean last = (k == geom.length() - 1);
                    if (dm < 8 && !last) continue;
                }
                pts.add(new double[]{la, lo});
                plat = la; plon = lo;
            }
            if (pts.size() < 2 || pts.size() > 65535) continue;

            ByteBuffer wb = ByteBuffer.allocate(3 + pts.size() * 8)
                                     .order(ByteOrder.LITTLE_ENDIAN);
            wb.put((byte) cls);
            wb.putShort((short) pts.size());
            for (double[] p : pts) {
                wb.putInt((int) Math.round(p[0] * 1e6));
                wb.putInt((int) Math.round(p[1] * 1e6));
            }
            ways.add(wb.array());
            count++;
        }
        return count;
    }

    /** Wrap encoded ways in the .mnosm header. */
    private static byte[] pack(List<byte[]> ways, int count,
                               double s, double w, double n, double e)
            throws Exception {
        int total = 6 + 8 * 4 + 4;
        for (byte[] b : ways) total += b.length;
        ByteBuffer out = ByteBuffer.allocate(total).order(ByteOrder.LITTLE_ENDIAN);
        out.put("MNOSM1".getBytes("US-ASCII"));
        out.putDouble(s); out.putDouble(w); out.putDouble(n); out.putDouble(e);
        out.putInt(count);
        for (byte[] b : ways) out.put(b);
        Log.i(TAG, "converted " + count + " ways -> " + total + " bytes");
        return out.array();
    }

    /** Stream the blob to the Pi in base64 chunks. */
    private static boolean sendToPi(android.content.Context ctx, byte[] blob, Progress cb) {
        try {
            if (!Link.isConnected(ctx)) return false;
            Link.send(ctx, "{\"type\":\"region_begin\",\"bytes\":" + blob.length + "}");

            final int CHUNK = 8192;             // raw bytes per chunk
            int sent = 0;
            while (sent < blob.length) {
                int len = Math.min(CHUNK, blob.length - sent);
                String b64 = Base64.encodeToString(blob, sent, len, Base64.NO_WRAP);
                Link.send(ctx, "{\"type\":\"region_chunk\",\"d\":\"" + b64 + "\"}");
                sent += len;
                if (sent % (128 * 1024) < CHUNK) {
                    cb.onStatus("Sending… " + (sent * 100 / blob.length) + "%");
                }
                try { Thread.sleep(6); } catch (InterruptedException ignored) {}
            }
            Link.send(ctx, "{\"type\":\"region_end\"}");
            return true;
        } catch (Exception e) {
            Log.e(TAG, "send", e);
            return false;
        }
    }
}

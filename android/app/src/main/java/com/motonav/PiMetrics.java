package com.motonav;

import android.content.Context;
import android.util.Log;

import org.json.JSONObject;

/**
 * Holds the last metrics snapshot the Pi sent back, and drives the
 * navigation lifecycle (start / end) from the phone.
 */
public class PiMetrics {

    private static final String TAG = "MotoNavMetrics";

    public static volatile long regionBytes = -1;
    public static volatile String regionInfo = "";
    public static volatile long diskFree = 0, diskTotal = 0;
    public static volatile int routePts = 0, steps = 0;
    public static volatile String dest = "", mapMode = "";
    public static volatile int uptimeS = 0;
    public static volatile double load1 = 0, cpuC = 0;
    public static volatile int tileCount = 0, tileWays = 0;
    public static volatile long updatedAt = 0;
    private static volatile android.content.Context appCtx;
    public static void attach(android.content.Context c) {
        appCtx = c.getApplicationContext();
    }

    public interface Listener { void onMetrics(); }
    private static volatile Listener listener;
    public static void setListener(Listener l) { listener = l; }

    /** Called by TcpClient for every line the Pi sends. */
    public static void onMessage(String line) {
        try {
            JSONObject o = new JSONObject(line);
            String type = o.optString("type");

            if ("map_gaps".equals(type)) {
                if (appCtx != null) GapFiller.onGaps(appCtx, o);
                return;
            }
            if (!"metrics".equals(type)) return;
            regionBytes = o.optLong("region_bytes", -1);
            regionInfo  = o.optString("region_info", "");
            diskFree    = o.optLong("disk_free", 0);
            diskTotal   = o.optLong("disk_total", 0);
            routePts    = o.optInt("route_pts", 0);
            steps       = o.optInt("steps", 0);
            dest        = o.optString("dest", "");
            mapMode     = o.optString("map_mode", "");
            tileCount   = o.optInt("tiles", 0);
            tileWays    = o.optInt("tile_ways", 0);
            uptimeS     = o.optInt("uptime_s", 0);
            load1       = o.optDouble("load1", 0);
            cpuC        = o.optDouble("cpu_c", 0);
            updatedAt   = System.currentTimeMillis();
            Listener l = listener;
            if (l != null) l.onMetrics();
        } catch (Exception e) {
            Log.d(TAG, "not metrics: " + e.getMessage());
        }
    }

    public static boolean isFresh() {
        return updatedAt > 0 && System.currentTimeMillis() - updatedAt < 20000;
    }

    // ── commands to the Pi ───────────────────────────────────────────────
    public static void request(Context c)   { Link.send(c, "{\"type\":\"metrics_req\"}"); }
    public static void clearMap(Context c)  { Link.send(c, "{\"type\":\"clear_map\"}"); }

    /** Map mode: "minimal" for Google navigation, "detailed" for OSM. */
    public static void setMapMode(Context c, String mode) {
        Link.send(c, "{\"type\":\"mapmode\",\"mode\":\"" + mode + "\"}");
    }

    /**
     * End navigation: the Pi drops the route, steps, trail and its cached
     * map, and returns to the idle screen. The phone keeps its own cache.
     */
    public static void endNavigation(Context c) {
        Router.clear(c);
        Link.send(c, "{\"type\":\"end_nav\"}");
        Log.i(TAG, "navigation ended from phone");
    }

    /**
     * Restore a cached map for this position, if we have one — so a repeat
     * trip needs no download at all.
     */
    public static boolean restoreCachedMap(final Context c, double lat, double lon,
                                           RegionDownloader.Progress cb) {
        MapCache.Entry e = MapCache.findCovering(c, lat, lon);
        if (e == null) return false;
        if (cb != null) cb.onStatus("Restoring cached map: " + e.name);
        boolean ok = MapCache.sendToPi(c, e.id, cb);
        if (cb != null) cb.onDone(ok, ok ? ("Restored " + e.name + " from cache")
                                         : "Cache restore failed");
        return ok;
    }

    public static String human(long bytes) {
        if (bytes < 0) return "—";
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024) + " KB";
        return String.format("%.1f MB", bytes / (1024.0 * 1024.0));
    }

    public static String uptime() {
        int s = uptimeS;
        if (s <= 0) return "—";
        int h = s / 3600, m = (s % 3600) / 60;
        return h > 0 ? (h + "h " + m + "m") : (m + "m");
    }
}

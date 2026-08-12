package com.motonav;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * Routes to a destination using free OSM services — no API key, no Google.
 *
 *   destination link  ->  LinkResolver  ->  coordinates
 *   coordinates       ->  OSRM          ->  geometry + turn steps + duration
 *   geometry          ->  Overpass      ->  3 km corridor of streets
 *
 * Everything is sent to the Pi, which then navigates on its own.
 */
public class Router {

    private static final String TAG = "MotoNavRoute";
    private static final String OSRM =
        "https://router.project-osrm.org/route/v1/driving/";
    private static final String UA = "MotoNav/3.0 (personal motorcycle display)";

    public static final int CORRIDOR_M = 3000;      // 3 km around the route

    // How far up the route we prep tiles for when navigation starts. Long
    // trips are streamed the rest of the way — see GapFiller / PiMetrics,
    // which top the buffer up as the Pi reports the rider approaching its
    // edge — instead of pulling the whole route down before setting off.
    public static final double BUFFER_AHEAD_KM = 15.0;

    public static class Step {
        public double lat, lon;
        public String maneuver, text, road;
    }

    private static volatile String activeDest = "";
    private static volatile double destLat, destLon;
    private static volatile List<double[]> activeRoute = new ArrayList<>();
    private static volatile List<Step> activeSteps = new ArrayList<>();
    private static volatile double totalKm = 0, totalSec = 0;
    private static volatile boolean busy = false;

    public interface Cb { void onResult(boolean ok, String msg); }

    public static String activeDestination() { return activeDest; }
    public static boolean hasRoute() { return !activeRoute.isEmpty(); }
    public static double km() { return totalKm; }
    public static double seconds() { return totalSec; }

    public static void clear(Context ctx) {
        activeDest = "";
        activeRoute = new ArrayList<>();
        activeSteps = new ArrayList<>();
        totalKm = totalSec = 0;
        try { Link.send(ctx, "{\"type\":\"route_clear\"}"); } catch (Exception ignored) {}
    }

    /**
     * The single entry point: a shared Google Maps link (or coordinates,
     * or a place name) becomes a full navigation session on the Pi.
     */
    public static void navigateTo(final Context ctx, final String sharedText,
                                  final double fromLat, final double fromLon,
                                  final Cb cb) {
        if (busy) { report(cb, false, "Already working…"); return; }
        busy = true;
        new Thread(() -> {
            try {
                Progress.set(Progress.Phase.RESOLVING, "reading the link");
                LinkResolver.Place dest = LinkResolver.resolve(sharedText);
                if (!dest.ok) {
                    Progress.fail("couldn't read that link");
                    report(cb, false, "Couldn't read that link");
                    return;
                }
                report(cb, true, "Destination: " + dest.label);

                Progress.set(Progress.Phase.ROUTING, dest.label);
                List<double[]> pts = osrm(fromLat, fromLon, dest.lat, dest.lon);
                if (pts == null || pts.size() < 2) {
                    Progress.fail("no route found");
                    report(cb, false, "No route found");
                    return;
                }

                activeDest = dest.label;
                destLat = dest.lat; destLon = dest.lon;
                activeRoute = pts;

                // tell the Pi the time first so its clock and ETA are right
                sendTime(ctx);
                sendRoute(ctx);
                sendSteps(ctx);
                report(cb, true, String.format("Route: %.1f km, %d min",
                        totalKm, (int) Math.round(totalSec / 60)));

                // ── send the Pi exactly the tiles the ride needs right now,
                //    taken from the map library already on the phone. Only
                //    the initial buffer window is prepped here — a long
                //    route streams in the rest as it's driven, instead of
                //    the whole trip landing on the Pi before setting off.
                Progress.set(Progress.Phase.SLICING, "checking offline library");
                java.util.List<double[]> bufferPts = windowFromStart(pts, BUFFER_AHEAD_KM);
                java.util.List<String> needed = TileCache.keysAlong(bufferPts, CORRIDOR_M);
                java.util.List<String> have = TileCache.haveOf(ctx, needed);
                java.util.List<String> lack = TileCache.missingOf(ctx, needed);
                report(cb, true, "Offline map: " + have.size() + "/" + needed.size()
                                 + " buffer tiles on the phone");

                if (!have.isEmpty()) {
                    Progress.set(Progress.Phase.SENDING, have.size() + " tiles \u2192 Pi");
                    TileCache.sendSlice(ctx, have, new RegionDownloader.Progress() {
                        @Override public void onStatus(String m) {
                            Progress.within(Progress.Phase.SENDING, 0.5, m);
                        }
                        @Override public void onDone(boolean ok, String m) {}
                    });
                }

                // fetch only what the phone genuinely lacks, and only for
                // the buffer — the rest streams in via map_gaps as the Pi
                // sees the rider approach the edge of what it already has
                if (!lack.isEmpty()) {
                    Progress.set(Progress.Phase.DOWNLOADING,
                        lack.size() + " tiles missing");
                    RegionDownloader.Detail det = (totalKm > 120)
                        ? RegionDownloader.Detail.NORMAL
                        : RegionDownloader.Detail.FULL;
                    RegionDownloader.runCorridor(ctx, bufferPts, CORRIDOR_M, det,
                        new RegionDownloader.Progress() {
                            @Override public void onStatus(String m) {
                                Progress.within(Progress.Phase.DOWNLOADING, 0.5, m);
                                report(cb, true, m);
                            }
                            @Override public void onDone(boolean ok, String m) {
                                Progress.done(m);
                                report(cb, ok, m);
                            }
                        });
                } else {
                    Progress.done("ready — map came from the phone library");
                }
            } catch (Exception e) {
                Log.e(TAG, "navigateTo", e);
                report(cb, false, "Error: " + e.getMessage());
            } finally {
                busy = false;
            }
        }, "router").start();
    }

    /** Re-route from the current position if we've drifted off the line. */
    public static void checkOffRoute(Context ctx, double lat, double lon) {
        List<double[]> r = activeRoute;
        if (r.size() < 2 || busy || activeDest.isEmpty()) return;
        double best = Double.MAX_VALUE;
        for (double[] p : r) {
            double d = Math.hypot((p[0] - lat) * 111320.0,
                (p[1] - lon) * 111320.0 * Math.cos(Math.toRadians(lat)));
            if (d < best) best = d;
            if (best < 60) return;
        }
        if (best > 150) {
            Log.i(TAG, "off-route " + (int) best + " m — re-routing");
            busy = true;
            new Thread(() -> {
                try {
                    List<double[]> pts = osrm(lat, lon, destLat, destLon);
                    if (pts != null && pts.size() > 1) {
                        activeRoute = pts;
                        sendRoute(ctx);
                        sendSteps(ctx);
                    }
                } catch (Exception e) {
                    Log.w(TAG, "re-route: " + e.getMessage());
                } finally { busy = false; }
            }, "reroute").start();
        }
    }

    /** First aheadKm of a route, measured along the polyline from its start. */
    static List<double[]> windowFromStart(List<double[]> pts, double aheadKm) {
        List<double[]> out = new ArrayList<>();
        if (pts.isEmpty()) return out;
        out.add(pts.get(0));
        double budget = aheadKm * 1000.0, dist = 0;
        for (int i = 1; i < pts.size() && dist < budget; i++) {
            double[] a = pts.get(i - 1), b = pts.get(i);
            dist += Math.hypot((b[0] - a[0]) * 111320.0,
                (b[1] - a[1]) * 111320.0 * Math.cos(Math.toRadians(b[0])));
            out.add(b);
        }
        return out;
    }

    // ── OSRM ─────────────────────────────────────────────────────────────
    private static List<double[]> osrm(double la1, double lo1, double la2, double lo2)
            throws Exception {
        String url = OSRM + lo1 + "," + la1 + ";" + lo2 + "," + la2
                   + "?overview=full&geometries=geojson&steps=true";
        String json = get(url);
        if (json == null) return null;
        JSONObject root = new JSONObject(json);
        JSONArray routes = root.optJSONArray("routes");
        if (routes == null || routes.length() == 0) return null;

        JSONObject r0 = routes.getJSONObject(0);
        totalKm = r0.optDouble("distance", 0) / 1000.0;
        totalSec = r0.optDouble("duration", 0);

        JSONArray coords = r0.getJSONObject("geometry").getJSONArray("coordinates");
        List<double[]> out = new ArrayList<>(coords.length());
        for (int i = 0; i < coords.length(); i++) {
            JSONArray c = coords.getJSONArray(i);
            out.add(new double[]{c.getDouble(1), c.getDouble(0)});
        }
        activeSteps = parseSteps(r0);
        return out;
    }

    private static List<Step> parseSteps(JSONObject route) {
        List<Step> out = new ArrayList<>();
        try {
            JSONArray legs = route.optJSONArray("legs");
            if (legs == null) return out;
            for (int li = 0; li < legs.length(); li++) {
                JSONArray sts = legs.getJSONObject(li).optJSONArray("steps");
                if (sts == null) continue;
                for (int si = 0; si < sts.length(); si++) {
                    JSONObject st = sts.getJSONObject(si);
                    JSONObject mv = st.optJSONObject("maneuver");
                    if (mv == null) continue;
                    JSONArray loc = mv.optJSONArray("location");
                    if (loc == null || loc.length() < 2) continue;
                    Step s = new Step();
                    s.lon = loc.getDouble(0);
                    s.lat = loc.getDouble(1);
                    s.road = st.optString("name", "");
                    String t = mv.optString("type", ""), m = mv.optString("modifier", "");
                    s.maneuver = mapManeuver(t, m);
                    s.text = describe(t, m, s.road);
                    out.add(s);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "steps: " + e.getMessage());
        }
        return out;
    }

    private static String mapManeuver(String type, String mod) {
        String t = type == null ? "" : type.toLowerCase();
        String m = mod == null ? "" : mod.toLowerCase();
        if (t.contains("arrive")) return "destination";
        if (t.contains("roundabout") || t.contains("rotary")) return "roundabout";
        if (t.contains("merge")) return "merge-left";
        if (m.contains("uturn")) return "u-turn-left";
        if (m.contains("sharp left")) return "sharp-left";
        if (m.contains("sharp right")) return "sharp-right";
        if (m.contains("slight left")) return "slight-left";
        if (m.contains("slight right")) return "slight-right";
        if (m.contains("left")) return "turn-left";
        if (m.contains("right")) return "turn-right";
        return "straight";
    }

    private static String describe(String type, String mod, String road) {
        String t = type == null ? "" : type.toLowerCase();
        String m = mod == null ? "" : mod.toLowerCase();
        String onto = (road == null || road.trim().isEmpty()) ? "" : " onto " + road;
        if (t.contains("arrive")) return "You have arrived";
        if (t.contains("depart")) return "Head off" + onto;
        if (t.contains("roundabout") || t.contains("rotary")) return "Take the roundabout";
        if (t.contains("merge")) return "Merge" + onto;
        if (t.contains("fork")) return "Keep " + (m.contains("left") ? "left" : "right") + onto;
        if (m.contains("uturn")) return "Make a U-turn";
        if (m.contains("sharp left")) return "Sharp left" + onto;
        if (m.contains("sharp right")) return "Sharp right" + onto;
        if (m.contains("slight left")) return "Slight left" + onto;
        if (m.contains("slight right")) return "Slight right" + onto;
        if (m.contains("left")) return "Turn left" + onto;
        if (m.contains("right")) return "Turn right" + onto;
        return "Continue straight" + onto;
    }

    // ── transmit ─────────────────────────────────────────────────────────
    /** Give the Pi our clock so its time display and ETA are correct. */
    public static void sendTime(Context ctx) {
        long epoch = System.currentTimeMillis() / 1000L;
        int off = java.util.TimeZone.getDefault().getOffset(System.currentTimeMillis()) / 1000;
        Link.send(ctx, "{\"type\":\"time\",\"epoch\":" + epoch + ",\"tz\":" + off + "}");
    }

    private static void sendRoute(Context ctx) {
        List<double[]> thin = simplify(activeRoute, 12.0);
        StringBuilder sb = new StringBuilder();
        sb.append("{\"type\":\"route\",\"dest\":\"").append(escape(activeDest))
          .append("\",\"km\":").append(String.format("%.2f", totalKm))
          .append(",\"sec\":").append((int) Math.round(totalSec))
          .append(",\"pts\":[");
        for (int i = 0; i < thin.size(); i++) {
            if (i > 0) sb.append(',');
            sb.append(Math.round(thin.get(i)[0] * 1e5)).append(',')
              .append(Math.round(thin.get(i)[1] * 1e5));
        }
        sb.append("]}");
        Link.send(ctx, sb.toString());
        Log.i(TAG, "sent route " + thin.size() + " pts");
    }

    private static void sendSteps(Context ctx) {
        List<Step> st = activeSteps;
        if (st.isEmpty()) return;
        StringBuilder sb = new StringBuilder("{\"type\":\"steps\",\"steps\":[");
        for (int i = 0; i < st.size(); i++) {
            Step s = st.get(i);
            if (i > 0) sb.append(',');
            sb.append("{\"lat\":").append(Math.round(s.lat * 1e5))
              .append(",\"lon\":").append(Math.round(s.lon * 1e5))
              .append(",\"m\":\"").append(escape(s.maneuver))
              .append("\",\"t\":\"").append(escape(s.text))
              .append("\",\"r\":\"").append(escape(s.road)).append("\"}");
        }
        sb.append("]}");
        Link.send(ctx, sb.toString());
        Log.i(TAG, "sent " + st.size() + " steps");
    }

    private static List<double[]> simplify(List<double[]> pts, double minM) {
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

    private static String get(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestProperty("User-Agent", UA);
        c.setConnectTimeout(12000);
        c.setReadTimeout(30000);
        if (c.getResponseCode() != 200) {
            Log.w(TAG, "HTTP " + c.getResponseCode());
            return null;
        }
        InputStream in = c.getInputStream();
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = in.read(b)) > 0) bo.write(b, 0, n);
        in.close();
        return bo.toString("UTF-8");
    }

    private static String escape(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static void report(Cb cb, boolean ok, String msg) {
        Log.i(TAG, msg);
        if (cb != null) cb.onResult(ok, msg);
    }
}

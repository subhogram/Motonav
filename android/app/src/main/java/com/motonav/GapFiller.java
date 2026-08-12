package com.motonav;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * The Pi tells us which stretches of the route it has no map for; we fetch
 * exactly those boxes and append them. Nothing already on the Pi is
 * re-downloaded, so a big pbf import means almost nothing to fetch.
 */
public class GapFiller {

    private static final String TAG = "MotoNavGaps";

    public static volatile boolean enabled = true;
    private static volatile boolean busy = false;
    public static volatile String lastStatus = "";

    public interface Progress { void onStatus(String msg); }
    private static volatile Progress listener;
    public static void setListener(Progress p) { listener = p; }

    private static void say(String m) {
        lastStatus = m;
        Log.i(TAG, m);
        Progress p = listener;
        if (p != null) p.onStatus(m);
    }

    /** Called by PiMetrics when a {"type":"map_gaps"} message arrives. */
    public static void onGaps(final Context ctx, JSONObject msg) {
        if (!enabled) return;
        try {
            JSONArray boxes = msg.optJSONArray("boxes");
            int missing = msg.optInt("missing", 0);
            int have = msg.optInt("have", 0);

            if (boxes == null || boxes.length() == 0) {
                say("Pi has the whole route (" + have + " tiles)");
                return;
            }
            if (busy) { Log.i(TAG, "already filling"); return; }
            busy = true;

            final List<double[]> todo = new ArrayList<>();
            for (int i = 0; i < boxes.length(); i++) {
                JSONObject b = boxes.getJSONObject(i);
                todo.add(new double[]{
                    b.optDouble("s"), b.optDouble("w"),
                    b.optDouble("n"), b.optDouble("e")});
            }

            say("Pi is missing " + missing + " tiles — fetching " + todo.size()
                + " area(s)");

            new Thread(() -> {
                int done = 0;
                for (double[] bx : todo) {
                    done++;
                    final int idx = done;
                    final int total = todo.size();

                    // reuse a cached blob when we already downloaded this area
                    MapCache.Entry cached = MapCache.findCovering(
                        ctx, (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2);
                    if (cached != null) {
                        say("Area " + idx + "/" + total + " — from phone cache");
                        MapCache.sendToPi(ctx, cached.id, null);
                        continue;
                    }

                    say("Area " + idx + "/" + total + " — downloading");
                    RegionDownloader.runBBoxSync(ctx, "gap " + idx,
                        bx[0], bx[1], bx[2], bx[3],
                        RegionDownloader.Detail.FULL,
                        new RegionDownloader.Progress() {
                            @Override public void onStatus(String m) {
                                say("Area " + idx + "/" + total + " — " + m);
                            }
                            @Override public void onDone(boolean ok, String m) {
                                say("Area " + idx + "/" + total + " — " + m);
                            }
                        });
                }
                say("Gap fill complete");
                busy = false;
                // ask the Pi to confirm what it now has
                Link.send(ctx, "{\"type\":\"gaps_req\"}");
            }, "gap-fill").start();

        } catch (Exception e) {
            busy = false;
            Log.e(TAG, "onGaps", e);
        }
    }

    /** Ask the Pi to re-check coverage for the active route. */
    public static void request(Context ctx) {
        Link.send(ctx, "{\"type\":\"gaps_req\"}");
    }
}

package com.motonav;

/**
 * One place for "what is the app doing right now, and how far along".
 * Drives the progress bar in MainActivity.
 */
public class Progress {

    public enum Phase {
        IDLE("", 0),
        RESOLVING("Resolving link", 5),
        ROUTING("Planning route", 15),
        SLICING("Preparing offline map", 35),
        SENDING("Sending map to Pi", 55),
        DOWNLOADING("Downloading map data", 70),
        SAVING("Saving to phone", 90),
        DONE("Ready", 100),
        FAILED("Failed", 0);

        public final String label;
        public final int base;
        Phase(String l, int b) { label = l; base = b; }
    }

    public interface Listener {
        void onProgress(Phase phase, int percent, String detail);
    }

    private static volatile Listener listener;
    public static volatile Phase phase = Phase.IDLE;
    public static volatile int percent = 0;
    public static volatile String detail = "";

    public static void setListener(Listener l) { listener = l; }

    public static void set(Phase p, int pct, String det) {
        phase = p;
        percent = Math.max(0, Math.min(100, pct));
        detail = det == null ? "" : det;
        Listener l = listener;
        if (l != null) l.onProgress(phase, percent, detail);
    }

    public static void set(Phase p, String det) {
        set(p, p.base, det);
    }

    /** Progress within a phase: `frac` 0..1 mapped between this phase and the next. */
    public static void within(Phase p, double frac, String det) {
        Phase[] all = Phase.values();
        int next = 100;
        for (int i = 0; i < all.length - 1; i++) {
            if (all[i] == p && all[i + 1].base > p.base) { next = all[i + 1].base; break; }
        }
        int pct = (int) Math.round(p.base + (next - p.base) * Math.max(0, Math.min(1, frac)));
        set(p, pct, det);
    }

    public static void done(String det) { set(Phase.DONE, 100, det); }
    public static void fail(String det) { set(Phase.FAILED, 0, det); }
    public static void idle() { set(Phase.IDLE, 0, ""); }

    public static boolean isBusy() {
        return phase != Phase.IDLE && phase != Phase.DONE && phase != Phase.FAILED;
    }
}

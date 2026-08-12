package com.motonav;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * Owns both transports and routes messages to whichever is connected.
 *
 * Mode:
 *   "auto"  - keep both alive; prefer Wi-Fi, fall back to Bluetooth
 *   "wifi"  - Wi-Fi only
 *   "bt"    - Bluetooth only
 */
public final class Link {

    public static final String PREFS = "motonav";
    public static final String KEY_IP = "pi_ip";
    public static final String KEY_PORT = "pi_port";
    public static final String KEY_THEME = "theme_mode";
    public static final String KEY_MODE = "transport_mode";
    public static final String KEY_BTNAME = "pi_bt_name";

    public static final String DEFAULT_IP = "192.168.43.50";
    public static final int DEFAULT_PORT = 9999;
    public static final String DEFAULT_BTNAME = "raspberrypi";

    private static TcpClient tcp;
    private static BtClient bt;
    private static Context app;

    private Link() {}

    public static synchronized void init(Context ctx) {
        if (app != null) return;
        app = ctx.getApplicationContext();
        SharedPreferences p = prefs(app);
        tcp = new TcpClient(p.getString(KEY_IP, DEFAULT_IP),
                            p.getInt(KEY_PORT, DEFAULT_PORT));
        tcp.setContext(app);
        bt = new BtClient(p.getString(KEY_BTNAME, DEFAULT_BTNAME));
        applyMode(mode(app));
    }

    private static void applyMode(String m) {
        if (tcp == null || bt == null) return;
        if ("bt".equals(m)) {
            tcp.stop();
            bt.start();
        } else if ("wifi".equals(m)) {
            bt.stop();
            tcp.start();
        } else {
            tcp.start();
            bt.start();
        }
    }

    /** Send over the preferred connected transport. */
    public static void send(Context ctx, String json) {
        init(ctx);
        String m = mode(ctx);
        if ("bt".equals(m)) { bt.send(json); return; }
        if ("wifi".equals(m)) { tcp.send(json); return; }
        if (tcp.isConnected()) tcp.send(json);
        else if (bt.isConnected()) bt.send(json);
    }

    public static boolean isConnected(Context ctx) {
        init(ctx);
        String m = mode(ctx);
        if ("bt".equals(m)) return bt.isConnected();
        if ("wifi".equals(m)) return tcp.isConnected();
        return tcp.isConnected() || bt.isConnected();
    }

    /** "WI-FI", "BLUETOOTH" or null when nothing is connected. */
    public static String activeTransport(Context ctx) {
        init(ctx);
        if (tcp != null && tcp.isConnected()) return "WI-FI";
        if (bt != null && bt.isConnected()) return "BLUETOOTH";
        return null;
    }

    public static SharedPreferences prefs(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static String ip(Context ctx)    { return prefs(ctx).getString(KEY_IP, DEFAULT_IP); }
    public static int port(Context ctx)     { return prefs(ctx).getInt(KEY_PORT, DEFAULT_PORT); }
    public static String btName(Context c)  { return prefs(c).getString(KEY_BTNAME, DEFAULT_BTNAME); }
    public static String mode(Context ctx)  { return prefs(ctx).getString(KEY_MODE, "auto"); }
    public static String theme(Context ctx) { return prefs(ctx).getString(KEY_THEME, "auto"); }

    public static void setTarget(Context ctx, String ip, int port) {
        init(ctx);
        prefs(ctx).edit().putString(KEY_IP, ip).putInt(KEY_PORT, port).apply();
        tcp.setTarget(ip, port);
    }

    public static void setBtName(Context ctx, String name) {
        init(ctx);
        prefs(ctx).edit().putString(KEY_BTNAME, name).apply();
        bt.setTargetName(name);
    }

    public static void setMode(Context ctx, String m) {
        init(ctx);
        prefs(ctx).edit().putString(KEY_MODE, m).apply();
        applyMode(m);
    }

    public static void setTheme(Context ctx, String mode) {
        prefs(ctx).edit().putString(KEY_THEME, mode).apply();
        sendTheme(ctx, mode);
    }

    public static void sendTheme(Context ctx, String mode) {
        send(ctx, "{\"type\":\"theme\",\"mode\":\"" + mode + "\"}");
    }
}

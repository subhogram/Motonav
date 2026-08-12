package com.motonav;

import android.content.Context;
import android.net.wifi.WifiManager;
import android.util.Log;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;

/**
 * Finds the Pi on whatever network the phone is currently on —
 * phone hotspot, home Wi-Fi, office Wi-Fi, anything shared.
 *
 * Sends a UDP broadcast probe; the Pi replies with its IP and port.
 * Removes the need to ever type an IP address.
 */
public class Discovery {

    private static final String TAG = "MotoNavDisc";
    private static final int PORT = 9998;
    private static final String MAGIC = "MOTONAV_DISCOVER";

    public static class Found {
        public final String ip;
        public final int port;
        public final String name;
        Found(String ip, int port, String name) {
            this.ip = ip; this.port = port; this.name = name;
        }
    }

    /** Blocking probe. Returns the first Pi that answers, or null. */
    public static Found probe(Context ctx, int timeoutMs) {
        DatagramSocket sock = null;
        WifiManager.MulticastLock lock = null;
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                lock = wm.createMulticastLock("motonav-disc");
                lock.setReferenceCounted(true);
                lock.acquire();
            }

            sock = new DatagramSocket();
            sock.setBroadcast(true);
            sock.setSoTimeout(Math.max(400, timeoutMs));

            byte[] out = MAGIC.getBytes("UTF-8");
            for (InetAddress target : broadcastAddresses()) {
                try {
                    sock.send(new DatagramPacket(out, out.length, target, PORT));
                } catch (Exception e) {
                    Log.d(TAG, "send to " + target + ": " + e.getMessage());
                }
            }

            long deadline = System.currentTimeMillis() + timeoutMs;
            byte[] buf = new byte[512];
            while (System.currentTimeMillis() < deadline) {
                try {
                    DatagramPacket in = new DatagramPacket(buf, buf.length);
                    sock.receive(in);
                    String reply = new String(in.getData(), 0, in.getLength(), "UTF-8");
                    // MOTONAV|<ip>|<port>|<hostname>
                    if (reply.startsWith("MOTONAV|")) {
                        String[] p = reply.split("\\|");
                        String ip = (p.length > 1 && !p[1].isEmpty())
                            ? p[1] : in.getAddress().getHostAddress();
                        int port = p.length > 2 ? Integer.parseInt(p[2].trim()) : 9999;
                        String nm = p.length > 3 ? p[3] : "raspberrypi";
                        Log.i(TAG, "found Pi " + ip + ":" + port + " (" + nm + ")");
                        return new Found(ip, port, nm);
                    }
                } catch (java.net.SocketTimeoutException te) {
                    break;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "probe failed: " + e.getMessage());
        } finally {
            if (sock != null) sock.close();
            if (lock != null) {
                try { lock.release(); } catch (Exception ignored) {}
            }
        }
        return null;
    }

    /** Broadcast addresses for every active interface, plus the global one. */
    private static List<InetAddress> broadcastAddresses() {
        List<InetAddress> out = new ArrayList<>();
        try {
            Enumeration<java.net.NetworkInterface> ifs =
                java.net.NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                java.net.NetworkInterface ni = ifs.nextElement();
                if (ni.isLoopback() || !ni.isUp()) continue;
                for (java.net.InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress b = ia.getBroadcast();
                    if (b != null) out.add(b);
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "iface scan: " + e.getMessage());
        }
        try {
            out.add(InetAddress.getByName("255.255.255.255"));
        } catch (Exception ignored) {}
        // common hotspot / router ranges as a last resort
        for (String s : new String[]{"192.168.43.255", "192.168.1.255",
                                     "192.168.0.255", "172.20.10.15"}) {
            try { out.add(InetAddress.getByName(s)); } catch (Exception ignored) {}
        }
        return out;
    }

    /** Fire-and-forget async probe that updates Link when it finds something. */
    public static void autoFind(final Context ctx, final Runnable onFound) {
        new Thread(() -> {
            Found f = probe(ctx, 2500);
            if (f != null) {
                Link.setTarget(ctx, f.ip, f.port);
                Link.prefs(ctx).edit().putBoolean("auto_found", true).apply();
                if (onFound != null) onFound.run();
            }
        }, "disc-probe").start();
    }
}

package com.motonav;

import android.util.Log;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Maintains a TCP connection to the Pi and sends newline-delimited JSON.
 * Auto-reconnects. Thread-safe.
 */
public class TcpClient {

    private static final String TAG = "MotoNavNet";

    private volatile String host;
    private volatile int port;

    private Socket socket;
    private OutputStream out;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicBoolean connected = new AtomicBoolean(false);
    private Thread thread;
    private volatile long lastOkMs = 0;
    private volatile int failCount = 0;
    private volatile android.content.Context appCtx;

    public interface Listener { void onStateChanged(boolean connected); }
    private volatile Listener listener;

    public TcpClient(String host, int port) {
        this.host = host;
        this.port = port;
    }

    public void setListener(Listener l) { this.listener = l; }
    public void setContext(android.content.Context c) { this.appCtx = c.getApplicationContext(); }
    public boolean isConnected() { return connected.get(); }
    public long lastOk() { return lastOkMs; }

    public void setTarget(String host, int port) {
        boolean changed = !host.equals(this.host) || port != this.port;
        this.host = host;
        this.port = port;
        if (changed) closeSocket();      // force reconnect to the new target
    }

    public void start() {
        if (running.getAndSet(true)) return;
        thread = new Thread(this::loop, "tcp-conn");
        thread.setDaemon(true);
        thread.start();

        Thread hb = new Thread(() -> {
            while (running.get()) {
                try { Thread.sleep(5000); } catch (InterruptedException e) { break; }
                if (connected.get()) send("{\"type\":\"ping\"}");
            }
        }, "tcp-heartbeat");
        hb.setDaemon(true);
        hb.start();
    }

    public void stop() {
        running.set(false);
        closeSocket();
        if (thread != null) thread.interrupt();
    }

    public synchronized void send(String json) {
        if (!connected.get() || out == null) return;
        try {
            out.write((json + "\n").getBytes("UTF-8"));
            out.flush();
            lastOkMs = System.currentTimeMillis();
        } catch (Exception e) {
            Log.w(TAG, "send failed: " + e.getMessage());
            closeSocket();
        }
    }

    private void loop() {
        while (running.get()) {
            if (!connected.get()) tryConnect();
            try { Thread.sleep(2000); } catch (InterruptedException e) { break; }
        }
    }

    private void tryConnect() {
        String h = host;
        if (h == null || h.trim().isEmpty()) return;
        Socket s = null;
        try {
            Log.i(TAG, "connecting " + h + ":" + port);
            s = new Socket();
            s.connect(new InetSocketAddress(h.trim(), port), 4000);
            s.setTcpNoDelay(true);
            s.setKeepAlive(true);
            socket = s;
            out = s.getOutputStream();
            connected.set(true);
            startReader(s);
            failCount = 0;
            lastOkMs = System.currentTimeMillis();
            Log.i(TAG, "connected");
            notifyState();
        } catch (Exception e) {
            Log.w(TAG, "connect failed: " + e.getMessage());
            try { if (s != null) s.close(); } catch (Exception ignored) {}
            if (connected.getAndSet(false)) notifyState();
            // after a couple of failures, go looking for the Pi ourselves
            failCount++;
            if (failCount % 3 == 0 && appCtx != null) {
                Discovery.Found f = Discovery.probe(appCtx, 2000);
                if (f != null && !f.ip.equals(host)) {
                    Log.i(TAG, "discovery moved target to " + f.ip + ":" + f.port);
                    Link.setTarget(appCtx, f.ip, f.port);
                }
            }
        }
    }

    /** Read JSON lines the Pi sends back (metrics, acks). */
    private void startReader(final Socket sock) {
        Thread t = new Thread(() -> {
            try {
                java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(sock.getInputStream(), "UTF-8"));
                String line;
                while ((line = r.readLine()) != null) {
                    if (line.trim().isEmpty()) continue;
                    PiMetrics.onMessage(line.trim());
                }
            } catch (Exception e) {
                Log.d(TAG, "reader ended: " + e.getMessage());
            }
        }, "tcp-reader");
        t.setDaemon(true);
        t.start();
    }

    private void closeSocket() {
        boolean was = connected.getAndSet(false);
        try { if (out != null) out.close(); } catch (Exception ignored) {}
        try { if (socket != null) socket.close(); } catch (Exception ignored) {}
        out = null; socket = null;
        if (was) notifyState();
    }

    private void notifyState() {
        Listener l = listener;
        if (l != null) {
            try { l.onStateChanged(connected.get()); } catch (Exception ignored) {}
        }
    }
}

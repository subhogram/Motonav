package com.motonav;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothSocket;
import android.util.Log;

import java.io.OutputStream;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Bluetooth serial (RFCOMM) link to the Pi. Same send()/start()/stop()
 * contract as TcpClient so Link can swap between them.
 */
public class BtClient {

    private static final String TAG = "MotoNavBT";
    private static final UUID SPP =
        UUID.fromString("00001101-0000-1000-8000-00805f9b34fb");
    private static final int CHANNEL = 22;

    private volatile String targetName;
    private BluetoothSocket socket;
    private OutputStream out;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicBoolean connected = new AtomicBoolean(false);
    private Thread thread;

    public BtClient(String targetName) {
        this.targetName = targetName;
    }

    public void setTargetName(String n) { this.targetName = n; }
    public boolean isConnected() { return connected.get(); }

    public void start() {
        if (running.getAndSet(true)) return;
        thread = new Thread(this::loop, "bt-conn");
        thread.setDaemon(true);
        thread.start();
    }

    public void stop() {
        running.set(false);
        close();
        if (thread != null) thread.interrupt();
    }

    public synchronized void send(String json) {
        if (!connected.get() || out == null) return;
        try {
            out.write((json + "\n").getBytes("UTF-8"));
            out.flush();
        } catch (Exception e) {
            Log.w(TAG, "send failed: " + e.getMessage());
            close();
        }
    }

    private void loop() {
        while (running.get()) {
            if (!connected.get()) tryConnect();
            try { Thread.sleep(3000); } catch (InterruptedException e) { break; }
        }
    }

    private void tryConnect() {
        BluetoothAdapter ad = BluetoothAdapter.getDefaultAdapter();
        if (ad == null || !ad.isEnabled()) return;
        BluetoothDevice pi = findPi(ad);
        if (pi == null) {
            Log.w(TAG, "paired device '" + targetName + "' not found");
            return;
        }
        try {
            ad.cancelDiscovery();
            socket = pi.createRfcommSocketToServiceRecord(SPP);
            socket.connect();
            out = socket.getOutputStream();
            connected.set(true);
            Log.i(TAG, "connected to " + pi.getName());
            return;
        } catch (Exception e) {
            Log.w(TAG, "SPP connect failed: " + e.getMessage());
        }
        // fallback: direct channel via reflection (some devices need this)
        try {
            java.lang.reflect.Method m =
                pi.getClass().getMethod("createRfcommSocket", int.class);
            socket = (BluetoothSocket) m.invoke(pi, CHANNEL);
            socket.connect();
            out = socket.getOutputStream();
            connected.set(true);
            Log.i(TAG, "connected (fallback channel " + CHANNEL + ")");
        } catch (Exception e) {
            Log.w(TAG, "fallback failed: " + e.getMessage());
            close();
        }
    }

    private BluetoothDevice findPi(BluetoothAdapter ad) {
        try {
            Set<BluetoothDevice> paired = ad.getBondedDevices();
            if (paired == null) return null;
            for (BluetoothDevice d : paired) {
                String n = d.getName();
                if (n != null && n.equalsIgnoreCase(targetName)) return d;
            }
            for (BluetoothDevice d : paired) {
                String n = d.getName();
                if (n != null && n.toLowerCase().contains(targetName.toLowerCase())) return d;
            }
        } catch (SecurityException e) {
            Log.e(TAG, "BLUETOOTH_CONNECT permission missing", e);
        }
        return null;
    }

    private void close() {
        connected.set(false);
        try { if (out != null) out.close(); } catch (Exception ignored) {}
        try { if (socket != null) socket.close(); } catch (Exception ignored) {}
        out = null; socket = null;
    }
}

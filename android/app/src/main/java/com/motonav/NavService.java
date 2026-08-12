package com.motonav;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

/**
 * Runs while a route is active: streams GPS to the Pi, keeps the Pi's
 * clock in sync, and re-routes if the rider leaves the line.
 *
 * Replaces the old Accessibility service — MotoNav no longer reads
 * Google Maps at all.
 */
public class NavService extends Service {

    private static final String TAG = "MotoNavSvc";
    private static final String CH_ID = "motonav_nav";
    private static final int NOTIF_ID = 42;

    public static final String ACTION_START = "com.motonav.START";
    public static final String ACTION_STOP = "com.motonav.STOP";

    private static volatile boolean running = false;
    public static boolean isRunning() { return running; }

    private LocationStreamer gps;
    private Handler handler;
    private final Runnable timeSync = new Runnable() {
        @Override public void run() {
            Router.sendTime(NavService.this);
            handler.postDelayed(this, 60000);       // keep the Pi clock honest
        }
    };

    public static void start(Context c) {
        Intent i = new Intent(c, NavService.class).setAction(ACTION_START);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    public static void stop(Context c) {
        c.startService(new Intent(c, NavService.class).setAction(ACTION_STOP));
    }

    @Override public IBinder onBind(Intent i) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();

        if (ACTION_STOP.equals(action)) {
            stopSelf();
            return START_NOT_STICKY;
        }

        if (!running) {
            startForeground(NOTIF_ID, buildNotification("Navigating"));
            Link.init(this);
            gps = new LocationStreamer(this);
            gps.start();
            handler = new Handler(Looper.getMainLooper());
            handler.post(timeSync);
            running = true;
            Log.i(TAG, "nav service started");
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (handler != null) handler.removeCallbacksAndMessages(null);
        if (gps != null) gps.stop();
        Log.i(TAG, "nav service stopped");
        super.onDestroy();
    }

    /** Called by LocationStreamer on each fix. */
    public void onPosition(double lat, double lon) {
        if (Router.hasRoute()) Router.checkOffRoute(this, lat, lon);
    }

    private Notification buildNotification(String text) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null && nm.getNotificationChannel(CH_ID) == null) {
                NotificationChannel ch = new NotificationChannel(
                    CH_ID, "MotoNav", NotificationManager.IMPORTANCE_LOW);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0,
            new Intent(this, MainActivity.class),
            Build.VERSION.SDK_INT >= 23
                ? PendingIntent.FLAG_IMMUTABLE : 0);

        Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
            ? new Notification.Builder(this, CH_ID)
            : new Notification.Builder(this);

        String dest = Router.activeDestination();
        return b.setContentTitle("MotoNav")
                .setContentText(dest.isEmpty() ? text : ("→ " + dest))
                .setSmallIcon(android.R.drawable.ic_menu_directions)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }
}

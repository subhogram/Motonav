package com.motonav;

import android.annotation.SuppressLint;
import android.content.Context;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.util.Log;

import org.json.JSONObject;

/**
 * Streams GPS position, heading and speed to the Pi (type = "pos").
 * Uses the platform LocationManager — no Play Services dependency.
 */
public class LocationStreamer implements LocationListener {

    private static final String TAG = "MotoNavLoc";
    private static final long MIN_TIME_MS = 1000;
    private static final float MIN_DIST_M = 0f;

    private final Context ctx;
    private LocationManager lm;
    private boolean running = false;

    private NavService owner;

    public LocationStreamer(Context ctx) {
        this.ctx = ctx;
        if (ctx instanceof NavService) owner = (NavService) ctx;
    }

    @SuppressLint("MissingPermission")
    public void start() {
        if (running) return;
        lm = (LocationManager) ctx.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) { Log.w(TAG, "no LocationManager"); return; }
        try {
            if (lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.GPS_PROVIDER,
                    MIN_TIME_MS, MIN_DIST_M, this);
            }
            if (lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER,
                    MIN_TIME_MS, MIN_DIST_M, this);
            }
            running = true;
            Log.i(TAG, "location updates started");
        } catch (SecurityException e) {
            Log.e(TAG, "location permission missing", e);
        }
    }

    public void stop() {
        if (lm != null) {
            try { lm.removeUpdates(this); } catch (Exception ignored) {}
        }
        running = false;
    }

    @Override
    public void onLocationChanged(Location loc) {
        try {
            JSONObject j = new JSONObject();
            j.put("type", "pos");
            j.put("lat", loc.getLatitude());
            j.put("lon", loc.getLongitude());
            j.put("bearing", loc.hasBearing() ? loc.getBearing() : JSONObject.NULL);
            j.put("speed_kph", loc.hasSpeed() ? loc.getSpeed() * 3.6 : JSONObject.NULL);
            Link.send(ctx, j.toString());
            if (owner != null) owner.onPosition(loc.getLatitude(), loc.getLongitude());
        } catch (Exception e) {
            Log.e(TAG, "pos json", e);
        }
    }

    @Override public void onProviderEnabled(String p) {}
    @Override public void onProviderDisabled(String p) {}
    @Override public void onStatusChanged(String p, int s, Bundle b) {}
}

package com.motonav;

import android.Manifest;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

public class MainActivity extends AppCompatActivity {

    private static final int REQ_PERMS = 100;

    private TextView tvLinkDot, tvLinkText, tvHint, tvRoute, tvPi, tvCache;
    private EditText etDest, etIp, etPort, etBtName;
    private Button mAuto, mWifi, mBt, bRegionDetail;
    private EditText etRegion;
    private android.view.View progressBlock;
    private android.widget.ProgressBar progBar;
    private TextView progPhase, progPct, progDetail;
    private RegionDownloader.Detail regionDetail = RegionDownloader.Detail.FULL;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable ticker = new Runnable() {
        @Override public void run() { refresh(); ui.postDelayed(this, 1500); }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);

        tvLinkDot  = findViewById(R.id.linkDot);
        tvLinkText = findViewById(R.id.linkText);
        tvHint     = findViewById(R.id.hint);
        tvRoute    = findViewById(R.id.routeStatus);
        tvPi       = findViewById(R.id.piMetrics);
        tvCache    = findViewById(R.id.cacheStatus);
        etDest     = findViewById(R.id.destInput);
        etIp       = findViewById(R.id.piIp);
        etPort     = findViewById(R.id.piPort);
        etBtName   = findViewById(R.id.piBtName);
        progressBlock = findViewById(R.id.progressBlock);
        progBar       = findViewById(R.id.progBar);
        progPhase     = findViewById(R.id.progPhase);
        progPct       = findViewById(R.id.progPct);
        progDetail    = findViewById(R.id.progDetail);
        etRegion      = findViewById(R.id.regionName);
        bRegionDetail = findViewById(R.id.btnRegionDetail);
        mAuto      = findViewById(R.id.modeAuto);
        mWifi      = findViewById(R.id.modeWifi);
        mBt        = findViewById(R.id.modeBt);

        etIp.setText(Link.ip(this));
        etPort.setText(String.valueOf(Link.port(this)));
        etBtName.setText(Link.btName(this));

        findViewById(R.id.btnGo).setOnClickListener(v -> startNavigation());
        findViewById(R.id.btnPaste).setOnClickListener(v -> pasteClipboard());
        findViewById(R.id.btnEndNav).setOnClickListener(v -> endNavigation());

        findViewById(R.id.btnMetrics).setOnClickListener(v -> {
            if (!Link.isConnected(this)) {
                Toast.makeText(this, "Not connected to the Pi", Toast.LENGTH_SHORT).show();
                return;
            }
            tvPi.setText("Reading…");
            PiMetrics.request(this);
        });
        findViewById(R.id.btnClearPiMap).setOnClickListener(v -> {
            PiMetrics.clearMap(this);
            Toast.makeText(this, "Cleared the Pi's map", Toast.LENGTH_SHORT).show();
            ui.postDelayed(() -> PiMetrics.request(this), 800);
        });
        findViewById(R.id.btnClearCache).setOnClickListener(v -> {
            long freed = MapCache.clearAll(this) + TileCache.clearAll(this);
            updateCache();
            Toast.makeText(this, "Freed " + PiMetrics.human(freed), Toast.LENGTH_SHORT).show();
        });

        findViewById(R.id.btnSave).setOnClickListener(v -> {
            int port = Link.DEFAULT_PORT;
            try { port = Integer.parseInt(etPort.getText().toString().trim()); }
            catch (Exception ignored) {}
            Link.setTarget(this, etIp.getText().toString().trim(), port);
            Link.setBtName(this, etBtName.getText().toString().trim());
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show();
        });

        findViewById(R.id.btnFindPi).setOnClickListener(v -> {
            Toast.makeText(this, "Searching…", Toast.LENGTH_SHORT).show();
            new Thread(() -> {
                Discovery.Found f = Discovery.probe(this, 3000);
                ui.post(() -> {
                    if (f != null) {
                        Link.setTarget(this, f.ip, f.port);
                        etIp.setText(f.ip);
                        etPort.setText(String.valueOf(f.port));
                        Toast.makeText(this, "Found " + f.name + " at " + f.ip,
                            Toast.LENGTH_LONG).show();
                    } else {
                        Toast.makeText(this, "No Pi found on this network",
                            Toast.LENGTH_LONG).show();
                    }
                });
            }, "find-pi").start();
        });

        bRegionDetail.setOnClickListener(v -> {
            RegionDownloader.Detail[] all = RegionDownloader.Detail.values();
            regionDetail = all[(regionDetail.ordinal() + 1) % all.length];
            bRegionDetail.setText(regionDetail == RegionDownloader.Detail.FULL ? "FULL"
                : regionDetail == RegionDownloader.Detail.NORMAL ? "MAIN" : "MAJOR");
        });

        findViewById(R.id.btnSaveRegion).setOnClickListener(v -> saveRegion());

        mAuto.setOnClickListener(v -> pickMode("auto"));
        mWifi.setOnClickListener(v -> pickMode("wifi"));
        mBt.setOnClickListener(v -> pickMode("bt"));
        applyModeButtons(Link.mode(this));

        Link.init(this);
        Discovery.autoFind(this, null);
        Progress.setListener((ph, pct, det) -> ui.post(() -> showProgress(ph, pct, det)));
        PiMetrics.attach(this);
        PiMetrics.setListener(() -> ui.post(this::showMetrics));
        GapFiller.setListener(m -> ui.post(() -> tvRoute.setText(m)));
        requestPerms();
        updateCache();

        handleShare(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleShare(intent);
    }

    /** A Maps link shared into MotoNav lands here. */
    private void handleShare(Intent intent) {
        if (intent == null) return;
        String text = null;
        if (Intent.ACTION_SEND.equals(intent.getAction())) {
            text = intent.getStringExtra(Intent.EXTRA_TEXT);
        } else if (Intent.ACTION_VIEW.equals(intent.getAction())
                && intent.getData() != null) {
            text = intent.getData().toString();
        }
        if (text == null || text.trim().isEmpty()) return;
        etDest.setText(text.trim());
        tvRoute.setText("Link received — starting…");
        ui.postDelayed(this::startNavigation, 400);
    }

    private void pasteClipboard() {
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (cm == null || !cm.hasPrimaryClip()) return;
        ClipData cd = cm.getPrimaryClip();
        if (cd == null || cd.getItemCount() == 0) return;
        CharSequence t = cd.getItemAt(0).coerceToText(this);
        if (t != null) etDest.setText(t.toString().trim());
    }

    @android.annotation.SuppressLint("MissingPermission")
    private void startNavigation() {
        String dest = etDest.getText().toString().trim();
        if (dest.isEmpty()) {
            Toast.makeText(this, "Paste a Maps link first", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!Link.isConnected(this)) {
            Toast.makeText(this, "Connect to the Pi first", Toast.LENGTH_SHORT).show();
            return;
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, "Location permission needed", Toast.LENGTH_SHORT).show();
            requestPerms();
            return;
        }

        android.location.LocationManager lm =
            (android.location.LocationManager) getSystemService(LOCATION_SERVICE);
        android.location.Location loc = null;
        try {
            loc = lm.getLastKnownLocation(android.location.LocationManager.GPS_PROVIDER);
            if (loc == null) loc = lm.getLastKnownLocation(
                android.location.LocationManager.NETWORK_PROVIDER);
        } catch (Exception ignored) {}
        if (loc == null) {
            Toast.makeText(this, "Waiting for a GPS fix…", Toast.LENGTH_SHORT).show();
            return;
        }

        NavService.start(this);
        tvRoute.setText("Starting…");
        Router.navigateTo(this, dest, loc.getLatitude(), loc.getLongitude(),
            (ok, msg) -> ui.post(() -> {
                tvRoute.setText(msg);
                if (!ok) Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
                updateCache();
            }));
    }

    private void endNavigation() {
        PiMetrics.endNavigation(this);
        NavService.stop(this);
        tvRoute.setText("Navigation ended — the Pi is idle.");
        Toast.makeText(this, "Navigation ended", Toast.LENGTH_SHORT).show();
    }

    private void pickMode(String m) {
        Link.setMode(this, m);
        applyModeButtons(m);
    }

    private void applyModeButtons(String m) {
        setBtn(mAuto, "auto".equals(m));
        setBtn(mWifi, "wifi".equals(m));
        setBtn(mBt, "bt".equals(m));
    }

    private void setBtn(Button b, boolean on) {
        b.setBackgroundResource(on ? R.drawable.btn_on : R.drawable.btn_off);
        b.setTextColor(on ? 0xFF0A0C10 : 0xFF8FA6BC);
    }

    private void requestPerms() {
        java.util.ArrayList<String> need = new java.util.ArrayList<>();
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.ACCESS_FINE_LOCATION);
        if (android.os.Build.VERSION.SDK_INT >= 31
                && ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.BLUETOOTH_CONNECT);
        if (android.os.Build.VERSION.SDK_INT >= 33
                && ContextCompat.checkSelfPermission(this, "android.permission.POST_NOTIFICATIONS")
                != PackageManager.PERMISSION_GRANTED)
            need.add("android.permission.POST_NOTIFICATIONS");
        if (!need.isEmpty())
            ActivityCompat.requestPermissions(this, need.toArray(new String[0]), REQ_PERMS);
    }

    @Override protected void onResume() {
        super.onResume();
        ui.post(ticker);
        updateCache();
    }

    @Override protected void onPause() {
        super.onPause();
        ui.removeCallbacks(ticker);
    }

    private void refresh() {
        boolean connected = Link.isConnected(this);
        String via = Link.activeTransport(this);
        tvLinkDot.setTextColor(connected ? 0xFF00C853 : 0xFFFF6D00);
        tvLinkText.setText(connected ? ("CONNECTED VIA " + via) : "NOT CONNECTED");
        tvLinkText.setTextColor(connected ? 0xFF00C853 : 0xFFFF6D00);

        boolean loc = ContextCompat.checkSelfPermission(this,
            Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        String hint;
        if (!loc)            hint = "Grant Location so the Pi can follow you.";
        else if (!connected) hint = "Turn on the hotspot or join the Pi's network, then FIND PI.";
        else if (NavService.isRunning() && Router.hasRoute())
                             hint = "Navigating to " + Router.activeDestination();
        else                 hint = "Ready. Share or paste a Maps link.";
        tvHint.setText(hint);
    }

    private void showProgress(Progress.Phase ph, int pct, String det) {
        boolean show = (ph != Progress.Phase.IDLE);
        progressBlock.setVisibility(show ? android.view.View.VISIBLE
                                         : android.view.View.GONE);
        if (!show) return;
        progPhase.setText(ph.label);
        progPct.setText(pct + "%");
        progBar.setProgress(pct);
        progDetail.setText(det);
        int col = (ph == Progress.Phase.FAILED) ? 0xFFC0392B
                : (ph == Progress.Phase.DONE)   ? 0xFF00C853 : 0xFF3BA9E0;
        progPhase.setTextColor(col);
        progPct.setTextColor(col);
        if (ph == Progress.Phase.DONE || ph == Progress.Phase.FAILED) {
            ui.postDelayed(() -> {
                if (!Progress.isBusy()) {
                    progressBlock.setVisibility(android.view.View.GONE);
                    Progress.idle();
                }
                updateCache();
            }, 4000);
        }
    }

    /** Download a named region around the current position and keep it on the phone. */
    @android.annotation.SuppressLint("MissingPermission")
    private void saveRegion() {
        String name = etRegion.getText().toString().trim();
        if (name.isEmpty()) name = "My area";
        if (Progress.isBusy()) {
            Toast.makeText(this, "Already working…", Toast.LENGTH_SHORT).show();
            return;
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            requestPerms();
            return;
        }
        android.location.LocationManager lm =
            (android.location.LocationManager) getSystemService(LOCATION_SERVICE);
        android.location.Location loc = null;
        try {
            loc = lm.getLastKnownLocation(android.location.LocationManager.GPS_PROVIDER);
            if (loc == null) loc = lm.getLastKnownLocation(
                android.location.LocationManager.NETWORK_PROVIDER);
        } catch (Exception ignored) {}
        if (loc == null) {
            Toast.makeText(this, "Waiting for a GPS fix…", Toast.LENGTH_SHORT).show();
            return;
        }

        // a sensible box around where you are; detail decides how big is wise
        double km = (regionDetail == RegionDownloader.Detail.FULL) ? 12
                  : (regionDetail == RegionDownloader.Detail.NORMAL) ? 35 : 90;
        double dLat = km / 111.32;
        double dLon = km / (111.32 * Math.cos(Math.toRadians(loc.getLatitude())));
        final String rn = name;
        Progress.set(Progress.Phase.DOWNLOADING, "starting " + rn);
        RegionDownloader.downloadRegion(this, rn,
            loc.getLatitude() - dLat, loc.getLongitude() - dLon,
            loc.getLatitude() + dLat, loc.getLongitude() + dLon,
            regionDetail,
            (ok, msg) -> ui.post(() -> {
                if (ok) Progress.done(msg); else Progress.fail(msg);
                tvRoute.setText(msg);
                updateCache();
                Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show();
            }));
    }

    private void showMetrics() {
        if (!PiMetrics.isFresh()) { tvPi.setText("No reply from the Pi."); return; }
        String s = "map on Pi   " + PiMetrics.human(PiMetrics.regionBytes)
                 + "  (" + PiMetrics.tileCount + " tiles, "
                 + PiMetrics.tileWays + " roads)\n"
                 + "route       " + PiMetrics.routePts + " pts, "
                 + PiMetrics.steps + " turns\n"
                 + (PiMetrics.dest.isEmpty() ? "" : ("to          " + PiMetrics.dest + "\n"))
                 + "disk free   " + PiMetrics.human(PiMetrics.diskFree) + "\n"
                 + "uptime      " + PiMetrics.uptime()
                 + "   cpu " + PiMetrics.cpuC + "C";
        tvPi.setText(s);
    }

    private void updateCache() {
        int tiles = TileCache.tileCount(this);
        long b = TileCache.totalBytes(this);
        java.util.List<TileCache.Region> regs = TileCache.regions(this);
        if (tiles == 0) {
            tvCache.setText("empty — download a region, or it fills as you ride");
            return;
        }
        StringBuilder sb = new StringBuilder();
        sb.append(tiles).append(" tiles, ").append(PiMetrics.human(b)).append("\n");
        int shown = 0;
        for (int i = regs.size() - 1; i >= 0 && shown < 3; i--, shown++) {
            TileCache.Region g = regs.get(i);
            sb.append("· ").append(g.name).append("  ")
              .append(g.ways).append(" roads\n");
        }
        sb.append("sent to the Pi automatically when you navigate");
        tvCache.setText(sb.toString());
    }
}

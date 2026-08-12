package com.motonav;

import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLDecoder;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Turns a shared Google Maps link into plain coordinates.
 *
 * Handles the shapes Google actually produces:
 *   https://maps.app.goo.gl/XXXX            (short — needs redirect)
 *   https://goo.gl/maps/XXXX                (short — needs redirect)
 *   https://www.google.com/maps/place/Name/@12.97,77.59,17z/...
 *   https://www.google.com/maps/search/?api=1&query=12.97,77.59
 *   https://maps.google.com/?q=12.97,77.59
 *   geo:12.97,77.59?q=...
 *
 * If the link carries only a place NAME, we geocode it with Nominatim.
 */
public class LinkResolver {

    private static final String TAG = "MotoNavLink";
    private static final String UA =
        "Mozilla/5.0 (Linux; Android 10) MotoNav/3.0";

    public static class Place {
        public double lat, lon;
        public String label = "";
        public boolean ok;
    }

    private static final Pattern P_AT =
        Pattern.compile("@(-?\\d+\\.\\d+),(-?\\d+\\.\\d+)");
    private static final Pattern P_3D4D =
        Pattern.compile("!3d(-?\\d+\\.\\d+)!4d(-?\\d+\\.\\d+)");
    private static final Pattern P_Q =
        Pattern.compile("[?&](?:q|query|daddr|destination)=(-?\\d+\\.\\d+)%?2?C?,?\\s*(-?\\d+\\.\\d+)");
    private static final Pattern P_GEO =
        Pattern.compile("geo:(-?\\d+\\.\\d+),(-?\\d+\\.\\d+)");
    private static final Pattern P_PLAIN =
        Pattern.compile("^\\s*(-?\\d{1,3}\\.\\d+)\\s*,\\s*(-?\\d{1,3}\\.\\d+)\\s*$");
    private static final Pattern P_URL =
        Pattern.compile("https?://\\S+");
    private static final Pattern P_PLACE_NAME =
        Pattern.compile("/maps/place/([^/@]+)");

    /** Pull the first URL out of shared text ("Check this out: https://…"). */
    public static String extractUrl(String shared) {
        if (shared == null) return null;
        Matcher m = P_URL.matcher(shared);
        return m.find() ? m.group() : null;
    }

    /** Resolve shared text (link, or "lat,lon", or a place name). */
    public static Place resolve(String shared) {
        Place p = new Place();
        if (shared == null || shared.trim().isEmpty()) return p;
        String text = shared.trim();

        // plain "12.97, 77.59"
        Matcher pm = P_PLAIN.matcher(text);
        if (pm.matches()) {
            p.lat = Double.parseDouble(pm.group(1));
            p.lon = Double.parseDouble(pm.group(2));
            p.label = String.format("%.5f, %.5f", p.lat, p.lon);
            p.ok = true;
            return p;
        }

        String url = extractUrl(text);
        if (url == null) {
            // not a link — treat the whole thing as a place name
            return geocodeName(text);
        }

        // expand short links
        String full = url;
        if (url.contains("goo.gl") || url.contains("app.goo.gl")
                || url.contains("maps.apple") || url.length() < 60) {
            String expanded = followRedirects(url);
            if (expanded != null) full = expanded;
        }
        Log.i(TAG, "resolved url: " + full);

        Place c = coordsFrom(full);
        if (c.ok) {
            c.label = placeNameFrom(full);
            if (c.label.isEmpty())
                c.label = String.format("%.5f, %.5f", c.lat, c.lon);
            return c;
        }

        // no coordinates in the URL — fall back to the place name
        String name = placeNameFrom(full);
        if (!name.isEmpty()) return geocodeName(name);
        return p;
    }

    /** Look for coordinates in any of the known URL shapes. */
    private static Place coordsFrom(String url) {
        Place p = new Place();
        String dec = url;
        try { dec = URLDecoder.decode(url, "UTF-8"); } catch (Exception ignored) {}

        for (Pattern pat : new Pattern[]{P_3D4D, P_AT, P_Q, P_GEO}) {
            Matcher m = pat.matcher(dec);
            if (m.find()) {
                try {
                    p.lat = Double.parseDouble(m.group(1));
                    p.lon = Double.parseDouble(m.group(2));
                    if (Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180) {
                        p.ok = true;
                        return p;
                    }
                } catch (Exception ignored) {}
            }
        }
        return p;
    }

    private static String placeNameFrom(String url) {
        try {
            Matcher m = P_PLACE_NAME.matcher(url);
            if (m.find()) {
                String n = URLDecoder.decode(m.group(1), "UTF-8");
                return n.replace('+', ' ').trim();
            }
        } catch (Exception ignored) {}
        return "";
    }

    /** Follow HTTP redirects to expand a short link. */
    private static String followRedirects(String url) {
        String cur = url;
        try {
            for (int hop = 0; hop < 5; hop++) {
                HttpURLConnection c = (HttpURLConnection) new URL(cur).openConnection();
                c.setInstanceFollowRedirects(false);
                c.setRequestProperty("User-Agent", UA);
                c.setConnectTimeout(12000);
                c.setReadTimeout(15000);
                int code = c.getResponseCode();
                String loc = c.getHeaderField("Location");
                if (code >= 300 && code < 400 && loc != null) {
                    cur = loc.startsWith("http") ? loc : new URL(new URL(cur), loc).toString();
                    c.disconnect();
                    continue;
                }
                // some links only reveal coords in the page body
                if (code == 200) {
                    InputStream in = c.getInputStream();
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    byte[] b = new byte[8192];
                    int n, total = 0;
                    while ((n = in.read(b)) > 0 && total < 400000) {
                        bo.write(b, 0, n); total += n;
                    }
                    in.close();
                    String body = bo.toString("UTF-8");
                    Matcher m = P_3D4D.matcher(body);
                    if (m.find()) return cur + "!3d" + m.group(1) + "!4d" + m.group(2);
                    m = P_AT.matcher(body);
                    if (m.find()) return cur + "@" + m.group(1) + "," + m.group(2);
                }
                c.disconnect();
                return cur;
            }
        } catch (Exception e) {
            Log.w(TAG, "redirect: " + e.getMessage());
        }
        return cur;
    }

    /** Nominatim lookup for a place name. */
    private static Place geocodeName(String name) {
        Place p = new Place();
        try {
            String url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
                       + java.net.URLEncoder.encode(name, "UTF-8");
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestProperty("User-Agent", "MotoNav/3.0 (personal device)");
            c.setConnectTimeout(12000);
            c.setReadTimeout(20000);
            if (c.getResponseCode() != 200) return p;
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] b = new byte[8192];
            int n;
            while ((n = in.read(b)) > 0) bo.write(b, 0, n);
            in.close();
            org.json.JSONArray arr = new org.json.JSONArray(bo.toString("UTF-8"));
            if (arr.length() == 0) return p;
            org.json.JSONObject o = arr.getJSONObject(0);
            p.lat = o.getDouble("lat");
            p.lon = o.getDouble("lon");
            p.label = o.optString("display_name", name);
            if (p.label.length() > 60) p.label = p.label.substring(0, 60) + "…";
            p.ok = true;
        } catch (Exception e) {
            Log.w(TAG, "geocode: " + e.getMessage());
        }
        return p;
    }
}

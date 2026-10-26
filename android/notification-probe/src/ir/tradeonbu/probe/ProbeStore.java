package ir.tradeonbu.probe;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

final class ProbeStore {
    static SharedPreferences prefs(Context c) { return c.getSharedPreferences("probe", Context.MODE_PRIVATE); }
    static String channel(Context c) { return prefs(c).getString("channel", "BehDark (VIP)"); }
    static synchronized void count(Context c, String key) {
        SharedPreferences p = prefs(c);
        p.edit().putInt(key, p.getInt(key, 0) + 1).apply();
    }
    static String hash(String s) throws Exception {
        byte[] bytes = MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        StringBuilder out = new StringBuilder();
        for (byte b : bytes) out.append(String.format("%02x", b & 255));
        return out.toString();
    }
    static synchronized void append(Context c, JSONObject event, String fingerprint) throws Exception {
        SharedPreferences p = prefs(c);
        JSONArray old = new JSONArray(p.getString("events", "[]"));
        for (int i = 0; i < old.length(); i++) {
            if (fingerprint.equals(old.getJSONObject(i).optString("fingerprint"))) {
                count(c, "duplicates");
                return;
            }
        }
        event.put("fingerprint", fingerprint);
        JSONArray next = new JSONArray();
        next.put(event);
        for (int i = 0; i < Math.min(old.length(), 9); i++) next.put(old.get(i));
        p.edit().putString("events", next.toString()).apply();
    }
    static synchronized void clear(Context c) {
        prefs(c).edit().remove("events").remove("telegram_seen").remove("target_seen")
                .remove("summaries").remove("duplicates").remove("errors").remove("last_received").apply();
    }
}

package ir.tradeonbu.probe;

import android.app.Notification;
import android.os.Bundle;
import android.os.Parcelable;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import org.json.JSONArray;
import org.json.JSONObject;

public final class ChannelListener extends NotificationListenerService {
    @Override public void onListenerConnected() {
        ProbeStore.prefs(this).edit().putBoolean("connected", true).apply();
        // Deliberately do not read active/historical notifications on connection.
    }
    @Override public void onListenerDisconnected() {
        ProbeStore.prefs(this).edit().putBoolean("connected", false).apply();
    }
    static String text(Bundle b, String key) {
        CharSequence value = b.getCharSequence(key);
        return value == null ? "" : value.toString();
    }
    static String clip(String value) { return value.length() > 2000 ? value.substring(0, 2000) : value; }

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || !ProbeStore.prefs(this).getBoolean("enabled", false)) return;
        if (!ChannelFilter.allowedPackage(sbn.getPackageName())) return;
        ProbeStore.count(this, "telegram_seen");
        try {
            Notification n = sbn.getNotification();
            Bundle b = n.extras;
            if (b == null) return;
            String title = text(b, Notification.EXTRA_TITLE);
            String bigTitle = text(b, Notification.EXTRA_TITLE_BIG);
            String conversation = text(b, Notification.EXTRA_CONVERSATION_TITLE);
            if (!ChannelFilter.matches(ProbeStore.channel(this), title, bigTitle, conversation)) return;
            ProbeStore.count(this, "target_seen");
            ProbeStore.prefs(this).edit().putLong("last_received", System.currentTimeMillis()).apply();
            // Aggregate notifications may combine chats. Do not retain their content.
            if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) {
                ProbeStore.count(this, "summaries");
                return;
            }
            String body = text(b, Notification.EXTRA_TEXT);
            String big = text(b, Notification.EXTRA_BIG_TEXT);
            JSONArray lines = new JSONArray();
            CharSequence[] allLines = b.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
            if (allLines != null) for (int i = 0; i < Math.min(8, allLines.length); i++)
                if (allLines[i] != null) lines.put(clip(allLines[i].toString()));
            JSONArray messages = new JSONArray();
            Parcelable[] raw = b.getParcelableArray(Notification.EXTRA_MESSAGES);
            if (raw != null) {
                java.util.List<Notification.MessagingStyle.Message> parsed = Notification.MessagingStyle.Message.getMessagesFromBundleArray(raw);
                for (int i = Math.max(0, parsed.size() - 8); i < parsed.size(); i++) {
                    Notification.MessagingStyle.Message m = parsed.get(i);
                    messages.put(new JSONObject().put("time_ms", m.getTimestamp())
                            .put("text", clip(m.getText() == null ? "" : m.getText().toString())));
                }
            }
            JSONObject content = new JSONObject().put("text", clip(body)).put("big_text", clip(big))
                    .put("lines", lines).put("messages", messages);
            String fingerprint = ProbeStore.hash(sbn.getKey() + "\n" + content.toString());
            JSONObject event = new JSONObject().put("captured_at_ms", System.currentTimeMillis())
                    .put("notification_post_time_ms", sbn.getPostTime()).put("app", sbn.getPackageName())
                    .put("channel", ProbeStore.channel(this)).put("title", clip(title))
                    .put("conversation_title", clip(conversation)).put("content", content)
                    .put("completeness", "unknown_compare_with_original")
                    .put("note", "Notification snapshot, not a Telegram message ID. Edits and deletions may be missed.");
            ProbeStore.append(this, event, fingerprint);
        } catch (Exception ignored) {
            // Never log notification text or another chat's metadata.
            ProbeStore.count(this, "errors");
        }
    }
}

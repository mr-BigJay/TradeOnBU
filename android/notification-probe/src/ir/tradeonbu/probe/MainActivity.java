package ir.tradeonbu.probe;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONArray;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private TextView status, output;
    private Button toggle;
    private EditText channel;
    private final Handler handler = new Handler();
    private String previousReport = "";
    private final Runnable refresh = new Runnable() {
        @Override public void run() { render(); handler.postDelayed(this, 1500); }
    };
    private int dp(int n) { return (int)(n * getResources().getDisplayMetrics().density); }
    private TextView label(LinearLayout box, String text, int size) {
        TextView view = new TextView(this);
        view.setText(text); view.setTextSize(size); view.setTextColor(Color.rgb(23, 38, 58));
        view.setPadding(0, dp(7), 0, dp(7)); box.addView(view); return view;
    }
    private Button button(LinearLayout box, String text, View.OnClickListener action) {
        Button b = new Button(this); b.setText(text); b.setAllCaps(false); b.setOnClickListener(action);
        box.addView(b, new LinearLayout.LayoutParams(-1, -2)); return b;
    }
    private boolean access() {
        String enabled = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        ComponentName mine = new ComponentName(this, ChannelListener.class);
        if (enabled == null) return false;
        for (String entry : enabled.split(":")) if (mine.equals(ComponentName.unflattenFromString(entry))) return true;
        return false;
    }
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        ScrollView scroll = new ScrollView(this);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL); box.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        box.setBackgroundColor(Color.rgb(245, 248, 252));
        box.setPadding(dp(18), dp(20), dp(18), dp(24));
        scroll.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom()); return insets;
        });
        scroll.addView(box); setContentView(scroll);
        label(box, "TradeOnBU · تست اعلان", 25);
        label(box, "نسخهٔ ۰٫۱ — فقط آزمایش روی گوشی", 15);
        label(box, "این برنامه اینترنت، ورود تلگرام و معامله ندارد. اندروید دسترسی عمومی اعلان‌ها می‌دهد، اما برنامه فقط اعلان‌های تلگرام با نام دقیق کانال انتخاب‌شده را نگه می‌دارد. متن مخفی یا محدودشده بازیابی نمی‌شود.", 15);
        label(box, "۱. نام دقیق کانال در اعلان", 18);
        channel = new EditText(this); channel.setSingleLine(true); channel.setText(ProbeStore.channel(this));
        channel.setTextDirection(View.TEXT_DIRECTION_LTR); box.addView(channel);
        label(box, "اگر تعداد اعلان تلگرام بالا رفت ولی کانال صفر ماند، نام باید با عنوان واقعی اعلان یکسان شود. نام حساب‌های دیگر ممکن است به عنوان اعلان اضافه شود.", 13);
        button(box, "۲. باز کردن مجوز دسترسی اعلان", v -> {
            try { startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)); }
            catch (Exception e) { Toast.makeText(this, "از تنظیمات گوشی، دسترسی اعلان را باز کن.", Toast.LENGTH_LONG).show(); }
        });
        toggle = button(box, "۳. شروع دریافت آزمایشی", v -> {
            SharedPreferences p = ProbeStore.prefs(this);
            if (p.getBoolean("enabled", false)) { p.edit().putBoolean("enabled", false).apply(); render(); return; }
            String name = channel.getText().toString().trim();
            if (name.isEmpty() || name.length() > 120) { channel.setError("نام دقیق کانال لازم است"); return; }
            if (!access()) { Toast.makeText(this, "ابتدا دسترسی اعلان را در تنظیمات فعال کن.", Toast.LENGTH_LONG).show(); return; }
            if (!name.equals(ProbeStore.channel(this))) ProbeStore.clear(this);
            p.edit().putString("channel", name).putBoolean("enabled", true).apply(); render();
        });
        status = label(box, "", 16);
        label(box, "اعلان‌های کانال و پیش‌نمایش متن را در تلگرام روشن کن. از صفحهٔ کانال خارج شو و منتظر یک پست جدید بمان. هنگام باز بودن کانال ممکن است اعلان تولید نشود. پست‌های قبلی خوانده نمی‌شوند.", 15);
        label(box, "برای Poco: در تنظیمات باتری برنامه، حالت بدون محدودیت را بررسی کن. این برنامه به SMS، مخاطبان یا Accessibility نیاز ندارد.", 13);
        button(box, "کپی گزارش برای بررسی", v -> {
            ((ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("TradeOnBU notification test", report()));
            Toast.makeText(this, "گزارش سه اعلان آخر کپی شد؛ قبل از ارسال متن را بررسی کن.", Toast.LENGTH_LONG).show();
        });
        button(box, "پاک کردن نمونه‌های ذخیره‌شده", v -> new AlertDialog.Builder(this)
                .setMessage("نمونه‌های محلی و شمارنده‌ها پاک شوند؟")
                .setPositiveButton("پاک کن", (d, w) -> { ProbeStore.clear(this); render(); })
                .setNegativeButton("انصراف", null).show());
        label(box, "متن دریافت‌شده — کامل‌بودن باید با پست اصلی مقایسه شود", 18);
        output = label(box, "", 13); output.setTextIsSelectable(true);
        output.setTextDirection(View.TEXT_DIRECTION_FIRST_STRONG); output.setLayoutDirection(View.LAYOUT_DIRECTION_LTR);
    }
    @Override public void onResume() { super.onResume(); handler.post(refresh); }
    @Override public void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
    private String report() {
        SharedPreferences p = ProbeStore.prefs(this);
        try {
            JSONArray events = new JSONArray(p.getString("events", "[]"));
            JSONArray latest = new JSONArray();
            for (int i = 0; i < Math.min(3, events.length()); i++) latest.put(events.get(i));
            return new JSONObject().put("app", "TradeOnBU Notification Probe 0.1")
                    .put("mode", "local_only_no_network_no_trading").put("permission_granted", access())
                    .put("enabled", p.getBoolean("enabled", false)).put("channel", ProbeStore.channel(this))
                    .put("telegram_notifications_seen", p.getInt("telegram_seen", 0))
                    .put("target_notifications_seen", p.getInt("target_seen", 0))
                    .put("aggregate_notifications_ignored", p.getInt("summaries", 0))
                    .put("duplicates_ignored", p.getInt("duplicates", 0)).put("errors", p.getInt("errors", 0))
                    .put("stored_snapshots", events.length()).put("latest_three_snapshots", latest).toString(2);
        } catch (Exception e) { return "خطا در خواندن گزارش محلی؛ نمونه‌ها را پاک کن."; }
    }
    private void render() {
        SharedPreferences p = ProbeStore.prefs(this);
        boolean enabled = p.getBoolean("enabled", false);
        channel.setEnabled(!enabled);
        toggle.setText(enabled ? "توقف دریافت آزمایشی" : "۳. شروع دریافت آزمایشی");
        status.setText("دسترسی اعلان: " + (access() ? "فعال" : "غیرفعال") + "\nدریافت: " + (enabled ? "روشن" : "خاموش")
                + "\nاعلان تلگرام: " + p.getInt("telegram_seen", 0) + " · کانال انتخاب‌شده: " + p.getInt("target_seen", 0));
        String text = report();
        if (!text.equals(previousReport)) { output.setText(text); previousReport = text; }
    }
}

import { useEffect, useState } from "react";
import { apiFetch, isAuthError } from "./api";

export function TelegramSettingsPage() {
  const [telegram, setTelegram] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setTelegram(await apiFetch("/api/gui/settings/telegram"));
    } catch (e) {
      if (!isAuthError(e)) setMsg(e instanceof Error ? e.message : "خطا");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load().catch(() => undefined);
  }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!telegram) return;
    await apiFetch("/api/gui/settings/telegram", { method: "PUT", body: JSON.stringify(telegram) });
    setMsg("ذخیره شد.");
    await load();
  }

  if (loading) return <p className="hint">بارگذاری…</p>;
  if (!telegram) return <p className="stale">تلگرام بارگذاری نشد.</p>;

  return (
    <div className="page">
      <h2>تلگرام</h2>
      <p className="hint">ارسال گزارش تحلیل AI به ربات شما (Paper only).</p>
      {msg && <p className="hint">{msg}</p>}
      <form className="card form-stack" onSubmit={save}>
        <label className="check-row">
          <input type="checkbox" checked={telegram.enabled} onChange={(e) => setTelegram({ ...telegram, enabled: e.target.checked })} />
          فعال
        </label>
        <label>توکن ربات</label>
        <input
          type="password"
          placeholder={telegram.bot_token_masked || "فقط در صورت تغییر"}
          onChange={(e) => setTelegram({ ...telegram, bot_token: e.target.value })}
        />
        <label>Chat ID</label>
        <input value={telegram.chat_id || ""} onChange={(e) => setTelegram({ ...telegram, chat_id: e.target.value })} />
        <label>کاربران مجاز (با کاما)</label>
        <input
          value={telegram.allowed_user_ids || ""}
          onChange={(e) => setTelegram({ ...telegram, allowed_user_ids: e.target.value })}
        />
        <div className="row-btns">
          <button type="submit">ذخیره</button>
          <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/settings/telegram/test", { method: "POST" }).then(() => setMsg("اتصال OK"))}>
            تست
          </button>
          <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/settings/telegram/send-test", { method: "POST" }).then(() => setMsg("پیام آزمایشی ارسال شد"))}>
            پیام آزمایشی
          </button>
        </div>
      </form>
    </div>
  );
}

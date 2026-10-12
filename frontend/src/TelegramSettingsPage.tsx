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

  if (loading) {
    return (
      <div className="page">
        <p className="hint center-hint">بارگذاری…</p>
      </div>
    );
  }

  if (!telegram) {
    return (
      <div className="page">
        <p className="toast toast-error">بارگذاری تلگرام ناموفق بود.</p>
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>تلگرام</h1>
        <p className="hint">دریافت گزارش تحلیل AI</p>
      </header>

      {msg && <p className="toast toast-ok">{msg}</p>}

      <form className="card form-stack" onSubmit={save}>
        <label className="toggle-row">
          <input type="checkbox" checked={telegram.enabled} onChange={(e) => setTelegram({ ...telegram, enabled: e.target.checked })} />
          <span>ربات فعال</span>
        </label>
        <label>توکن ربات</label>
        <input
          type="password"
          autoComplete="off"
          placeholder={telegram.bot_token_masked || "فقط در صورت تغییر"}
          onChange={(e) => setTelegram({ ...telegram, bot_token: e.target.value })}
        />
        <label>Chat ID</label>
        <input inputMode="numeric" value={telegram.chat_id || ""} onChange={(e) => setTelegram({ ...telegram, chat_id: e.target.value })} />
        <label>کاربران مجاز (با کاما)</label>
        <input value={telegram.allowed_user_ids || ""} onChange={(e) => setTelegram({ ...telegram, allowed_user_ids: e.target.value })} />
        <button type="submit" className="btn-primary btn-block">
          ذخیره
        </button>
        <div className="stack-btns">
          <button type="button" className="secondary btn-block" onClick={() => apiFetch("/api/gui/settings/telegram/test", { method: "POST" }).then(() => setMsg("اتصال OK"))}>
            تست اتصال
          </button>
          <button
            type="button"
            className="secondary btn-block"
            onClick={() => apiFetch("/api/gui/settings/telegram/send-test", { method: "POST" }).then(() => setMsg("پیام آزمایشی ارسال شد"))}
          >
            پیام آزمایشی
          </button>
        </div>
      </form>
    </div>
  );
}

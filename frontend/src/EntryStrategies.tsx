import { useEffect, useState } from "react";
import { apiFetch } from "./api";
import "./entryStrategies.css";

const labels = {
  ict_reversal: "A · ICT Reversal — برگشت قیمت",
  trend_continuation: "B · Trend Continuation — ادامهٔ روند",
  breakout_retest: "C · Breakout + Retest — شکست و بازآزمایی",
};
type Strategies = Record<keyof typeof labels, boolean>;

export function EntryStrategies() {
  const [values, setValues] = useState<Strategies | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    let active = true;
    apiFetch("/api/gui/chatgpt/strategies").then(v => { if (active) setValues(v); })
      .catch(() => { if (active) setMessage("دریافت تنظیمات استراتژی‌ها ناموفق بود؛ صفحه را تازه کنید."); });
    return () => { active = false; };
  }, []);
  async function save() {
    setBusy(true); setMessage("");
    try {
      const saved = await apiFetch("/api/gui/chatgpt/strategies/settings", {method: "PUT", body: JSON.stringify(values)});
      setValues(saved); setMessage("تنظیمات استراتژی‌ها ذخیره شد.");
    } catch (e) { setMessage(e instanceof Error ? e.message : "خطا در ذخیره تنظیمات"); }
    finally { setBusy(false); }
  }
  return <section className="card entry-strategies" dir="rtl" aria-label="استراتژی‌های تحلیل">
    <h3>استراتژی‌های تحلیل</h3>
    <p className="hint">یک درخواست مشترک؛ حداکثر یک ستاپ در هر تحلیل.</p>
    {values && (Object.keys(labels) as (keyof Strategies)[]).map(key => <label key={key} className="entry-strategies__option">
      <input type="checkbox" checked={values[key]} disabled={busy} onChange={e => setValues({...values, [key]: e.target.checked})} /> <span>{labels[key]}</span>
    </label>)}
    <button disabled={!values || busy || !Object.values(values).some(Boolean)} onClick={save}>ذخیره استراتژی‌ها</button>
    <details className="entry-strategies__help">
      <summary>راهنمای تنظیمات</summary>
      <p className="hint">فاصلهٔ تحلیل تغییر نمی‌کند. ذخیره نیاز به دسترسی مدیر دارد. غیرفعال‌سازی جلوی ستاپ و ورود جدید را می‌گیرد؛ سفارش در انتظار هنگام بررسی ورود لغو می‌شود. مدیریت پوزیشن باز ادامه دارد.</p>
    </details>
    {message && <p role="status">{message}</p>}
  </section>;
}

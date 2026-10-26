import { useEffect, useState } from "react";
import { apiFetch } from "./api";

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
  return <section className="card" dir="rtl" aria-label="استراتژی‌های تحلیل">
    <h3>استراتژی‌های تحلیل</h3>
    <p className="hint">همهٔ استراتژی‌های فعال در یک درخواست بررسی می‌شوند؛ هر روش می‌تواند یک فرصت مستقل را پایش کند؛ اجازهٔ ورود تابع محدودیت مشترک ریسک است. فاصلهٔ تحلیل تغییر نمی‌کند.</p>
    {values && (Object.keys(labels) as (keyof Strategies)[]).map(key => <label key={key} style={{display: "block", margin: "12px 0"}}>
      <input type="checkbox" checked={values[key]} disabled={busy} onChange={e => setValues({...values, [key]: e.target.checked})} /> {labels[key]}
    </label>)}
    <button disabled={!values || busy || !Object.values(values).some(Boolean)} onClick={save}>ذخیره استراتژی‌ها</button>
    <p className="hint">ذخیره نیاز به دسترسی مدیر دارد. غیرفعال‌سازی جلوی ستاپ و ورود جدید را می‌گیرد؛ سفارش در انتظار هنگام بررسی ورود لغو می‌شود. مدیریت پوزیشن باز ادامه دارد.</p>
    {message && <p role="status">{message}</p>}
  </section>;
}

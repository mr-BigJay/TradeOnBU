import { useEffect, useState } from "react";
import { apiFetch } from "./api";

type Tab = "telegram" | "strategy" | "risk" | "market" | "system" | "account";

export function SettingsPage() {
  const [tab, setTab] = useState<Tab>("telegram");
  const [telegram, setTelegram] = useState<any>(null);
  const [strategy, setStrategy] = useState<any>(null);
  const [risk, setRisk] = useState<any>(null);
  const [market, setMarket] = useState<any>(null);
  const [system, setSystem] = useState<any>(null);
  const [msg, setMsg] = useState("");

  async function loadAll() {
    const [t, s, r, m, sys] = await Promise.all([
      apiFetch("/api/gui/settings/telegram"),
      apiFetch("/api/gui/settings/strategy"),
      apiFetch("/api/gui/settings/risk"),
      apiFetch("/api/gui/settings/market"),
      apiFetch("/api/gui/settings/system"),
    ]);
    setTelegram(t);
    setStrategy(s);
    setRisk(r);
    setMarket(m);
    setSystem(sys);
  }

  useEffect(() => {
    loadAll().catch((e) => setMsg(String(e)));
  }, []);

  async function saveTelegram(e: React.FormEvent) {
    e.preventDefault();
    if (!telegram) return;
    await apiFetch("/api/gui/settings/telegram", { method: "PUT", body: JSON.stringify(telegram) });
    setMsg("تنظیمات تلگرام ذخیره شد");
    await loadAll();
  }

  async function saveStrategy(e: React.FormEvent) {
    e.preventDefault();
    if (!strategy) return;
    await apiFetch("/api/gui/settings/strategy", { method: "PUT", body: JSON.stringify(strategy) });
    setMsg("تنظیمات استراتژی ذخیره شد");
  }

  async function saveRisk(e: React.FormEvent) {
    e.preventDefault();
    if (!risk) return;
    await apiFetch("/api/gui/settings/risk", { method: "PUT", body: JSON.stringify(risk) });
    setMsg("تنظیمات ریسک ذخیره شد");
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "telegram", label: "تلگرام" },
    { id: "strategy", label: "استراتژی ICT" },
    { id: "risk", label: "مدیریت ریسک" },
    { id: "market", label: "داده بازار" },
    { id: "system", label: "دامنه و SSL" },
    { id: "account", label: "حساب مدیر" },
  ];

  return (
    <div className="settings-wrap">
      <h2>تنظیمات</h2>
      {msg && <p className="hint">{msg}</p>}
      <nav className="settings-tabs">
        {tabs.map((t) => (
          <button key={t.id} type="button" className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "telegram" && telegram && (
        <form className="card" onSubmit={saveTelegram}>
          <p>وضعیت: {telegram.status === "configured" ? "پیکربندی شده" : "پیکربندی نشده"}</p>
          <label>
            <input type="checkbox" checked={telegram.enabled} onChange={(e) => setTelegram({ ...telegram, enabled: e.target.checked })} /> فعال‌سازی تلگرام
          </label>
          <label>توکن ربات (فقط هنگام تغییر)</label>
          <input
            type="password"
            placeholder={telegram.bot_token_masked || "****"}
            onChange={(e) => setTelegram({ ...telegram, bot_token: e.target.value })}
          />
          <label>Chat ID</label>
          <input value={telegram.chat_id || ""} onChange={(e) => setTelegram({ ...telegram, chat_id: e.target.value })} />
          <label>شناسه کاربران مجاز (با کاما)</label>
          <input
            value={telegram.allowed_user_ids || ""}
            onChange={(e) => setTelegram({ ...telegram, allowed_user_ids: e.target.value })}
          />
          <label>
            <input
              type="checkbox"
              checked={telegram.require_approval}
              onChange={(e) => setTelegram({ ...telegram, require_approval: e.target.checked })}
            />{" "}
            نیاز به تأیید تلگرام
          </label>
          <div className="row-btns">
            <button type="submit">ذخیره</button>
            <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/settings/telegram/test", { method: "POST" }).then(() => setMsg("اتصال OK"))}>
              تست اتصال
            </button>
            <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/settings/telegram/send-test", { method: "POST" }).then(() => setMsg("پیام ارسال شد"))}>
              پیام آزمایشی
            </button>
          </div>
        </form>
      )}

      {tab === "strategy" && strategy && (
        <form className="card" onSubmit={saveStrategy}>
          <label>نماد</label>
          <input value={strategy.default_symbol} onChange={(e) => setStrategy({ ...strategy, default_symbol: e.target.value })} />
          <label>فاصله اسکن (ثانیه)</label>
          <input type="number" value={strategy.scan_interval_seconds} onChange={(e) => setStrategy({ ...strategy, scan_interval_seconds: Number(e.target.value) })} />
          <label>حداقل Net R:R</label>
          <input type="number" step="0.1" value={strategy.min_net_rr} onChange={(e) => setStrategy({ ...strategy, min_net_rr: Number(e.target.value) })} />
          <label>حداکثر انتظار (دقیقه)</label>
          <input type="number" value={strategy.max_pending_minutes} onChange={(e) => setStrategy({ ...strategy, max_pending_minutes: Number(e.target.value) })} />
          <label>فاصله از دست رفتن ورود (%)</label>
          <input type="number" step="0.01" value={strategy.max_missed_entry_pct} onChange={(e) => setStrategy({ ...strategy, max_missed_entry_pct: Number(e.target.value) })} />
          <label>حداکثر نگه‌داری paper (ساعت)</label>
          <input type="number" step="0.5" value={strategy.holding_time_hours} onChange={(e) => setStrategy({ ...strategy, holding_time_hours: Number(e.target.value) })} />
          <label>
            <input type="checkbox" checked={strategy.scanning_enabled} onChange={(e) => setStrategy({ ...strategy, scanning_enabled: e.target.checked })} /> اسکن خودکار ICT
          </label>
          <button type="submit">ذخیره</button>
        </form>
      )}

      {tab === "risk" && risk && (
        <form className="card" onSubmit={saveRisk}>
          <p className="hint">{risk.leverage_disclaimer}</p>
          <p>حالت: {risk.trading_mode}</p>
          <label>مارجین (USDT)</label>
          <input type="number" value={risk.margin_usdt} onChange={(e) => setRisk({ ...risk, margin_usdt: Number(e.target.value) })} />
          <label>اهرم شبیه‌سازی</label>
          <input type="number" value={risk.leverage} onChange={(e) => setRisk({ ...risk, leverage: Number(e.target.value) })} />
          <label>Maker Fee</label>
          <input type="number" step="0.0001" value={risk.maker_fee_rate} onChange={(e) => setRisk({ ...risk, maker_fee_rate: Number(e.target.value) })} />
          <label>Taker Fee</label>
          <input type="number" step="0.0001" value={risk.taker_fee_rate} onChange={(e) => setRisk({ ...risk, taker_fee_rate: Number(e.target.value) })} />
          <label>Spread (bps)</label>
          <input type="number" value={risk.spread_bps} onChange={(e) => setRisk({ ...risk, spread_bps: Number(e.target.value) })} />
          <label>Slippage (bps)</label>
          <input type="number" value={risk.slippage_bps} onChange={(e) => setRisk({ ...risk, slippage_bps: Number(e.target.value) })} />
          <label>
            <input type="checkbox" checked={risk.paper_trading_paused} onChange={(e) => setRisk({ ...risk, paper_trading_paused: e.target.checked })} /> توقف paper trading
          </label>
          <button type="submit">ذخیره</button>
        </form>
      )}

      {tab === "market" && market && (
        <div className="card">
          <p>صرافی: {market.exchange}</p>
          <p>نماد: {market.symbol}</p>
          <p>وضعیت API: {market.public_api_status}</p>
          <p>تازگی داده: {market.data_fresh ? "تازه" : "کهنه/خطا"}</p>
          <p>قیمت: {market.live_price ?? "—"}</p>
          {market.last_error && <p className="stale">{market.last_error}</p>}
        </div>
      )}

      {tab === "system" && system && (
        <div className="card">
          <p>دامنه: {system.domain || "—"}</p>
          <p>HTTPS: {system.https_expected ? "فعال (Caddy)" : "—"}</p>
          <p>حالت معاملاتی: Paper Only</p>
          <p className="hint">تغییر دامنه نیازمند اجرای مجدد نصب/اسکript به‌روزرسانی Caddy است.</p>
        </div>
      )}

      {tab === "account" && (
        <div className="card">
          <p>تغییر رمز از API /api/auth/password — فرم کامل در نسخه بعدی UI.</p>
        </div>
      )}
    </div>
  );
}

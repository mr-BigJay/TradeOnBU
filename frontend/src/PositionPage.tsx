import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import { mergeOpenFormDefaults, type OpenFormState } from "./positionFormMerge";

export type PositionPrefill = {
  direction: string;
  entry_price?: number;
  stop_loss: number;
  take_profit: number;
  ai_setup_id?: string;
  symbol?: string;
};

type Tab = "wallet" | "settings" | "open" | "report";

const TABS: { id: Tab; label: string }[] = [
  { id: "wallet", label: "کیف پول" },
  { id: "settings", label: "تنظیمات" },
  { id: "open", label: "پوزیشن باز" },
  { id: "report", label: "گزارش" },
];

type Props = {
  prefill: PositionPrefill | null;
  onConsumePrefill: () => void;
  initialTab?: Tab;
};

export function PositionPage({ prefill, onConsumePrefill, initialTab }: Props) {
  const [tab, setTab] = useState<Tab>(initialTab || "wallet");
  const [msg, setMsg] = useState("");
  const [wallet, setWallet] = useState<Record<string, unknown> | null>(null);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [positions, setPositions] = useState<unknown[]>([]);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [period, setPeriod] = useState("all");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");

  const [deposit, setDeposit] = useState("");
  const [withdraw, setWithdraw] = useState("");
  const [initialBal, setInitialBal] = useState("1000");

  const [form, setForm] = useState<OpenFormState>({
    direction: "LONG",
    margin_usdt: "50",
    leverage: "10",
    order_type: "market",
    entry_price: "",
    stop_loss: "",
    take_profit: "",
    ai_setup_id: "",
  });
  const preserveExecutionRef = useRef(false);

  useEffect(() => {
    if (prefill) {
      setTab("open");
      preserveExecutionRef.current = prefill.entry_price != null;
      setForm((f) => ({
        ...f,
        direction: prefill.direction,
        entry_price: prefill.entry_price != null ? String(prefill.entry_price) : f.entry_price,
        stop_loss: String(prefill.stop_loss),
        take_profit: String(prefill.take_profit),
        ai_setup_id: prefill.ai_setup_id || "",
        order_type: prefill.entry_price != null ? "limit" : "market",
      }));
      onConsumePrefill();
    }
  }, [prefill, onConsumePrefill]);

  const loadWallet = useCallback(async () => {
    setWallet(await apiFetch("/api/gui/position/wallet"));
  }, []);

  const loadSettings = useCallback(async () => {
    setSettings(await apiFetch("/api/gui/position/settings"));
  }, []);

  const loadPositions = useCallback(async () => {
    const d = await apiFetch("/api/gui/position/positions");
    setPositions(d.positions || []);
  }, []);

  const loadReport = useCallback(async () => {
    const qs = new URLSearchParams({ period });
    if (period === "custom") {
      if (customStart) qs.set("start_date", customStart);
      if (customEnd) qs.set("end_date", customEnd);
    }
    setReport(await apiFetch(`/api/gui/position/reports?${qs}`));
  }, [period, customStart, customEnd]);

  useEffect(() => {
    if (tab === "wallet") loadWallet().catch((e) => setMsg(String(e)));
    if (tab === "settings") loadSettings().catch((e) => setMsg(String(e)));
    if (tab === "open") {
      loadSettings().catch(() => {});
      loadPositions().catch((e) => setMsg(String(e)));
    }
    if (tab === "report") loadReport().catch((e) => setMsg(String(e)));
  }, [tab, loadWallet, loadSettings, loadPositions, loadReport]);

  useEffect(() => {
    if (settings && tab === "open") {
      setForm((f) => mergeOpenFormDefaults(f, settings, preserveExecutionRef.current));
    }
  }, [settings, tab]);

  const settingsForm = useMemo(() => settings || {}, [settings]);

  async function saveSettings() {
    setMsg("");
    await apiFetch("/api/gui/position/settings", {
      method: "PUT",
      body: JSON.stringify({
        default_margin_usdt: Number(settingsForm.default_margin_usdt),
        default_leverage: Number(settingsForm.default_leverage),
        default_order_type: settingsForm.default_order_type,
        slippage_bps: Number(settingsForm.slippage_bps),
        maker_fee_rate: Number(settingsForm.maker_fee_rate),
        taker_fee_rate: Number(settingsForm.taker_fee_rate),
        max_margin_per_position: Number(settingsForm.max_margin_per_position),
        max_risk_pct_per_position: Number(settingsForm.max_risk_pct_per_position),
      }),
    });
    setMsg("تنظیمات ذخیره شد.");
    await loadSettings();
  }

  async function submitOpen(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      await apiFetch("/api/gui/position/positions/open", {
        method: "POST",
        body: JSON.stringify({
          symbol: "BTCUSDT",
          direction: form.direction,
          margin_usdt: Number(form.margin_usdt),
          leverage: Number(form.leverage),
          order_type: form.order_type,
          entry_price: form.entry_price ? Number(form.entry_price) : null,
          stop_loss: Number(form.stop_loss),
          take_profit: Number(form.take_profit),
          ai_setup_id: form.ai_setup_id || null,
        }),
      });
      setMsg("سفارش ثبت شد.");
      await loadPositions();
      await loadWallet();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "خطا");
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>پوزیشن</h1>
        <p className="hint">کیف پول مجازی · شبیه‌ساز Bitunix · Paper فقط</p>
      </header>

      <nav className="sub-tabs" aria-label="زیرمنو پوزیشن">
        {TABS.map((t) => (
          <button key={t.id} type="button" className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>

      {msg && <p className={`toast ${msg.includes("خطا") ? "toast-error" : "toast-ok"}`}>{msg}</p>}

      {tab === "wallet" && wallet && (
        <section className="section">
          <div className="stat-grid">
            <div className="stat-card">
              <span className="stat-label">موجودی کل</span>
              <strong>{wallet.wallet_balance as number} USDT</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">موجودی آزاد</span>
              <strong>{wallet.available_balance as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">مارجین در استفاده</span>
              <strong>{wallet.margin_in_use as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">PnL تحقق‌نیافته</span>
              <strong>{wallet.unrealized_pnl as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">PnL تحقق‌یافته</span>
              <strong>{wallet.realized_pnl as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">کارمزد کل</span>
              <strong>{wallet.total_fees as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">اکوییتی</span>
              <strong>{wallet.account_equity as number}</strong>
            </div>
          </div>

          {!wallet.initial_balance_set && (
            <div className="card form-card">
              <h3>موجودی اولیه</h3>
              <input value={initialBal} onChange={(e) => setInitialBal(e.target.value)} inputMode="decimal" />
              <button
                type="button"
                className="btn-primary"
                onClick={() =>
                  apiFetch("/api/gui/position/wallet/initial", {
                    method: "POST",
                    body: JSON.stringify({ amount_usdt: Number(initialBal) }),
                  }).then(() => loadWallet())
                }
              >
                تنظیم
              </button>
            </div>
          )}

          <div className="card form-card">
            <h3>واریز / برداشت</h3>
            <label>واریز USDT</label>
            <input value={deposit} onChange={(e) => setDeposit(e.target.value)} inputMode="decimal" />
            <button
              type="button"
              className="secondary"
              onClick={() =>
                apiFetch("/api/gui/position/wallet/deposit", {
                  method: "POST",
                  body: JSON.stringify({ amount_usdt: Number(deposit) }),
                }).then(() => {
                  setDeposit("");
                  loadWallet();
                })
              }
            >
              واریز
            </button>
            <label>برداشت USDT</label>
            <input value={withdraw} onChange={(e) => setWithdraw(e.target.value)} inputMode="decimal" />
            <button
              type="button"
              className="secondary"
              onClick={() =>
                apiFetch("/api/gui/position/wallet/withdraw", {
                  method: "POST",
                  body: JSON.stringify({ amount_usdt: Number(withdraw) }),
                }).then(() => {
                  setWithdraw("");
                  loadWallet();
                })
              }
            >
              برداشت
            </button>
          </div>

          <h3 className="section-title">تاریخچه تراکنش</h3>
          <ul className="ledger-list">
            {((wallet.ledger as { entry_type: string; amount_usdt: number; created_at_tehran: string }[]) || []).map(
              (row, i) => (
                <li key={i} className="ledger-row">
                  <span>{row.entry_type}</span>
                  <span>{row.amount_usdt}</span>
                  <span>{row.created_at_tehran}</span>
                </li>
              ),
            )}
          </ul>
        </section>
      )}

      {tab === "settings" && settings && (
        <section className="section card form-card">
          <label>مارجین پیش‌فرض</label>
          <input
            value={String(settingsForm.default_margin_usdt ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, default_margin_usdt: e.target.value })}
          />
          <label>اهرم پیش‌فرض</label>
          <input
            value={String(settingsForm.default_leverage ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, default_leverage: e.target.value })}
          />
          <label>نوع سفارش</label>
          <select
            value={String(settingsForm.default_order_type ?? "market")}
            onChange={(e) => setSettings({ ...settingsForm, default_order_type: e.target.value })}
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
          </select>
          <label>Maker fee (VIP1)</label>
          <input
            value={String(settingsForm.maker_fee_rate ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, maker_fee_rate: e.target.value })}
          />
          <label>Taker fee (VIP1)</label>
          <input
            value={String(settingsForm.taker_fee_rate ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, taker_fee_rate: e.target.value })}
          />
          <label>Slippage (bps)</label>
          <input
            value={String(settingsForm.slippage_bps ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, slippage_bps: e.target.value })}
          />
          <label>حداکثر مارجین هر پوزیشن (USDT)</label>
          <input
            value={String(settingsForm.max_margin_per_position ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, max_margin_per_position: e.target.value })}
          />
          <label>حداکثر ریسک حساب هر پوزیشن (%)</label>
          <input
            value={String(settingsForm.max_risk_pct_per_position ?? "")}
            onChange={(e) => setSettings({ ...settingsForm, max_risk_pct_per_position: e.target.value })}
          />
          <p className="hint">حالت مارجین: {String(settingsForm.position_mode ?? "isolated")} (فعلاً Isolated)</p>
          <p className="hint">
            منبع کارمزد: {(settingsForm.fee_source_url as string) || "—"} · آخرین بررسی:{" "}
            {settingsForm.fee_verified_on as string}
          </p>
          <button type="button" className="btn-primary" onClick={saveSettings}>
            ذخیره
          </button>
        </section>
      )}

      {tab === "open" && (
        <section className="section">
          <form className="card form-card" onSubmit={submitOpen}>
            <h3>باز کردن پوزیشن BTCUSDT</h3>
            <label>جهت</label>
            <select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value })}>
              <option value="LONG">LONG</option>
              <option value="SHORT">SHORT</option>
            </select>
            <label>مارجین USDT</label>
            <input
              value={form.margin_usdt}
              onChange={(e) => setForm({ ...form, margin_usdt: e.target.value })}
              inputMode="decimal"
            />
            <label>اهرم</label>
            <input value={form.leverage} onChange={(e) => setForm({ ...form, leverage: e.target.value })} />
            <label>نوع ورود</label>
            <select value={form.order_type} onChange={(e) => setForm({ ...form, order_type: e.target.value })}>
              <option value="market">Market</option>
              <option value="limit">Limit</option>
            </select>
            {form.order_type === "limit" && (
              <>
                <label>قیمت ورود</label>
                <input
                  value={form.entry_price}
                  onChange={(e) => setForm({ ...form, entry_price: e.target.value })}
                  inputMode="decimal"
                />
              </>
            )}
            <label>Stop Loss</label>
            <input value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} />
            <label>Take Profit</label>
            <input value={form.take_profit} onChange={(e) => setForm({ ...form, take_profit: e.target.value })} />
            {form.ai_setup_id && <p className="hint">AI setup: {form.ai_setup_id}</p>}
            <button type="submit" className="btn-primary btn-block">
              تأیید سفارش مجازی
            </button>
          </form>

          <h3 className="section-title">فعال / در انتظار</h3>
          {(positions as Record<string, unknown>[]).map((p) => (
            <div key={String(p.public_id)} className="card position-card">
              <div className="history-card-top">
                <span className={p.direction === "LONG" ? "badge badge-long" : "badge badge-short"}>
                  {String(p.direction)} · {String(p.status)}
                </span>
              </div>
              <p className="meta-line">
                مارجین {String(p.margin_usdt)} · اهرم {String(p.leverage)} · ورود {String(p.entry_price ?? "—")}
              </p>
              {(p.metrics as Record<string, number>) && (
                <p className="hint">
                  PnL خالص (برآورد): {(p.metrics as Record<string, number>).net_unrealized_pnl} · RR خالص:{" "}
                  {(p.metrics as Record<string, number>).net_rr}
                </p>
              )}
              <div className="btn-row">
                {p.status === "pending_entry" && (
                  <button
                    type="button"
                    className="secondary"
                    onClick={() =>
                      apiFetch(`/api/gui/position/positions/${p.public_id}/cancel`, { method: "POST" }).then(loadPositions)
                    }
                  >
                    لغو
                  </button>
                )}
                {p.status === "open" && (
                  <button
                    type="button"
                    className="secondary"
                    onClick={() =>
                      apiFetch(`/api/gui/position/positions/${p.public_id}/close`, { method: "POST" }).then(() => {
                        loadPositions();
                        loadWallet();
                      })
                    }
                  >
                    بستن دستی
                  </button>
                )}
              </div>
            </div>
          ))}
        </section>
      )}

      {tab === "report" && report && (
        <section className="section position-report-print">
          <div className="filter-row no-print">
            {(["today", "7d", "30d", "all", "custom"] as const).map((p) => (
              <button key={p} type="button" className={period === p ? "active" : "secondary"} onClick={() => setPeriod(p)}>
                {p === "today"
                  ? "امروز"
                  : p === "7d"
                    ? "۷ روز"
                    : p === "30d"
                      ? "۳۰ روز"
                      : p === "custom"
                        ? "بازه دلخواه"
                        : "همه"}
              </button>
            ))}
            {period === "custom" && (
              <>
                <input type="date" value={customStart} onChange={(e) => setCustomStart(e.target.value)} />
                <input type="date" value={customEnd} onChange={(e) => setCustomEnd(e.target.value)} />
                <button type="button" className="secondary" onClick={() => loadReport().catch((e) => setMsg(String(e)))}>
                  اعمال
                </button>
              </>
            )}
            <button
              type="button"
              className="secondary"
              onClick={async () => {
                const qs = new URLSearchParams({ period });
                if (period === "custom") {
                  if (customStart) qs.set("start_date", customStart);
                  if (customEnd) qs.set("end_date", customEnd);
                }
                const resp = await fetch(`/api/gui/position/reports/export.csv?${qs}`, { credentials: "include" });
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `position-report-${period}.csv`;
                a.click();
                URL.revokeObjectURL(url);
              }}
            >
              CSV
            </button>
            <button type="button" className="secondary" onClick={() => window.print()}>
              چاپ
            </button>
          </div>
          <h2 className="print-only">گزارش عملکرد پوزیشن (Paper)</h2>
          <div className="stat-grid">
            <div className="stat-card">
              <span className="stat-label">معاملات</span>
              <strong>{report.total_trades as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">برد / باخت</span>
              <strong>
                {report.winning_trades as number} / {report.losing_trades as number}
              </strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Win rate</span>
              <strong>{report.win_rate as number}%</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">سود ناخالص</span>
              <strong>{report.gross_profits as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">ضرر ناخالص</span>
              <strong>{report.gross_losses as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">سود خالص (تحقق‌یافته)</span>
              <strong>{report.net_profit_loss as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">PnL تحقق‌نیافته</span>
              <strong>{report.unrealized_pnl as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">اکوییتی</span>
              <strong>{report.account_equity as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Profit factor</span>
              <strong>{report.profit_factor as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">میانگین R:R</span>
              <strong>{report.average_rr as number}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Max DD</span>
              <strong>{report.max_drawdown_pct as number}%</strong>
            </div>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>نماد</th>
                <th>جهت</th>
                <th>ورود</th>
                <th>خروج</th>
                <th>بسته (تهران)</th>
                <th>PnL</th>
              </tr>
            </thead>
            <tbody>
              {((report.history as Record<string, unknown>[]) || []).map((row, i) => (
                <tr key={i}>
                  <td>{String(row.symbol)}</td>
                  <td>{String(row.direction)}</td>
                  <td>{String(row.entry_price)}</td>
                  <td>{String(row.exit_price)}</td>
                  <td>{displayTehranTime(row.closed_at_tehran as string)}</td>
                  <td>{String(row.net_pnl_usdt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

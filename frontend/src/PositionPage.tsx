import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import { mergeOpenFormDefaults, type OpenFormState } from "./positionFormMerge";
import { EquityCurve, MoneyValue, numberText, PositionTradeCard, StatTile, tpModelExecutionText } from "./PositionVisuals";
import { PositionPerformancePreview, PositionWalletOverview } from "./PositionOverview";

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
  { id: "open", label: "پوزیشن‌ها" },
  { id: "report", label: "گزارش" },
];

type Props = {
  prefill: PositionPrefill | null;
  onConsumePrefill: () => void;
  initialTab?: Tab;
};

export function PositionPage({ prefill, onConsumePrefill, initialTab }: Props) {
  const [tab, setTab] = useState<Tab>(initialTab || "open");
  const [msg, setMsg] = useState("");
  const [wallet, setWallet] = useState<Record<string, unknown> | null>(null);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [positions, setPositions] = useState<unknown[]>([]);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [overviewReport, setOverviewReport] = useState<Record<string, unknown> | null>(null);
  const [period, setPeriod] = useState("30d");
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

  const loadOverviewReport = useCallback(async () => {
    setOverviewReport(await apiFetch("/api/gui/position/reports?period=30d"));
  }, []);

  useEffect(() => {
    if (tab === "wallet") loadWallet().catch((e) => setMsg(String(e)));
    if (tab === "settings") loadSettings().catch((e) => setMsg(String(e)));
    if (tab === "open") {
      loadSettings().catch((e) => setMsg("خطا در دریافت تنظیمات: " + String(e)));
      loadPositions().catch((e) => setMsg("خطا در دریافت پوزیشن‌ها: " + String(e)));
      loadWallet().catch((e) => setMsg("خطا در دریافت کیف پول: " + String(e)));
      loadOverviewReport().catch((e) => setMsg("خطا در دریافت گزارش: " + String(e)));
    }
    if (tab === "report") loadReport().catch((e) => setMsg(String(e)));
  }, [tab, loadWallet, loadSettings, loadPositions, loadReport, loadOverviewReport]);

  useEffect(() => {
    if (settings && tab === "open") {
      setForm((f) => mergeOpenFormDefaults(f, settings, preserveExecutionRef.current));
    }
  }, [settings, tab]);

  const settingsForm = useMemo(() => settings || {}, [settings]);
  const activePositions = positions.filter((p) => (p as Record<string, unknown>).status !== "pending_entry");
  const pendingPositions = positions.filter((p) => (p as Record<string, unknown>).status === "pending_entry");

  const formMargin = Number(form.margin_usdt);
  const formLeverage = Number(form.leverage);
  const orderNotional = Number.isFinite(formMargin) && Number.isFinite(formLeverage) &&
    formMargin > 0 && formLeverage > 0 ? formMargin * formLeverage : null;
  const makerRate = Number(settingsForm.maker_fee_rate);
  const takerRate = Number(settingsForm.taker_fee_rate);
  const estimatedRoundTripFees = orderNotional !== null && settings &&
    Number.isFinite(makerRate) && Number.isFinite(takerRate) &&
    makerRate >= 0 && takerRate >= 0
    ? orderNotional * (form.order_type === "limit" ? makerRate + takerRate : takerRate * 2)
    : null;

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
        auto_open_paper_positions: Boolean(settingsForm.auto_open_paper_positions),
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
    <div className="page position-page">
      <header className="page-header position-page__header">
        <div>
          <h1>مدیریت پوزیشن</h1>
          <p className="hint">معاملات آزمایشی · گزارش مالی · شبیه‌ساز Bitunix</p>
        </div>
        <span className="position-page__paper"><span aria-hidden="true" /> PAPER ONLY</span>
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
        <section className="section position-wallet-section">
          <div className="position-section-heading">
            <div><h2>نمای کلی کیف پول</h2><p>موجودی و عملکرد حساب آزمایشی</p></div>
          </div>
          <div className="position-stat-grid position-stat-grid--wallet">
            <StatTile label="اکوییتی حساب" emphasis>
              <MoneyValue value={wallet.account_equity} />
            </StatTile>
            <StatTile label="موجودی کل">
              <MoneyValue value={wallet.wallet_balance} />
            </StatTile>
            <StatTile label="موجودی آزاد">
              <MoneyValue value={wallet.available_balance} />
            </StatTile>
            <StatTile label="مارجین در استفاده">
              <MoneyValue value={wallet.margin_in_use} />
            </StatTile>
            <StatTile label="سود / زیان تحقق‌یافته">
              <MoneyValue value={wallet.realized_pnl} signed />
            </StatTile>
            <StatTile label="سود / زیان تحقق‌نیافته">
              <MoneyValue value={wallet.unrealized_pnl} signed />
            </StatTile>
            <StatTile label="کارمزد کل" footnote="هزینه معاملات">
              <MoneyValue value={wallet.total_fees} />
            </StatTile>
          </div>

          <div className="position-wallet-actions">
            {!wallet.initial_balance_set && (
              <div className="card form-card">
                <h3>موجودی اولیه</h3>
                <label htmlFor="position-initial-balance">مبلغ به USDT</label>
                <input id="position-initial-balance" value={initialBal}
                  onChange={(e) => setInitialBal(e.target.value)} inputMode="decimal" />
                <button type="button" className="btn-primary" onClick={() =>
                  apiFetch("/api/gui/position/wallet/initial", {
                    method: "POST",
                    body: JSON.stringify({ amount_usdt: Number(initialBal) }),
                  }).then(() => loadWallet())
                }>تنظیم موجودی اولیه</button>
              </div>
            )}
            <div className="card form-card position-wallet-transfer">
              <h3>مدیریت موجودی آزمایشی</h3>
              <div className="position-wallet-transfer__fields">
                <div>
                  <label htmlFor="position-deposit">واریز USDT</label>
                  <input id="position-deposit" value={deposit}
                    onChange={(e) => setDeposit(e.target.value)} inputMode="decimal" />
                  <button type="button" className="secondary" onClick={() =>
                    apiFetch("/api/gui/position/wallet/deposit", {
                      method: "POST", body: JSON.stringify({ amount_usdt: Number(deposit) }),
                    }).then(() => { setDeposit(""); loadWallet(); })
                  }>واریز به کیف پول</button>
                </div>
                <div>
                  <label htmlFor="position-withdraw">برداشت USDT</label>
                  <input id="position-withdraw" value={withdraw}
                    onChange={(e) => setWithdraw(e.target.value)} inputMode="decimal" />
                  <button type="button" className="secondary" onClick={() =>
                    apiFetch("/api/gui/position/wallet/withdraw", {
                      method: "POST", body: JSON.stringify({ amount_usdt: Number(withdraw) }),
                    }).then(() => { setWithdraw(""); loadWallet(); })
                  }>برداشت از کیف پول</button>
                </div>
              </div>
            </div>
          </div>

          <div className="card position-ledger-panel">
            <div className="position-section-heading">
              <div><h2>تاریخچه تراکنش‌ها</h2><p>ورودی‌ها و خروجی‌های کیف پول آزمایشی</p></div>
            </div>
            <ul className="ledger-list">
              {((wallet.ledger as { entry_type: string; amount_usdt: number; created_at_tehran: string }[]) || []).map(
                (row, i) => (
                  <li key={i} className="ledger-row position-ledger-row">
                    <span className="position-ledger-row__type">{row.entry_type}</span>
                    <MoneyValue value={row.amount_usdt} />
                    <span className="position-ledger-row__time">{displayTehranTime(row.created_at_tehran)}</span>
                  </li>
                ),
              )}
              {(!Array.isArray(wallet.ledger) || wallet.ledger.length === 0) &&
                <li className="position-empty-list">تراکنشی ثبت نشده است.</li>}
            </ul>
          </div>
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
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={Boolean(settingsForm.auto_open_paper_positions)}
              onChange={(e) =>
                setSettings({ ...settingsForm, auto_open_paper_positions: e.target.checked })
              }
            />
            Auto-open paper positions after valid GPT ICT setup (paper only)
          </label>
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
        <div className="position-open-dashboard">
          <PositionWalletOverview wallet={wallet} />
          <section className="section position-trading-layout">
          <form id="position-new-order" className="card form-card position-order-form" onSubmit={submitOpen}>
            <div className="position-section-heading">
              <div><h2>ثبت سفارش جدید</h2><p>Paper Trading · محاسبات شبیه‌سازی‌شده</p></div>
            </div>
            <div className="position-order-fields">
              <div className="position-order-fields__wide">
                <span className="position-form-label">جهت معامله</span>
                <div className="position-direction-select" role="group" aria-label="جهت معامله">
                  <button type="button" aria-pressed={form.direction === "LONG"}
                    className={form.direction === "LONG" ? "position-direction-choice position-direction-choice--long active" : "position-direction-choice position-direction-choice--long"}
                    onClick={() => setForm({ ...form, direction: "LONG" })}>LONG</button>
                  <button type="button" aria-pressed={form.direction === "SHORT"}
                    className={form.direction === "SHORT" ? "position-direction-choice position-direction-choice--short active" : "position-direction-choice position-direction-choice--short"}
                    onClick={() => setForm({ ...form, direction: "SHORT" })}>SHORT</button>
                </div>
              </div>
              <div>
                <label htmlFor="position-margin">مارجین (USDT)</label>
                <input id="position-margin" value={form.margin_usdt}
                  onChange={(e) => setForm({ ...form, margin_usdt: e.target.value })} inputMode="decimal" />
              </div>
              <div>
                <label htmlFor="position-leverage">اهرم</label>
                <input id="position-leverage" value={form.leverage}
                  onChange={(e) => setForm({ ...form, leverage: e.target.value })} inputMode="numeric" />
              </div>
              <div>
                <label htmlFor="position-order-type">نوع ورود</label>
                <select id="position-order-type" value={form.order_type}
                  onChange={(e) => setForm({ ...form, order_type: e.target.value })}>
                  <option value="market">Market</option>
                  <option value="limit">Limit</option>
                </select>
              </div>
              {form.order_type === "limit" && (
                <div className="position-order-fields__wide">
                  <label htmlFor="position-entry">قیمت ورود</label>
                  <input id="position-entry" value={form.entry_price}
                    onChange={(e) => setForm({ ...form, entry_price: e.target.value })} inputMode="decimal" />
                </div>
              )}
              <div>
                <label htmlFor="position-stop">حد ضرر (SL)</label>
                <input id="position-stop" value={form.stop_loss}
                  onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} inputMode="decimal" />
              </div>
              <div>
                <label htmlFor="position-take">حد سود (TP)</label>
                <input id="position-take" value={form.take_profit}
                  onChange={(e) => setForm({ ...form, take_profit: e.target.value })} inputMode="decimal" />
              </div>
            </div>
            {form.ai_setup_id && <p className="hint">AI setup: <bdi dir="ltr">{form.ai_setup_id}</bdi></p>}
            <div className="position-order-summary">
              <h3>خلاصه سفارش</h3>
              <div><span>مارجین مورد نیاز</span><MoneyValue value={formMargin > 0 ? formMargin : null} /></div>
              <div><span>ارزش اسمی پوزیشن</span><MoneyValue value={orderNotional} /></div>
              <div><span>کارمزد تقریبی رفت‌وبرگشت</span><MoneyValue value={estimatedRoundTripFees} decimals={4} /></div>
              <p>برآورد کارمزد با فرض اجرای Limit به‌صورت Maker است؛ نرخ و هزینه واقعی ممکن است متفاوت باشند.</p>
            </div>
            <button type="submit" className="btn-primary btn-block">تأیید سفارش مجازی</button>
            <p className="position-order-form__note">سفارش صرفاً Paper است و به بازار واقعی ارسال نمی‌شود.</p>
          </form>

          <div className="position-trading-list">
            <div className="position-section-heading">
              <div><h2>پوزیشن‌های باز</h2><p>وضعیت معاملات فعال با داده‌های موجود بازار</p></div>
              <div className="position-list-heading__actions">
                <span className="position-count" dir="ltr">{activePositions.length}</span>
                <a className="position-new-order-link" href="#position-new-order">+ سفارش جدید</a>
              </div>
            </div>
            {activePositions.length === 0 && (
              <div className="position-empty-list position-empty-list--card">
                <strong>پوزیشن بازی وجود ندارد</strong>
                <p>با ثبت سفارش آزمایشی، جزئیات معامله در این بخش ظاهر می‌شود.</p>
              </div>
            )}
            {(activePositions as Record<string, unknown>[]).map((p) => (
              <PositionTradeCard key={String(p.public_id)} position={p}
                onCancel={() => {
                  apiFetch("/api/gui/position/positions/" + p.public_id + "/cancel", { method: "POST" })
                    .then(() => loadPositions()).catch((e) => setMsg(String(e)));
                }}
                onClose={() => {
                  apiFetch("/api/gui/position/positions/" + p.public_id + "/close", { method: "POST" })
                    .then(() => { loadPositions(); loadWallet(); loadOverviewReport(); }).catch((e) => setMsg(String(e)));
                }}
              />
            ))}
            <div className="position-section-heading position-pending-heading">
              <div><h2>سفارش‌های در انتظار ورود</h2><p>تا پیش از تکمیل ورود، سود و زیان محاسبه نمی‌شود</p></div>
              <span className="position-count position-count--pending" dir="ltr">{pendingPositions.length}</span>
            </div>
            {pendingPositions.length === 0 && (
              <div className="position-empty-list position-empty-list--card">
                سفارش در انتظار ورود وجود ندارد.
              </div>
            )}
            {(pendingPositions as Record<string, unknown>[]).map((p) => (
              <PositionTradeCard key={String(p.public_id)} position={p}
                onCancel={() => {
                  apiFetch("/api/gui/position/positions/" + p.public_id + "/cancel", { method: "POST" })
                    .then(() => loadPositions()).catch((e) => setMsg(String(e)));
                }}
                onClose={() => {
                  apiFetch("/api/gui/position/positions/" + p.public_id + "/close", { method: "POST" })
                    .then(() => { loadPositions(); loadWallet(); loadOverviewReport(); }).catch((e) => setMsg(String(e)));
                }}
              />
            ))}
          </div>
          </section>
          <PositionPerformancePreview report={overviewReport} onOpenReport={() => setTab("report")} />
        </div>
      )}

      {tab === "report" && report && (
        <section className="section position-report-section position-report-print">
          <div className="position-section-heading">
            <div><h2>گزارش عملکرد معاملات</h2><p>نتایج واقعی سفارش‌های Paper در بازه انتخابی</p></div>
          </div>
          <div className="filter-row position-report-filters no-print">
            {(["today", "7d", "30d", "all", "custom"] as const).map((p) => (
              <button key={p} type="button" className={period === p ? "active" : "secondary"} onClick={() => setPeriod(p)}>
                {p === "today" ? "امروز" : p === "7d" ? "۷ روز" : p === "30d" ? "۳۰ روز" : p === "custom" ? "بازه دلخواه" : "همه"}
              </button>
            ))}
            {period === "custom" && (
              <div className="position-report-custom">
                <input type="date" aria-label="شروع بازه" value={customStart} onChange={(e) => setCustomStart(e.target.value)} />
                <input type="date" aria-label="پایان بازه" value={customEnd} onChange={(e) => setCustomEnd(e.target.value)} />
                <button type="button" className="secondary" onClick={() => loadReport().catch((e) => setMsg(String(e)))}>اعمال</button>
              </div>
            )}
            <div className="position-report-filters__actions">
              <button type="button" className="secondary" onClick={async () => {
                try {
                  const qs = new URLSearchParams({ period });
                  if (period === "custom") {
                    if (customStart) qs.set("start_date", customStart);
                    if (customEnd) qs.set("end_date", customEnd);
                  }
                  const resp = await fetch("/api/gui/position/reports/export.csv?" + qs, { credentials: "include" });
                  if (!resp.ok) throw new Error("خطا در دریافت فایل CSV");
                  const blob = await resp.blob();
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = "position-report-" + period + ".csv";
                  a.click();
                  URL.revokeObjectURL(url);
                } catch (e) { setMsg(e instanceof Error ? e.message : "خطا در دریافت گزارش"); }
              }}>CSV</button>
              <button type="button" className="secondary" onClick={() => window.print()}>چاپ</button>
            </div>
          </div>
          <h2 className="print-only">گزارش عملکرد پوزیشن (Paper)</h2>
          <div className="position-stat-grid position-stat-grid--report">
            <StatTile label="سود / زیان خالص" emphasis footnote="تحقق‌یافته پس از هزینه‌ها">
              <MoneyValue value={report.net_profit_loss} signed />
            </StatTile>
            <StatTile label="اکوییتی حساب" emphasis>
              <MoneyValue value={report.account_equity} />
            </StatTile>
            <StatTile label="نرخ برد">
              <span dir="ltr">{numberText(report.win_rate, 1)}%</span>
            </StatTile>
            <StatTile label="تعداد معاملات">
              <span dir="ltr">{numberText(report.total_trades, 0)}</span>
            </StatTile>
            <StatTile label="سود ناخالص">
              <MoneyValue value={report.gross_profits} tone="profit" />
            </StatTile>
            <StatTile label="زیان ناخالص">
              <MoneyValue value={report.gross_losses} tone="loss" />
            </StatTile>
            <StatTile label="PnL تحقق‌نیافته">
              <MoneyValue value={report.unrealized_pnl} signed />
            </StatTile>
            <StatTile label="Profit Factor">
              <span dir="ltr">{numberText(report.profit_factor)}</span>
            </StatTile>
            <StatTile label="میانگین R:R">
              <span dir="ltr">{numberText(report.average_rr)}</span>
            </StatTile>
            <StatTile label="حداکثر افت سرمایه">
              <MoneyValue value={report.max_drawdown_pct} unit="%" tone="loss" />
            </StatTile>
            <StatTile label="برد / باخت">
              <span dir="ltr">{numberText(report.winning_trades, 0)} / {numberText(report.losing_trades, 0)}</span>
            </StatTile>
            <StatTile label="کارمزد کل">
              <MoneyValue value={report.fees} />
            </StatTile>
          </div>
          <div className="card position-chart-panel">
            <EquityCurve history={report.equity_history} />
          </div>
          <div className="card position-report-history">
            <div className="position-section-heading">
              <div><h2>تاریخچه معاملات</h2><p>سود و زیان خالص هر معامله با احتساب هزینه‌ها</p></div>
            </div>
            <div className="position-table-scroll" role="region" tabIndex={0} aria-label="تاریخچه معاملات">
              <table className="data-table position-performance-table">
                <thead><tr>
                  <th>نماد</th><th>جهت</th><th>ورود</th><th>خروج</th>
                  <th>TP مدل ← اجرایی</th><th>زمان بسته‌شدن (تهران)</th><th>کارمزد</th><th>PnL خالص</th>
                </tr></thead>
                <tbody>
                  {((report.history as Record<string, unknown>[]) || []).map((row, i) => (
                    <tr key={String(row.public_id ?? i)}>
                      <td><strong dir="ltr">{String(row.symbol ?? "—")}</strong></td>
                      <td><span className={"position-direction position-direction--" +
                        (row.direction === "LONG" ? "long" : "short")}>{String(row.direction ?? "—")}</span></td>
                      <td dir="ltr">{numberText(row.entry_price)}</td>
                      <td dir="ltr">{numberText(row.exit_price)}</td>
                      <td dir="ltr">{tpModelExecutionText(row.take_profit, row.tp_execution_policy)}</td>
                      <td>{displayTehranTime(row.closed_at_tehran as string)}</td>
                      <td><MoneyValue value={row.fees_usdt} /></td>
                      <td><MoneyValue value={row.net_pnl_usdt} signed /></td>
                    </tr>
                  ))}
                  {(!Array.isArray(report.history) || report.history.length === 0) &&
                    <tr><td colSpan={8} className="position-empty-list">معامله بسته‌شده‌ای در این بازه وجود ندارد.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

    </div>
  );
}

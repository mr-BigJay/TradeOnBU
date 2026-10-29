import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import "./bitunixAccount.css";

type NumberLike = string | null;
type Account = {
  currency: string; available: NumberLike; frozen: NumberLike;
  position_margin: NumberLike; unrealized_pnl: NumberLike;
  cross_unrealized_pnl: NumberLike; isolated_unrealized_pnl: NumberLike;
  transferable: NumberLike; bonus: NumberLike; position_mode: string;
};
type Position = {
  id: string; side: string; leverage: NumberLike; margin_mode: string;
  qty: NumberLike; entry_price: NumberLike; margin: NumberLike;
  entry_value: NumberLike; unrealized_pnl: NumberLike; realized_pnl: NumberLike;
  roi_pct: NumberLike; liq_price: NumberLike; margin_rate: NumberLike;
  fee: NumberLike; funding: NumberLike; created_at: number | null;
};
type Order = {
  id: string; side: string; type: string; status: string;
  qty: NumberLike; filled_qty: NumberLike; price: NumberLike; leverage: NumberLike;
  margin_mode: string; reduce_only: boolean; tp_price: NumberLike;
  sl_price: NumberLike; created_at: number | null;
};
type Tpsl = {
  id: string; position_id: string; tp_price: NumberLike; sl_price: NumberLike;
  tp_qty: NumberLike; sl_qty: NumberLike; tp_type: string; sl_type: string;
};
type Closed = {
  id: string; side: string; leverage: NumberLike; margin_mode: string; qty: NumberLike;
  entry_price: NumberLike; close_price: NumberLike; realized_pnl: NumberLike;
  fee: NumberLike; funding: NumberLike; closed_at: number | null;
};
type Snapshot = {
  configured: boolean; read_only: boolean; live_trading: boolean; symbol?: string;
  updated_at?: string; account?: Account; settings?: { leverage: NumberLike; margin_mode: string };
  positions?: Position[]; orders?: Order[]; tpsl_orders?: Tpsl[];
  history?: { positions: Closed[]; displayed: number; total: number | null };
};

function fmt(v: NumberLike | undefined, digits = 4): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n)
    ? n.toLocaleString("en-US", { maximumFractionDigits: digits })
    : "—";
}
function pnl(v: NumberLike | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return (Number(v) > 0 ? "+" : "") + fmt(v);
}
function colorPnl(v: NumberLike | undefined) {
  return v != null && Number(v) !== 0
    ? Number(v) > 0 ? "bu-profit" : "bu-loss" : "";
}
function dateFmt(ms: number | null | undefined) {
  if (ms == null) return "—";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("fa-IR", {
    timeZone: "Asia/Tehran", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}
function Tag({ side }: { side: string }) {
  const t = side?.toUpperCase() || "UNKNOWN";
  return <span className={t === "LONG" || t === "BUY" ? "bu-tag bu-tag--long" : t === "SHORT" || t === "SELL" ? "bu-tag bu-tag--short" : "bu-tag"}>
    {t}
  </span>;
}
function Metric({ label, value, unit, change }: { label: string; value: NumberLike | undefined; unit?: string; change?: boolean }) {
  return <div className="bu-metric">
    <span className="bu-metric__label">{label}</span>
    <strong className={change ? colorPnl(value) : ""} dir="ltr">
      {change ? pnl(value) : fmt(value)}{value != null && unit ? <small> {unit}</small> : null}
    </strong>
  </div>;
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="bu-field"><span>{label}</span><strong dir="ltr">{children}</strong></div>;
}

export function BitunixAccountPage() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  const load = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    try {
      const result: Snapshot = await apiFetch("/api/gui/bitunix/snapshot");
      if (!mounted.current) return;
      setData(result);
      setError("");
      setLastRefresh(result.updated_at || null);
    } catch (e) {
      if (!mounted.current) return;
      // Clear old balances and positions: never pass stale data off as current.
      setData(null);
      setLastRefresh(null);
      setError(e instanceof Error ? e.message : "خطا در دریافت اطلاعات Bitunix");
    } finally {
      busy.current = false;
      if (mounted.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 45_000);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, [load]);

  const positions = data?.positions || [];
  const orders = data?.orders || [];
  const tpsl = data?.tpsl_orders || [];
  const history = data?.history?.positions || [];

  return <div className="page bu-page" dir="rtl">
    <header className="page-header bu-header">
      <div>
        <h1>حساب واقعی Bitunix</h1>
        <p className="hint">گزارش حساب فیوچرز · BTCUSDT · مستقل از معاملات آزمایشی</p>
      </div>
      <span className="bu-readonly">فقط مشاهده</span>
    </header>
    <div className="bu-toolbar">
      <div className="bu-sync">
        <span className={error ? "bu-sync__dot bu-sync__dot--error" : data?.configured ? "bu-sync__dot" : "bu-sync__dot bu-sync__dot--off"} />
        {error ? "خطای اتصال" : !data?.configured ? "منتظر پیکربندی API" : "اتصال به حساب واقعی"}
        {lastRefresh && <small>به‌روزرسانی: {dateFmt(Date.parse(lastRefresh))} (تهران)</small>}
      </div>
      <button type="button" className="secondary" onClick={() => void load()} disabled={loading} aria-label="به‌روزرسانی حساب">
        {loading ? "در حال دریافت…" : "↻ به‌روزرسانی"}
      </button>
    </div>
    {error && <p role="alert" className="toast toast-error">{error}</p>}
    {!data && !error && <div className="card">در حال دریافت اطلاعات حساب…</div>}
    {data && !data.configured && <div className="card bu-note" role="status">
      <h2>اتصال دائمی API تنظیم نشده</h2>
      <p>تست موقت اتصال موفق بوده، ولی برای نمایش گرافیکی باید کلیدهای API داخل تنظیمات امن سرور ثبت و کانتینر دوباره ساخته شود. کلیدها را در مرورگر یا چت وارد نکن.</p>
      <code dir="ltr">BITUNIX_MONITOR_API_KEY / BITUNIX_MONITOR_SECRET_KEY</code>
    </div>}
    {data?.configured && data.account && <>
      <section className="bu-section" aria-label="خلاصه حساب">
        <div className="bu-section__heading"><h2>خلاصه حساب</h2><span className="bu-section__meta">USDT Futures</span></div>
        <div className="bu-metrics">
          <Metric label="موجودی آزاد" value={data.account.available} unit="USDT" />
          <Metric label="مارجین پوزیشن‌ها" value={data.account.position_margin} unit="USDT" />
          <Metric label="مبلغ قفل‌شده سفارش‌ها" value={data.account.frozen} unit="USDT" />
          <Metric label="سود / زیان شناور کل" value={data.account.unrealized_pnl} unit="USDT" change />
          <Metric label="حداکثر موجودی قابل انتقال" value={data.account.transferable} unit="USDT" />
          <Metric label="بونوس فیوچرز" value={data.account.bonus} unit="USDT" />
        </div>
        <div className="bu-configuration">
          <div><span>حالت پوزیشن</span><strong dir="ltr">{data.account.position_mode || "—"}</strong></div>
          <div><span>لوریج تنظیم‌شده BTC</span><strong dir="ltr">{fmt(data.settings?.leverage, 0)}×</strong></div>
          <div><span>حالت مارجین BTC</span><strong dir="ltr">{data.settings?.margin_mode || "—"}</strong></div>
        </div>
        <p className="bu-footnote">لوریج بالا، تنظیم فعلی نماد است؛ لوریج هر پوزیشن یا سفارش ممکن است متفاوت باشد.</p>
      </section>
      <section className="bu-section" aria-label="پوزیشن‌های واقعی باز">
        <div className="bu-section__heading"><h2>پوزیشن‌های باز</h2><span className="bu-count">{positions.length}</span></div>
        {!positions.length && <div className="card bu-empty">در حال حاضر پوزیشن باز BTCUSDT وجود ندارد.</div>}
        {positions.map((p, index) => <article className="card bu-trade" key={p.id || index}>
          <div className="bu-trade__top"><div><Tag side={p.side} /><strong> BTCUSDT</strong></div>
            <span dir="ltr" className="bu-meta">{fmt(p.leverage, 0)}× · {p.margin_mode}</span>
          </div>
          <div className="bu-trade__pnl">
            <span>سود / زیان شناور</span><strong className={colorPnl(p.unrealized_pnl)} dir="ltr">{pnl(p.unrealized_pnl)} USDT</strong>
            <span className={colorPnl(p.roi_pct)} dir="ltr">ROI تقریبی: {pnl(p.roi_pct)}%</span>
          </div>
          <div className="bu-fields">
            <Field label="حجم">{fmt(p.qty, 8)} BTC</Field>
            <Field label="قیمت ورود">{fmt(p.entry_price, 2)}</Field>
            <Field label="مارجین">{fmt(p.margin)} USDT</Field>
            <Field label="قیمت لیکویید">{fmt(p.liq_price, 2)}</Field>
            <Field label="نرخ مارجین">{p.margin_rate == null ? "—" : fmt(p.margin_rate, 6)}</Field>
            <Field label="سود تحقق‌یافته">{pnl(p.realized_pnl)} USDT</Field>
            <Field label="کارمزد ثبت‌شده">{fmt(p.fee)} USDT</Field>
            <Field label="فاندینگ">{pnl(p.funding)} USDT</Field>
          </div>
          <p className="bu-footnote">ROI بر اساس مارجین فعلی محاسبه شده؛ کارمزد و فاندینگ جداگانه نمایش داده می‌شوند.</p>
        </article>)}
      </section>
      <section className="bu-section" aria-label="سفارش‌های در انتظار">
        <div className="bu-section__heading"><h2>سفارش‌های در انتظار</h2><span className="bu-count">{orders.length}</span></div>
        {!orders.length && <div className="card bu-empty">سفارش عادی در انتظار BTCUSDT وجود ندارد.</div>}
        {orders.map((o, index) => <article className="card bu-trade" key={o.id || index}>
          <div className="bu-trade__top"><Tag side={o.side}/><span dir="ltr" className="bu-meta">{o.status || "—"} · {o.type || "—"}</span></div>
          <div className="bu-fields">
            <Field label="قیمت سفارش">{fmt(o.price, 2)}</Field>
            <Field label="حجم">{fmt(o.qty, 8)} BTC</Field>
            <Field label="حجم اجراشده">{fmt(o.filled_qty, 8)} BTC</Field>
            <Field label="لوریج">{fmt(o.leverage, 0)}×</Field>
            <Field label="مارجین">{o.margin_mode || "—"}</Field>
            <Field label="Reduce-only">{o.reduce_only ? "بله" : "خیر"}</Field>
            <Field label="حد سود همراه سفارش">{fmt(o.tp_price, 2)}</Field>
            <Field label="حد ضرر همراه سفارش">{fmt(o.sl_price, 2)}</Field>
          </div>
          <p className="bu-footnote">جهت BUY/SELL به‌تنهایی در حساب Hedge مشخص‌کننده بازکردن یا بستن پوزیشن نیست.</p>
        </article>)}
      </section>
      <section className="bu-section" aria-label="سفارش‌های حد سود و حد ضرر">
        <div className="bu-section__heading"><h2>سفارش‌های TP / SL مستقل</h2><span className="bu-count">{tpsl.length}</span></div>
        {!tpsl.length && <div className="card bu-empty">سفارش مستقل TP/SL برای BTCUSDT مشاهده نشد.</div>}
        {tpsl.map((x, index) => <article className="card bu-trade" key={x.id || index}>
          <div className="bu-fields">
            <Field label="شناسه پوزیشن">{x.position_id || "—"}</Field>
            <Field label="حد سود">{fmt(x.tp_price, 2)}</Field>
            <Field label="حجم TP">{fmt(x.tp_qty, 8)} BTC</Field>
            <Field label="نوع TP">{x.tp_type || "—"}</Field>
            <Field label="حد ضرر">{fmt(x.sl_price, 2)}</Field>
            <Field label="حجم SL">{fmt(x.sl_qty, 8)} BTC</Field>
            <Field label="نوع SL">{x.sl_type || "—"}</Field>
          </div>
        </article>)}
        <p className="bu-footnote">این فهرست وجود سفارش‌های شرطی را نشان می‌دهد؛ تضمین‌کننده پوشش کامل حجم پوزیشن نیست.</p>
      </section>
      <section className="bu-section" aria-label="تاریخچه پوزیشن‌های بسته‌شده">
        <div className="bu-section__heading"><h2>پوزیشن‌های بسته‌شده</h2>
          <span className="bu-section__meta">آخرین {history.length} مورد{data.history?.total != null ? " از " + data.history.total : ""}</span>
        </div>
        {!history.length && <div className="card bu-empty">تاریخچه پوزیشن بسته‌شده‌ای برای BTCUSDT دریافت نشد.</div>}
        {history.map((h, index) => <article className="card bu-trade" key={h.id || index}>
          <div className="bu-trade__top"><Tag side={h.side}/><span className="bu-meta">{dateFmt(h.closed_at)} (تهران)</span></div>
          <div className="bu-trade__pnl"><span>PNL تحقق‌یافته (قبل از کارمزد/فاندینگ)</span>
            <strong dir="ltr" className={colorPnl(h.realized_pnl)}>{pnl(h.realized_pnl)} USDT</strong>
          </div>
          <div className="bu-fields">
            <Field label="حجم بیشینه">{fmt(h.qty, 8)} BTC</Field>
            <Field label="لوریج">{fmt(h.leverage, 0)}×</Field>
            <Field label="قیمت ورود">{fmt(h.entry_price, 2)}</Field>
            <Field label="قیمت خروج">{fmt(h.close_price, 2)}</Field>
            <Field label="کارمزد">{fmt(h.fee)} USDT</Field>
            <Field label="فاندینگ">{pnl(h.funding)} USDT</Field>
          </div>
        </article>)}
      </section>
    </>}
    <p className="bu-bottom-note">اطلاعات مستقیم Bitunix · به‌روزرسانی خودکار هر ۴۵ ثانیه هنگام بازبودن این صفحه · فقط خواندنی · معاملات واقعی ربات همچنان غیرفعال است.</p>
  </div>;
}

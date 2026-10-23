import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import "./dailyOutlook.css";

type Level = { id: string; label_fa: string; price: number };
type Scenario = {
  title_fa: string; direction: "bullish" | "bearish" | "range";
  thesis_fa: string; activation_fa: string; invalidation_fa: string;
  activation_level_ids: string[]; invalidation_level_ids: string[]; target_level_ids: string[];
  path_fa: string[];
};
type Report = {
  summary_fa: string; weekly_context_fa: string; daily_structure_fa: string;
  dominant: Scenario; alternative: Scenario; uncertainty_fa: string;
  watch_next_fa: string[]; change_since_previous_fa: string;
};
type Run = {
  id: number; report_date: string; symbol: string; status: string; stale: boolean;
  started_at: string; completed_at: string | null; model: string | null; error_code?: string;
  report?: Report; usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number };
  context?: { as_of: string; source: string; live_price: number; daily_atr14: number;
    levels: Level[]; daily_candles: number[][]; data_quality_warnings?: unknown[] };
};
type Schedule = { enabled: boolean; hour: number; minute: number; next_run_at: string | null };
type Dashboard = { settings: Schedule; latest: Run | null; latest_completed: Run | null;
  history: Run[]; attempted_today: boolean; can_retry_today?: boolean };
const dir = { bullish: "صعودی", bearish: "نزولی", range: "رنج" };
const statusLabel: Record<string, string> = { completed: "آماده", running: "در حال تولید", failed: "ناموفق" };
const price = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 2 });

function LevelList({ ids, levels }: { ids: string[]; levels: Level[] }) {
  return <div className="outlook-level-chips">{ids.map(id => {
    const l = levels.find(v => v.id === id);
    return l ? <span key={id}>{l.label_fa} <bdi>{price(l.price)}</bdi></span> : null;
  })}</div>;
}

function ScenarioCard({ scenario: s, levels, primary }: { scenario: Scenario; levels: Level[]; primary: boolean }) {
  return <article className={`card outlook-scenario ${primary ? "outlook-primary" : ""}`}>
    <div className="outlook-card-top"><span>{primary ? "سناریوی غالب" : "سناریوی جایگزین"}</span>
      <span className={`outlook-badge ${s.direction}`}>{dir[s.direction]}</span></div>
    <h3>{s.title_fa}</h3><p>{s.thesis_fa}</p>
    <h4>چه زمانی فعال می‌شود؟</h4><p>{s.activation_fa}</p>
    <LevelList ids={s.activation_level_ids} levels={levels} />
    <h4>مسیر احتمالی، در صورت تحقق شروط</h4>
    <ol className="outlook-path">{s.path_fa.map((step, i) => <li key={i}>{step}</li>)}</ol>
    <h4>نواحی هدف احتمالی</h4><LevelList ids={s.target_level_ids} levels={levels} />
    <div className="outlook-invalidation"><h4>چه چیزی سناریو را باطل می‌کند؟</h4><p>{s.invalidation_fa}</p>
      <LevelList ids={s.invalidation_level_ids} levels={levels} /></div>
  </article>;
}

function HistoryChart({ bars }: { bars: number[][] }) {
  if (bars.length < 2) return null;
  const values = bars.map(b => b[4]);
  const lo = Math.min(...values), hi = Math.max(...values);
  const points = values.map((v, i) => `${30 + i / (values.length - 1) * 690},${160 - (v - lo) / Math.max(hi - lo, 1) * 130}`).join(" ");
  return <div className="outlook-history-chart" dir="ltr">
    <svg viewBox="0 0 750 190" role="img" aria-label="قیمت بسته‌شدن روزانه در ۳۰ روز گذشته؛ نمودار تاریخی">
      {[30, 95, 160].map(y => <line key={y} x1="30" y1={y} x2="720" y2={y} stroke="#253246" />)}
      <polyline points={points} fill="none" stroke="#5ed8b0" strokeWidth="3" />
      <text x="30" y="20" fill="#a6b4c8" fontSize="12">{price(hi)}</text>
      <text x="30" y="180" fill="#a6b4c8" fontSize="12">{price(lo)}</text>
    </svg>
    <div className="outlook-chart-dates"><span>{new Date(bars[0][0]).toISOString().slice(0, 10)}</span>
      <span>{new Date(bars[bars.length - 1][0]).toISOString().slice(0, 10)}</span></div>
  </div>;
}

export function DailyOutlookPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [selected, setSelected] = useState<Run | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [time, setTime] = useState("08:05");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const initialized = useRef(false);
  const selectionRequest = useRef(0);
  const refresh = useCallback(async () => {
    const result: Dashboard = await apiFetch("/api/gui/daily-outlook");
    setData(result);
    if (!initialized.current) {
      setEnabled(result.settings.enabled);
      setTime(`${String(result.settings.hour).padStart(2, "0")}:${String(result.settings.minute).padStart(2, "0")}`);
      initialized.current = true;
    }
  }, []);
  useEffect(() => {
    refresh().catch(e => setError(e.message));
    const timer = window.setInterval(() => refresh().catch(e => setError(e.message)), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const action = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : "خطا در دریافت اطلاعات"); }
    finally { setBusy(false); }
  };
  const run = selected || data?.latest_completed;
  const report = run?.report;
  const levels = run?.context?.levels || [];

  return <section className="daily-outlook" dir="rtl">
    <header className="outlook-heading"><div><span className="outlook-eyebrow">تصویر بزرگ‌تر بازار</span>
      <h2>چشم‌انداز روزانه</h2><p>سناریوهای مشروط ۳ تا ۷ روز آینده، بر پایهٔ ساختار هفتگی، روزانه و ۴ساعته</p></div>
      <span className="outlook-horizon">۳–۷ روز</span></header>
    <div className="card outlook-controls">
      <label className="outlook-toggle"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />تحلیل خودکار روزانه</label>
      <label>ساعت تهران <input aria-label="ساعت تهران" type="time" value={time} onChange={e => setTime(e.target.value)} /></label>
      <button type="button" disabled={busy || !data || !/^\d{2}:\d{2}$/.test(time)} onClick={() => action(async () => {
        const [hour, minute] = time.split(":").map(Number);
        await apiFetch("/api/gui/daily-outlook/settings", { method: "PUT", body: JSON.stringify({ enabled, hour, minute }) });
        setNotice("زمان‌بندی ذخیره شد.");
      })}>ذخیره زمان‌بندی</button>
      <button type="button" className="secondary" disabled={busy || !data || (data.attempted_today && !data.can_retry_today)} onClick={() => action(async () => {
        await apiFetch("/api/gui/daily-outlook/generate", { method: "POST" });
        selectionRequest.current++; setSelected(null); setNotice("تولید گزارش شروع شد؛ نتیجه پس از تکمیل نمایش داده می‌شود.");
      })}>{data?.can_retry_today ? "تلاش مجدد گزارش امروز" : "تولید گزارش امروز"}</button>
      <p className="hint outlook-control-note">حداکثر یک درخواست AI در روز برای هر نماد؛ خطای داده پیش از ارسال به AI قابل تلاش مجدد دستی است. با اتصال API مصرف جداگانه دارد.</p>
      <p className="hint outlook-control-note">{data?.settings.enabled ? `نوبت بعد: ${displayTehranTime(null, data.settings.next_run_at)}` : "زمان‌بندی خودکار خاموش است."}</p>
    </div>
    {error && <p role="alert" className="toast toast-error">{error}</p>}
    {notice && <p role="status" className="toast">{notice}</p>}
    {data?.latest?.status === "running" && <p role="status" className="card outlook-status">گزارش امروز در حال تولید است؛ ممکن است چند دقیقه زمان ببرد.</p>}
    {data?.latest?.status === "failed" && <div role="alert" className="card outlook-status">تولید گزارش {data.latest.report_date} ناموفق بود. گزارش قبلی، در صورت وجود، با تاریخ خودش نمایش داده می‌شود.
      <p className="hint">کد خطا: <bdi>{data.latest.error_code}</bdi></p></div>}
    {!data && !error && <p>در حال دریافت اطلاعات…</p>}
    {data && !report && <div className="card outlook-empty"><h3>اولین چشم‌انداز را بسازید</h3><p>گزارش شامل سناریوی غالب و جایگزین، نواحی کلیدی، شروط ابطال و موارد قابل پیگیری خواهد بود.</p></div>}
    {report && run && <>
      <div className="card outlook-summary">
        <div className="outlook-card-top"><span><bdi>{run.symbol}</bdi> · {run.report_date}</span><span className={`outlook-badge ${report.dominant.direction}`}>{dir[report.dominant.direction]}</span></div>
        {run.stale && <p className="outlook-stale">این گزارش مربوط به روز گذشته یا قبل‌تر است؛ به‌عنوان تحلیل تازه استفاده نشود.</p>}
        {!!run.context?.data_quality_warnings?.length && <p className="outlook-stale">در کندل‌های قدیمی ۴ساعته ناسازگاری وجود دارد؛ آن کندل‌ها مستقیم به مدل ارسال نشده‌اند و فقط تجمیع روزانهٔ بررسی‌شده استفاده شده است.</p>}
        <h3>برداشت غالب بازار</h3><p>{report.summary_fa}</p>
        <div className="outlook-meta"><span>زمان داده: {displayTehranTime(null, run.context?.as_of)}</span>
          <span>قیمت هنگام تحلیل: <bdi>{run.context?.live_price ? price(run.context.live_price) : "—"}</bdi></span></div>
        <HistoryChart bars={run.context?.daily_candles || []} />
        <p className="hint">نمودار تاریخی قیمت بسته‌شدن روزانه؛ مسیر آینده در سناریوهای مشروط زیر توضیح داده شده است.</p>
      </div>
      <div className="outlook-grid"><article className="card"><h3>ساختار هفتگی</h3><p>{report.weekly_context_fa}</p></article>
        <article className="card"><h3>ساختار روزانه</h3><p>{report.daily_structure_fa}</p></article></div>
      <div className="outlook-grid"><ScenarioCard primary scenario={report.dominant} levels={levels} />
        <ScenarioCard primary={false} scenario={report.alternative} levels={levels} /></div>
      <div className="outlook-grid"><article className="card"><h3>چه چیزهایی را پیگیری کنیم؟</h3><ul>{report.watch_next_fa.map((s, i) => <li key={i}>{s}</li>)}</ul></article>
        <article className="card"><h3>تغییر نسبت به گزارش قبلی</h3><p>{report.change_since_previous_fa}</p><h4>ابهام‌های تحلیل</h4><p>{report.uncertainty_fa}</p></article></div>
      <details className="card"><summary>منابع داده و مصرف گزارش</summary><p>کندل‌های Bitunix؛ روزانه و هفتگی از کندل‌های کامل ۴ساعته با مرز UTC ساخته شده‌اند. اطلاعات تکمیلی بازار، در صورت تازگی، زمینهٔ کمکی است.</p>
        <p>منبع اخبار و تقویم اقتصادی متصل نیست. این بخش سفارش معاملاتی ایجاد نمی‌کند.</p>
        <p>مدل: <bdi>{run.model || "—"}</bdi> · توکن ورودی: <bdi>{run.usage?.input_tokens ?? "—"}</bdi> · توکن خروجی: <bdi>{run.usage?.output_tokens ?? "—"}</bdi></p>
      </details>
    </>}
    {!!data?.history.length && <div className="card outlook-archive"><h3>آرشیو گزارش‌ها</h3>
      <button type="button" className="secondary" onClick={() => { selectionRequest.current++; setSelected(null); }}>آخرین گزارش آماده</button>
      <div className="outlook-archive-list">{data.history.map(r => <button type="button" className={run?.id === r.id ? "active" : "secondary"} key={r.id} disabled={r.status !== "completed" || busy} onClick={async () => {
        const request = ++selectionRequest.current;
        try { const detail = await apiFetch(`/api/gui/daily-outlook/runs/${r.id}`); if (selectionRequest.current === request) setSelected(detail); }
        catch (e) { setError(e instanceof Error ? e.message : "خطا در دریافت گزارش"); }
      }}>{r.report_date} · {statusLabel[r.status] || r.status}</button>)}</div>
    </div>}
  </section>;
}

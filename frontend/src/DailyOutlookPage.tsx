import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import "./dailyOutlook.css";

type Level = { id: string; label_fa: string; price: number };
type Scenario = {
  title_fa: string; direction: "bullish" | "bearish" | "range";
  thesis_fa: string; activation_fa: string; invalidation_fa: string;
  activation_level_ids: string[]; invalidation_level_ids: string[]; target_level_ids: string[];
  path_fa: string[]; path_level_ids?: string[];
};
type Report = {
  summary_fa: string; weekly_context_fa: string; daily_structure_fa: string;
  dominant: Scenario; alternative: Scenario; uncertainty_fa: string;
  watch_next_fa: string[]; change_since_previous_fa: string;
};
type Run = {
  title?: string; summary?: string; direction?: keyof typeof dir; snapshot_price?: number;
  id: number; report_date: string; symbol: string; status: string; stale: boolean;
  started_at: string; completed_at: string | null; model: string | null; error_code?: string;
  report?: Report; usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number };
  context?: { as_of: string; source: string; live_price: number; daily_atr14: number;
    levels: Level[]; daily_candles: number[][]; four_hour_candles?: number[][]; data_quality_warnings?: unknown[] };
};
type Schedule = { enabled: boolean; hour: number; minute: number; next_run_at: string | null };
type Dashboard = { settings: Schedule; latest: Run | null; latest_completed: Run | null;
  history: Run[]; next_cursor?: number | null; attempted_today: boolean; can_retry_today?: boolean };
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

function reportRoute() { return window.location.hash.match(/^#\/outlook\/reports\/(\d+)$/)?.[1] || null; }

function ReportImage({ id }: { id: number }) {
  const [image, setImage] = useState<{ data_url: string; filename: string;
    nearby?: { support: (Level & { code: string })[]; resistance: (Level & { code: string })[] };
    channel?: { available: boolean; direction?: keyof typeof dir; upper?: number; lower?: number; timeframe?: string; historical?: boolean; end_time_ms?: number; position?: "above" | "below" | "inside" };
  } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setImage(null); setError("");
    apiFetch(`/api/gui/daily-outlook/runs/${id}/chart-image`)
      .then(result => { if (!cancelled) setImage(result); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [id, retry]);
  return <figure className="card outlook-report-image">
    {image ? <><img src={image.data_url} alt="چارت کندل‌های واقعی و مسیر سناریوهای هفت روز آینده" onError={() => { setImage(null); setError("نمایش تصویر ممکن نشد"); }} />
      <a href={image.data_url} download={image.filename}>دانلود تصویر چارت</a></>
      : error ? <div role="alert">{error} <button onClick={() => setRetry(v => v + 1)}>دریافت دوباره تصویر</button></div>
      : <p role="status">در حال آماده‌سازی تصویر چارت…</p>}
    {image?.nearby && <div className="outlook-grid outlook-overlay-key">
      {(["support", "resistance"] as const).map(side => <div key={side}><h4>{side === "support" ? "حمایت‌های احتمالی" : "مقاومت‌های احتمالی"}</h4>
        {image.nearby![side].length ? image.nearby![side].map(l => <p key={l.id}><bdi>{l.code} · {price(l.price)}</bdi> — {l.label_fa}</p>) : <p>سطحی در داده‌های این گزارش موجود نیست.</p>}</div>)}
    </div>}
    {image?.channel && <p className="hint">{image.channel.available
      ? `کانال ${image.channel.historical ? "تاریخی" : "اخیر"} ${dir[image.channel.direction!]} در تایم‌فریم ${image.channel.timeframe}: کف در زمان گزارش ${price(image.channel.lower!)} و سقف ${price(image.channel.upper!)}. قیمت زمان گزارش ${image.channel.position === "above" ? "بالاتر از سقف کانال" : image.channel.position === "below" ? "پایین‌تر از کف کانال" : "داخل کانال"} است. بخش پیوسته، کانال تاریخی و بخش خط‌چین، امتداد هندسی آن است؛ کانال شکسته‌شده نیز برای مقایسه حفظ می‌شود.`
      : "کانال قابل اتکایی با معیارهای ترسیم در داده‌های این گزارش پیدا نشد؛ کانالی رسم نشده است."}</p>}
    <figcaption>حمایت و مقاومت بر پایهٔ سطوح تاریخی و نسبت به قیمت زمان گزارش دسته‌بندی شده‌اند؛ واکنش قیمت به آن‌ها تضمین‌شده نیست. کندل‌ها واقعی و مربوط به زمان گزارش‌اند. خط سبز: سناریوی غالب؛ خط زرد: جایگزین. مسیر هفت‌روزه مشروط است و فاصلهٔ نقاط، زمان قطعی رسیدن قیمت را نشان نمی‌دهد.</figcaption>
  </figure>;
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
  const [reportId, setReportId] = useState(reportRoute);
  const [detailError, setDetailError] = useState("");
  const [older, setOlder] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<number | null | undefined>(undefined);
  useEffect(() => {
    const update = () => setReportId(reportRoute());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    let cancelled = false;
    setSelected(null); setDetailError("");
    if (reportId) apiFetch(`/api/gui/daily-outlook/runs/${reportId}`)
      .then(r => { if (!cancelled) setSelected(r); })
      .catch(e => { if (!cancelled) setDetailError(e.message); });
    return () => { cancelled = true; };
  }, [reportId]);
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
  const run = reportId && selected?.id === Number(reportId) ? selected : null;
  const history = [...new Map([...older, ...(data?.history || [])].map(r => [r.id, r])).values()].sort((a,b) => b.id-a.id);
  const nextCursor = cursor === undefined ? data?.next_cursor : cursor;
  const report = run?.report;
  const levels = run?.context?.levels || [];

  return <section className="daily-outlook" dir="rtl">
    {!reportId && <><header className="outlook-heading"><div><span className="outlook-eyebrow">تصویر بزرگ‌تر بازار</span>
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
        setNotice("تولید گزارش شروع شد؛ نتیجه پس از تکمیل نمایش داده می‌شود.");
      })}>{data?.can_retry_today ? "تلاش مجدد گزارش امروز" : "تولید گزارش امروز"}</button>
      <p className="hint outlook-control-note">حداکثر یک درخواست AI در روز برای هر نماد؛ خطای داده پیش از ارسال به AI قابل تلاش مجدد دستی است. با اتصال API مصرف جداگانه دارد.</p>
      <p className="hint outlook-control-note">{data?.settings.enabled ? `نوبت بعد: ${displayTehranTime(null, data.settings.next_run_at)}` : "زمان‌بندی خودکار خاموش است."}</p>
    </div>
    </>}
    {error && <p role="alert" className="toast toast-error">{error}</p>}
    {notice && <p role="status" className="toast">{notice}</p>}
    {!reportId && data?.latest?.status === "running" && <p role="status" className="card outlook-status">گزارش امروز در حال تولید است؛ ممکن است چند دقیقه زمان ببرد.</p>}
    {!reportId && data?.latest?.status === "failed" && <div role="alert" className="card outlook-status">تولید گزارش {data.latest.report_date} ناموفق بود. گزارش‌های قبلی در تایم‌لاین در دسترس‌اند.
      <p className="hint">کد خطا: <bdi>{data.latest.error_code}</bdi></p></div>}
    {!data && !error && <p>در حال دریافت اطلاعات…</p>}
    {!reportId && data && !history.length && <div className="card outlook-empty"><h3>اولین چشم‌انداز را بسازید</h3><p>گزارش شامل سناریوی غالب و جایگزین، نواحی کلیدی، شروط ابطال و موارد قابل پیگیری خواهد بود.</p></div>}
    {reportId && <a className="outlook-back" href="#/outlook">← بازگشت به گزارش‌های روزانه</a>}
    {reportId && detailError && <p role="alert">{detailError}</p>}
    {reportId && !run && !detailError && <p role="status">در حال دریافت گزارش…</p>}
    {run && !report && <p>گزارش هنوز آماده نیست: {statusLabel[run.status] || run.status}</p>}
    {report && run && <>
      <header className="outlook-heading"><div><span className="outlook-eyebrow">گزارش روزانه · <bdi>{run.report_date}</bdi></span>
        <h2>{report.dominant.title_fa}</h2><p><bdi>{run.symbol}</bdi> · {displayTehranTime(null, run.completed_at)}</p></div></header>
      <ReportImage key={run.id} id={run.id} />
      <div className="card outlook-summary">
        <div className="outlook-card-top"><span><bdi>{run.symbol}</bdi> · {run.report_date}</span><span className={`outlook-badge ${report.dominant.direction}`}>{dir[report.dominant.direction]}</span></div>
        {run.stale && <p className="outlook-stale">این گزارش مربوط به روز گذشته یا قبل‌تر است؛ به‌عنوان تحلیل تازه استفاده نشود.</p>}
        {!!run.context?.data_quality_warnings?.length && <p className="outlook-stale">در کندل‌های قدیمی ۴ساعته ناسازگاری وجود دارد؛ آن کندل‌ها مستقیم به مدل ارسال نشده‌اند و فقط تجمیع روزانهٔ بررسی‌شده استفاده شده است.</p>}
        <h3>برداشت غالب بازار</h3><p>{report.summary_fa}</p>
        <div className="outlook-meta"><span>زمان داده: {displayTehranTime(null, run.context?.as_of)}</span>
          <span>قیمت هنگام تحلیل: <bdi>{run.context?.live_price ? price(run.context.live_price) : "—"}</bdi></span></div>
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
      <article className="card outlook-bottom-summary" aria-label="خلاصه نهایی سناریو">
        <h3>خلاصهٔ سناریوی این گزارش</h3>
        <p><strong>سناریوی غالب: {dir[report.dominant.direction]}.</strong> {report.dominant.thesis_fa}</p>
        <p><strong>شرط فعال‌شدن:</strong> {report.dominant.activation_fa}</p>
        <p><strong>مسیر مورد انتظار:</strong> {report.dominant.path_fa.join(" ← ")}</p>
        <h4>اهداف مشروط</h4><LevelList ids={report.dominant.target_level_ids} levels={levels} />
        <p><strong>ابطال سناریو:</strong> {report.dominant.invalidation_fa}</p>
        <LevelList ids={report.dominant.invalidation_level_ids} levels={levels} />
        <p><strong>سناریوی جایگزین: {dir[report.alternative.direction]}.</strong> {report.alternative.activation_fa}</p>
        <p className="hint">این جمع‌بندی مربوط به {run.report_date} است؛ فعال‌شدن شروط با دادهٔ زنده بررسی نشده است.</p>
      </article>
    </>}
    {!reportId && !!history.length && <div className="outlook-timeline" aria-label="تایم‌لاین گزارش‌های روزانه">
      {history.map(r => <article className="outlook-timeline-entry" key={r.id}>
        <div className="outlook-timeline-date"><time>{r.report_date}</time><span>{displayTehranTime(null, r.completed_at || r.started_at)}</span></div>
        <div className="card outlook-post">
          <div className="outlook-card-top"><span><bdi>{r.symbol}</bdi> · {statusLabel[r.status] || r.status}</span>
            {r.direction && <span className={`outlook-badge ${r.direction}`}>{dir[r.direction]}</span>}</div>
          <h3>{r.status === "completed" ? <a href={`#/outlook/reports/${r.id}`}>{r.title || "چشم‌انداز روزانه بیت‌کوین"}</a> : "گزارش روزانه"}</h3>
          <p>{r.summary || (r.status === "running" ? "گزارش در حال آماده‌سازی است…" : r.error_code || "")}</p>
          <div className="outlook-card-top">{r.snapshot_price && <span className="hint">قیمت هنگام گزارش: <bdi>{price(r.snapshot_price)}</bdi></span>}
            {r.status === "completed" && <a href={`#/outlook/reports/${r.id}`}>مشاهده گزارش و چارت ←</a>}</div>
        </div>
      </article>)}
      {nextCursor && <button disabled={busy} onClick={() => action(async () => {
        const result = await apiFetch(`/api/gui/daily-outlook/runs?before_id=${nextCursor}`);
        setOlder(v => [...v, ...result.items]); setCursor(result.next_cursor);
      })}>گزارش‌های قدیمی‌تر</button>}
    </div>}
  </section>;
}

import { IndicatorContext } from "./IndicatorContext";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import "./dailyOutlook.css";

type Level = { id: string; label_fa: string; price: number };
type Scenario = {
  title_fa: string; direction: "bullish" | "bearish" | "range";
  thesis_fa: string; activation_fa: string; invalidation_fa: string;
  activation_level_ids: string[]; invalidation_level_ids: string[]; target_level_ids: string[];
  path_fa: string[]; path_level_ids?: string[]; retest_level_ids?: string[]; target_unavailable_reason_fa?: string | null;
};
type TradePlan = {
  source?: string; setup_id?: string;
  action: "long" | "short" | "wait"; rationale_fa: string; trigger_fa: string;
  entry_price?: number; stop_loss?: number; take_profits?: number[]; risk_reward?: number[];
};
const evidenceNames: Record<string,string> = {funding:"Funding Rate",open_interest:"Open Interest",trade_flow:"Trade Flow",order_book:"Order Book",liquidations:"Liquidations"};
const evidenceEffects: Record<string,string> = {supports:"همسو با سناریوی غالب",opposes:"مخالف سناریوی غالب",neutral:"خنثی",not_assessed:"توضیح داده نشده"};
const missingReasons: Record<string,string> = {not_recorded:"داده ثبت نشده",stale_or_future:"زمان داده خارج از محدوده مجاز",collector_disabled:"جمع‌آوری غیرفعال",collector_unavailable:"منبع در دسترس نبود",collector_stale:"دادهٔ جمع‌آوری‌شده قدیمی بود",collector_error:"خطای جمع‌آوری",invalid_source_or_quality:"منبع یا کیفیت داده پذیرفته نشد",micro_not_recorded:"دادهٔ لحظه‌ای ثبت نشده",micro_source_mismatch:"منبع دادهٔ لحظه‌ای نامعتبر",micro_stale_or_future:"زمان دادهٔ لحظه‌ای خارج از محدوده مجاز",micro_invalid_values:"مقادیر دادهٔ لحظه‌ای نامعتبر",snapshot_gate_prevented_micro_read:"به‌علت نامعتبر بودن دادهٔ پایه بررسی نشد",snapshot_invalid_or_missing:"دادهٔ پایه نامعتبر یا ثبت‌نشده",snapshot_stale_or_future:"دادهٔ پایه قدیمی یا دارای زمان نامعتبر",snapshot_symbol_mismatch:"نماد دادهٔ پایه نامعتبر",no_public_snapshot:"دادهٔ پایه ثبت نشده",snapshot_read_failed:"خواندن داده ناموفق",context_size_limit:"حجم داده بیش از حد مجاز"};
type SwingPlan = TradePlan & {
  structural_reason_fa?: string; holding_days?: number; entry_expires_at?: string;
  funding_included?: boolean; validation_reason?: string;
};
type Report = {
  analysis_mode?: string;
  trend_assessment?: { phase: string; phase_fa: string; reversal_watch_fa: string; expected_path_fa: string; horizon_fa: string };
  previous_plan_review?: { prior_report_id: number; assessment_fa: string; late_entry_fa: string };
  swing_plan?: SwingPlan | null;
  analyst_levels?: (Level & {reason_fa: string; timeframe: string})[];
  supplemental_evidence?: {source:string;effect:string;explanation_fa:string}[];
  trade_plan?: TradePlan;
  summary_fa: string; weekly_context_fa: string; daily_structure_fa: string;
  dominant: Scenario; alternative: Scenario; uncertainty_fa: string;
  watch_next_fa: string[]; change_since_previous_fa: string;
};
type Run = {
  title?: string; summary?: string; direction?: keyof typeof dir; snapshot_price?: number;
  slot_close_ms?: number; id: number; report_date: string; symbol: string; status: string; stale: boolean;
  started_at: string; completed_at: string | null; model: string | null; error_code?: string;
  validation_errors?: { field: string; type: string }[];
  report?: Report; usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number };
  context?: { strategy_features?: unknown; market_intelligence?: {status?:string;reason?:string;missing_sources?:Record<string,string>}; telegram_delivery?: { status: string }; as_of: string; source: string; live_price: number; daily_atr14: number;
    levels: Level[]; daily_candles: number[][]; four_hour_candles?: number[][]; data_quality_warnings?: unknown[] };
};
type Schedule = { cadence_hours?: number; enabled: boolean; hour: number; minute: number; next_run_at: string | null };
type Dashboard = { settings: Schedule; latest: Run | null; latest_completed: Run | null;
  can_generate?: boolean; history: Run[]; next_cursor?: number | null; attempted_today: boolean; can_retry_today?: boolean };
const dir = { bullish: "صعودی", bearish: "نزولی", range: "رنج" };
const reportError = (run: Run) => ({
  ai_request_failed: "درخواست تحلیل از API ناموفق بود؛ جزئیات ثبت‌شده را بررسی کنید.",
  outlook_response_schema_invalid: "پاسخ API با قالب گزارش سازگار نیست.",
  outlook_response_json_invalid: "پاسخ API به شکل JSON معتبر دریافت نشده است.",
  outlook_response_evidence_invalid: "سطوح یا مسیر سناریو با شواهد گزارش سازگار نیست.",
  outlook_generation_failed: "تولید گزارش ناموفق بود؛ جزئیات این خطای قدیمی ثبت نشده است.",
} as Record<string,string>)[run.error_code || ""] || run.error_code || "";
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
    {!!s.retest_level_ids?.length && <><h4>نواحی بازآزمایی؛ نه هدف</h4><LevelList ids={s.retest_level_ids} levels={levels} /></>}
    <h4>نواحی هدف احتمالی</h4>{s.target_level_ids.length ? <LevelList ids={s.target_level_ids} levels={levels} /> : <p>{s.target_unavailable_reason_fa || "هدف معتبر مشخص نشده است."}</p>}
    <div className="outlook-invalidation"><h4>چه چیزی سناریو را باطل می‌کند؟</h4><p>{s.invalidation_fa}</p>
      <LevelList ids={s.invalidation_level_ids} levels={levels} /></div>
  </article>;
}

function TradePlanCard({ plan, date }: { plan?: TradePlan; date: string }) {
  return <article className="card outlook-bottom-summary" aria-label="پلن معاملاتی">
    <h3>پلن معاملاتی</h3>
    {plan && plan.source !== "ict_engine" ? <p>انتظار (WAIT) · پلن آرشیوی این گزارش تأیید ستاپ برگشتی و نقطهٔ ابطال معتبر از موتور A ندارد. برای بررسی وضعیت فعلی، تب فرصت‌ها را ببینید.</p> : !plan ? <p>این گزارش قدیمی پلن معاملاتی ساختاریافته ندارد؛ پلن در گزارش‌های جدید نمایش داده می‌شود.</p> : <>
      <p><strong>{plan.action === "long" ? "لانگ مشروط" : plan.action === "short" ? "شورت مشروط" : "انتظار؛ فعلاً بدون معامله"}</strong> — {plan.rationale_fa}</p>
      <p><strong>{plan.action === "wait" ? "شرط بررسی مجدد:" : "شرط ورود:"}</strong> {plan.trigger_fa}</p>
      {plan.action !== "wait" && <div className="outlook-level-chips">
        <span>ورود (Entry): <bdi>{plan.entry_price != null ? price(plan.entry_price) : "—"}</bdi></span>
        <span>حد ضرر (SL): <bdi>{plan.stop_loss != null ? price(plan.stop_loss) : "—"}</bdi></span>
        {plan.take_profits?.map((target, i) => <span key={i}>حد سود <bdi>TP{i + 1}: {price(target)}</bdi> · سود به زیان <bdi>1:{plan.risk_reward?.[i]?.toFixed(2) ?? "—"}</bdi></span>)}
      </div>}
    </>}
    <p className="hint">تصویر زمان گزارش {date} · هدف بر مبنای ریسک خالص و هزینه‌های مدل اجرای Paper است. وضعیت فعلی در تب فرصت‌ها نمایش داده می‌شود.</p>
  </article>;
}

function reportRoute() { return window.location.hash.match(/^#\/(?:outlook|analysis)\/reports\/(\d+)$/)?.[1] || null; }

function ReportImage({ id }: { id: number }) {
  const [image, setImage] = useState<{ data_url: string; filename: string;
    nearby?: { support: (Level & { code: string })[]; resistance: (Level & { code: string })[] };
    channel?: { available: boolean; direction?: keyof typeof dir; upper?: number; lower?: number; timeframe?: string; historical?: boolean; start_time_ms?: number; end_time_ms?: number; position?: "above" | "below" | "inside" };
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
      : <p role="status">در حال بارگذاری تصویر چارت…</p>}
    {image?.nearby && <div className="outlook-grid outlook-overlay-key">
      {(["support", "resistance"] as const).map(side => <div key={side}><h4>{side === "support" ? "حمایت‌های احتمالی" : "مقاومت‌های احتمالی"}</h4>
        {image.nearby![side].length ? image.nearby![side].map(l => <p key={l.id}><bdi>{l.code} · {price(l.price)}</bdi> — {l.label_fa}</p>) : <p>سطحی در داده‌های این گزارش موجود نیست.</p>}</div>)}
    </div>}
    {image?.channel && <p className="hint">{image.channel.available
      ? `کانال ${image.channel.historical ? "تاریخی" : "اخیر"} ${dir[image.channel.direction!]} در تایم‌فریم ${image.channel.timeframe}: کف در زمان گزارش ${price(image.channel.lower!)} و سقف ${price(image.channel.upper!)}. قیمت زمان گزارش ${image.channel.position === "above" ? "بالاتر از سقف کانال" : image.channel.position === "below" ? "پایین‌تر از کف کانال" : "داخل کانال"} است. بخش پیوسته، کانال تاریخی و بخش خط‌چین، امتداد هندسی آن است؛ کانال شکسته‌شده نیز برای مقایسه حفظ می‌شود.`
      : "کانال قابل اتکایی با معیارهای ترسیم در داده‌های این گزارش پیدا نشد؛ کانالی رسم نشده است."}</p>}
    {image?.channel?.available && image.channel.start_time_ms && image.channel.end_time_ms &&
      <p className="hint">بازهٔ مبنای کانال: {displayTehranTime(null, new Date(image.channel.start_time_ms).toISOString())} تا {displayTehranTime(null, new Date(image.channel.end_time_ms).toISOString())}.
        دایره‌های آبی روی چارت، نقاط مبنای شیب کانال را نشان می‌دهند.</p>}
    <figcaption>حمایت و مقاومت بر پایهٔ سطوح تاریخی و نسبت به قیمت زمان گزارش دسته‌بندی شده‌اند؛ واکنش قیمت به آن‌ها تضمین‌شده نیست. کندل‌ها واقعی و مربوط به زمان گزارش‌اند. خط سبز: سناریوی غالب؛ خط زرد: جایگزین. مسیر هفت‌روزه مشروط است و فاصلهٔ نقاط، زمان قطعی رسیدن قیمت را نشان نمی‌دهد.</figcaption>
  </figure>;
}

export function DailyOutlookPage({ cadence = "daily" }: { cadence?: "daily" | "4h" }) {
  const root = cadence === "4h" ? "analysis" : "outlook";
  const query = cadence === "4h" ? "?cadence=4h" : "";
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
    const result: Dashboard = await apiFetch(`/api/gui/daily-outlook${query}`);
    setData(result);
    if (!initialized.current) {
      setEnabled(result.settings.enabled);
      setTime(`${String(result.settings.hour).padStart(2, "0")}:${String(result.settings.minute).padStart(2, "0")}`);
      initialized.current = true;
    }
  }, [query]);
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
  const fourHourly = data?.settings.cadence_hours === 4;

  return <section className="daily-outlook" dir="rtl">
    {!reportId && <><header className="outlook-heading"><div><span className="outlook-eyebrow">تصویر بزرگ‌تر بازار</span>
      <h2>{fourHourly ? "تحلیل چهار‌ساعته" : "چشم‌انداز روزانه"}</h2><p>سناریوهای مشروط ۳ تا ۷ روز آینده، بر پایهٔ ساختار هفتگی، روزانه و ۴ساعته</p></div>
      <span className="outlook-horizon">۳–۷ روز</span></header>
    <div className="card outlook-controls">
      <label className="outlook-toggle"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />{fourHourly ? "تحلیل خودکار چهار‌ساعته" : "تحلیل خودکار روزانه"}</label>
      {fourHourly ? <p>پنج دقیقه پس از بسته‌شدن کندل ۴ساعته؛ ساعت تهران: ۰۳:۳۵، ۰۷:۳۵، ۱۱:۳۵، ۱۵:۳۵، ۱۹:۳۵، ۲۳:۳۵</p> : <label>ساعت تهران <input aria-label="ساعت تهران" type="time" value={time} onChange={e => setTime(e.target.value)} /></label>}
      <button type="button" disabled={busy || !data || !/^\d{2}:\d{2}$/.test(time)} onClick={() => action(async () => {
        const [hour, minute] = time.split(":").map(Number);
        await apiFetch(`/api/gui/daily-outlook/settings${query}`, { method: "PUT", body: JSON.stringify({ enabled, hour, minute }) });
        setNotice("زمان‌بندی ذخیره شد.");
      })}>ذخیره زمان‌بندی</button>
      <button type="button" className="secondary" disabled={busy || !data || data.can_generate === false || (data.attempted_today && !data.can_retry_today)} onClick={() => action(async () => {
        await apiFetch(`/api/gui/daily-outlook/generate${query}`, { method: "POST" });
        setNotice("تولید گزارش شروع شد؛ نتیجه پس از تکمیل نمایش داده می‌شود.");
      })}>{fourHourly ? (data?.can_retry_today ? "تلاش مجدد این نوبت" : "تولید گزارش این نوبت") : (data?.can_retry_today ? "تلاش مجدد گزارش امروز" : "تولید گزارش امروز")}</button>
      <p className="hint outlook-control-note">{fourHourly ? "حداکثر شش گزارش زمان‌بندی‌شده در شبانه‌روز؛ گزارش حتی بدون فرصت ورود به تلگرام ارسال می‌شود. بررسی فرصت‌های معاملاتی مستقل است." : "حداکثر یک درخواست AI در روز برای هر نماد؛ خطای داده پیش از ارسال به AI قابل تلاش مجدد دستی است."} با اتصال API مصرف جداگانه دارد.</p>
      <p className="hint outlook-control-note">{data?.settings.enabled ? `نوبت بعد: ${displayTehranTime(null, data.settings.next_run_at)}` : "زمان‌بندی خودکار خاموش است."}</p>
    </div>
    </>}
    {error && <p role="alert" className="toast toast-error">{error}</p>}
    {notice && <p role="status" className="toast">{notice}</p>}
    {!reportId && data?.latest?.status === "running" && <p role="status" className="card outlook-status">گزارش امروز در حال تولید است؛ ممکن است چند دقیقه زمان ببرد.</p>}
    {!reportId && data?.latest?.status === "failed" && <div role="alert" className="card outlook-status">تولید گزارش {data.latest.report_date} ناموفق بود. گزارش‌های قبلی در تایم‌لاین در دسترس‌اند.
      <p>{reportError(data.latest)}</p>
      {(data.latest.validation_errors || []).map((e,i) => <p className="hint" key={i}>فیلد: <bdi>{e.field}</bdi> · نوع خطا: <bdi>{e.type}</bdi></p>)}
      <p className="hint">کد خطا: <bdi>{data.latest.error_code}</bdi></p></div>}
    {!data && !error && <p>در حال دریافت اطلاعات…</p>}
    {!reportId && data && !history.length && <div className="card outlook-empty"><h3>اولین چشم‌انداز را بسازید</h3><p>گزارش شامل سناریوی غالب و جایگزین، نواحی کلیدی، شروط ابطال و موارد قابل پیگیری خواهد بود.</p></div>}
    {reportId && <a className="outlook-back" href={`#/${root}`}>← بازگشت به گزارش‌ها</a>}
    {reportId && detailError && <p role="alert">{detailError}</p>}
    {reportId && !run && !detailError && <p role="status">در حال دریافت گزارش…</p>}
    {run && !report && <p>گزارش هنوز آماده نیست: {statusLabel[run.status] || run.status}</p>}
    {report && run && <>
      <header className="outlook-heading"><div><span className="outlook-eyebrow">{run.slot_close_ms ? "گزارش چهار‌ساعته" : "گزارش روزانه"} · <bdi>{run.report_date}</bdi></span>
        <h2>{report.dominant.title_fa}</h2><p><bdi>{run.symbol}</bdi> · {displayTehranTime(null, run.completed_at)}</p></div></header>
      {run.context?.telegram_delivery && <p className="hint">وضعیت تلگرام: {({sent:"ارسال شد",partial:"ارسال ناقص متن یا چارت",disabled:"اتصال تلگرام غیرفعال است",sending:"در حال ارسال یا نتیجه نامشخص",failed_or_unknown:"ارسال ناموفق یا نتیجه نامشخص"} as Record<string,string>)[run.context.telegram_delivery.status] || run.context.telegram_delivery.status}</p>}
      <ReportImage key={run.id} id={run.id} />
      <div className="card outlook-summary">
        <div className="outlook-card-top"><span><bdi>{run.symbol}</bdi> · {run.report_date}</span><span className={`outlook-badge ${report.dominant.direction}`}>{dir[report.dominant.direction]}</span></div>
        {run.stale && <p className="outlook-stale">این گزارش مربوط به نوبت قبلی است؛ به‌عنوان تحلیل تازه استفاده نشود.</p>}
        {!!run.context?.data_quality_warnings?.length && <p className="outlook-stale">در کندل‌های قدیمی ۴ساعته ناسازگاری وجود دارد؛ آن کندل‌ها مستقیم به مدل ارسال نشده‌اند و فقط تجمیع روزانهٔ بررسی‌شده استفاده شده است.</p>}
        <h3>برداشت غالب بازار</h3><p>{report.summary_fa}</p>
        <div className="outlook-meta"><span>زمان داده: {displayTehranTime(null, run.context?.as_of)}</span>
          <span>قیمت هنگام تحلیل: <bdi>{run.context?.live_price ? price(run.context.live_price) : "—"}</bdi></span></div>
      </div>
      {report.previous_plan_review && <section className="card">
        <h3>پیگیری سناریوی قبلی</h3>
        <a href={`#/outlook/reports/${report.previous_plan_review.prior_report_id}`}>گزارش مرجع</a>
        <p>{report.previous_plan_review.assessment_fa}</p>
        <h4>اگر قبلاً وارد نشده‌اید</h4><p>{report.previous_plan_review.late_entry_fa}</p>
        <p className="hint">ورود واقعی و سود یا زیان معامله در این بخش ثبت یا تأیید نمی‌شود.</p>
      </section>}
      {report.trend_assessment && <section className="card">
        <h3>مرحلهٔ روند و مسیر پیش‌رو</h3><p>{report.trend_assessment.phase_fa}</p>
        <p>{report.trend_assessment.expected_path_fa}</p>
        <h4>ناحیهٔ حساس و احتمال تغییر مسیر</h4><p>{report.trend_assessment.reversal_watch_fa}</p>
        <p className="hint">{report.trend_assessment.horizon_fa}</p>
      </section>}
      <details className="card" open={!!report.trend_assessment}><summary>جزئیات تحلیل و سناریوها</summary>
      <IndicatorContext features={run.context?.strategy_features} />
      <div className="outlook-grid"><article className="card"><h3>ساختار هفتگی</h3><p>{report.weekly_context_fa}</p></article>
        <article className="card"><h3>ساختار روزانه</h3><p>{report.daily_structure_fa}</p></article></div>
      <div className="outlook-grid"><ScenarioCard primary scenario={report.dominant} levels={levels} />
        <ScenarioCard primary={false} scenario={report.alternative} levels={levels} /></div>
      <IndicatorContext features={run.context?.strategy_features} />
      <div className="outlook-grid"><article className="card"><h3>چه چیزهایی را پیگیری کنیم؟</h3><ul>{report.watch_next_fa.map((s, i) => <li key={i}>{s}</li>)}</ul></article>
        <article className="card"><h3>تغییر نسبت به گزارش قبلی</h3><p>{report.change_since_previous_fa}</p></article></div>
      </details>
      {(report.supplemental_evidence || run.context?.market_intelligence?.missing_sources) && <section className="card">
        <h3>شواهد تکمیلی</h3>
        <p className="hint">برداشت مدل نسبت به سناریوی غالب؛ داده‌های کوتاه‌مدت، تأیید مستقل ورود نیستند.</p>
        {(report.supplemental_evidence || []).map(item => <p key={item.source}><strong><bdi>{evidenceNames[item.source] || item.source}</bdi> — {evidenceEffects[item.effect] || item.effect}:</strong> {item.explanation_fa}</p>)}
        {Object.entries(run.context?.market_intelligence?.missing_sources || {}).map(([name,reason]) => <p key={name}><strong><bdi>{evidenceNames[name] || name}</bdi> — ارسال نشده:</strong> {reason.split(';').map(code=>missingReasons[code] || 'داده قابل استفاده نبود').join('؛ ')}</p>)}
        {!report.supplemental_evidence?.length && !Object.keys(run.context?.market_intelligence?.missing_sources || {}).length && <p>شواهد تکمیلی در این گزارش ثبت نشده است.</p>}
      </section>}
      <p className="card"><strong>ابهام‌های مهم:</strong> {report.uncertainty_fa}</p>
      <details className="card"><summary>منابع داده و مصرف گزارش</summary><p>کندل‌های Bitunix؛ روزانه و هفتگی از کندل‌های کامل ۴ساعته با مرز UTC ساخته شده‌اند. اطلاعات تکمیلی بازار، در صورت تازگی، زمینهٔ کمکی است.</p>
        <p>منبع اخبار و تقویم اقتصادی متصل نیست. این بخش سفارش معاملاتی ایجاد نمی‌کند.</p>
        <p>مدل: <bdi>{run.model || "—"}</bdi> · توکن ورودی: <bdi>{run.usage?.input_tokens ?? "—"}</bdi> · توکن خروجی: <bdi>{run.usage?.output_tokens ?? "—"}</bdi></p>
      </details>
      {report.analysis_mode === "independent_htf_v1"
        ? <SwingPlanCard plan={report.swing_plan} />
        : <TradePlanCard plan={report.trade_plan} date={run.report_date} />}
      {!!report.analyst_levels?.length && <details className="card"><summary>سطوح انتخاب‌شده توسط تحلیلگر</summary>
        {report.analyst_levels.map(level => <p key={level.id}><strong>{level.label_fa} · <bdi>{price(level.price)}</bdi> دلار:</strong> {level.reason_fa}</p>)}
      </details>}
    </>}
    {!reportId && !!history.length && <div className="outlook-timeline" aria-label="تایم‌لاین گزارش‌های روزانه">
      {history.map(r => <article className="outlook-timeline-entry" key={r.id}>
        <div className="outlook-timeline-date"><time>{r.report_date}</time><span>{displayTehranTime(null, r.completed_at || r.started_at)}</span></div>
        <div className="card outlook-post">
          <div className="outlook-card-top"><span><bdi>{r.symbol}</bdi> · {statusLabel[r.status] || r.status}</span>
            {r.direction && <span className={`outlook-badge ${r.direction}`}>{dir[r.direction]}</span>}</div>
          <h3>{r.status === "completed" ? <a href={`#/${root}/reports/${r.id}`}>{r.title || "چشم‌انداز روزانه بیت‌کوین"}</a> : "گزارش روزانه"}</h3>
          <p>{r.summary || (r.status === "running" ? "گزارش در حال آماده‌سازی است…" : reportError(r))}</p>
          <div className="outlook-card-top">{r.snapshot_price && <span className="hint">قیمت هنگام گزارش: <bdi>{price(r.snapshot_price)}</bdi></span>}
            {r.status === "completed" && <a href={`#/${root}/reports/${r.id}`}>مشاهده گزارش و چارت ←</a>}</div>
        </div>
      </article>)}
      {nextCursor && <button disabled={busy} onClick={() => action(async () => {
        const result = await apiFetch(`/api/gui/daily-outlook/runs?before_id=${nextCursor}&cadence=${cadence}`);
        setOlder(v => [...v, ...result.items]); setCursor(result.next_cursor);
      })}>گزارش‌های قدیمی‌تر</button>}
    </div>}
  </section>;
}


function SwingPlanCard({plan}: {plan?: SwingPlan | null}) {
  if (!plan || plan.action === "wait") return <section className="card"><h3>برنامهٔ ورود مشروط</h3>
    <p>{plan?.rationale_fa || "این گزارش قدیمی برنامهٔ ورود مشخصی ندارد؛ نبود برنامه به معنی نبود فرصت نیست."}</p></section>;
  const expired = !!plan.entry_expires_at && Date.parse(plan.entry_expires_at) <= Date.now();
  return <section className="card">
    <h3>برنامهٔ ورود مشروط — {plan.action === "long" ? "لانگ" : "شورت"}</h3>
    <p className="hint">صرفاً پیشنهاد تحلیلی؛ سفارش یا پوزیشن خودکار ثبت نمی‌شود.</p>
    {expired && <p className="outlook-stale">مهلت ورود این پیشنهاد گذشته است؛ ورود تازه نیاز به ارزیابی مجدد دارد.</p>}
    <p>{plan.rationale_fa}</p><p>شرط ورود: {plan.trigger_fa}</p>
    <p>ورود: <bdi>{price(plan.entry_price!)}</bdi> دلار · حدضرر: <bdi>{price(plan.stop_loss!)}</bdi> دلار</p>
    <p>اهداف: {(plan.take_profits || []).map(t => price(t)).join(" / ")} دلار</p>
    <p>{plan.structural_reason_fa}</p>
    <p>افق نگهداری از زمان ورود: حدود {plan.holding_days} روز؛ تضمین زمان رسیدن نیست.</p>
    <p>اعتبار ورود تا: {displayTehranTime(null, plan.entry_expires_at)}</p>
    <p>سود به زیان پس از هزینه‌های تخمینی: {(plan.risk_reward || []).map(r => `1:${r.toFixed(2)}`).join(" / ")}</p>
    <p className="hint">فاندینگ آینده در این برآورد لحاظ نشده است. انقضای ورود به معنی خروج پوزیشن باز نیست.</p>
  </section>;
}

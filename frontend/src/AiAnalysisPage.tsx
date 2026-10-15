import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";
import { displayTehranTime } from "./displayTime";
import type { PositionPrefill } from "./PositionPage";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  last_run_at?: string;
  last_error?: string;
  next_run_at?: string;
  last_run_at_tehran?: string;
  next_run_at_tehran?: string;
  interval_minutes?: number;
  ai_analysis_model?: string;
  ai_reasoning_effort?: string;
};

type HistoryRow = {
  id: number;
  status: string;
  decision?: string;
  display_decision?: string;
  report_preview?: string;
  created_at: string;
  created_at_tehran?: string;
  telegram_sent: boolean;
  has_chart?: boolean;
};

type ConditionalPlan = {
  id: number;
  source_run_id: number;
  symbol: string;
  direction: "LONG" | "SHORT";
  status: "watching" | "ready" | "activated" | "expired" | "invalidated";
  entry_zone_low: number;
  entry_zone_high: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  last_reason?: string;
  expires_at: string;
  linked_setup_id?: string;
  evidence?: { sweep_ms?: number; poi_ms?: number; structure_ms?: number };
};

type HistoryDetail = {
  id: number;
  report_fa?: string;
  has_chart?: boolean;
  chart_url?: string | null;
  decision?: string;
  structured?: Record<string, unknown>;
  inference?: {
    requested_model?: string;
    requested_reasoning_effort?: string;
    response_model?: string;
  };
};

function paperPrefillFromDetail(detail: HistoryDetail): PositionPrefill | null {
  const decision = (detail.decision || "").toUpperCase();
  if (decision !== "LONG" && decision !== "SHORT") return null;
  const s = detail.structured || {};
  const sl = Number(s.stop_loss);
  const tp = Number(s.take_profit);
  const entry = s.entry_price != null ? Number(s.entry_price) : undefined;
  if (!Number.isFinite(sl) || !Number.isFinite(tp)) return null;
  if (decision === "LONG" && entry != null && !(sl < entry && entry < tp)) return null;
  if (decision === "SHORT" && entry != null && !(tp < entry && entry < sl)) return null;
  return {
    direction: decision,
    entry_price: entry,
    stop_loss: sl,
    take_profit: tp,
    ai_setup_id: typeof s.setup_id === "string" ? s.setup_id : undefined,
    symbol: typeof s.symbol === "string" ? s.symbol : "BTCUSDT",
  };
}

function decisionBadge(d?: string) {
  const u = (d || "—").toUpperCase();
  if (u === "LONG") return "badge badge-long";
  if (u === "SHORT") return "badge badge-short";
  if (u === "NO_TRADE") return "badge badge-neutral";
  return "badge";
}

type Props = {
  onOpenPaperPosition?: (prefill: PositionPrefill) => void;
};

export function AiAnalysisPage({ onOpenPaperPosition }: Props) {
  const [status, setStatus] = useState<Status | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [plans, setPlans] = useState<ConditionalPlan[]>([]);
  const [msg, setMsg] = useState("");
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [chartZoom, setChartZoom] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const [s, h, p] = await Promise.all([
      apiFetch("/api/gui/chatgpt/status"),
      apiFetch("/api/gui/chatgpt/history?limit=40"),
      apiFetch("/api/gui/chatgpt/conditional-plans?limit=50"),
    ]);
    setStatus(s);
    setHistory(h);
    setPlans(p);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setMsg(String(e)));
  }, [refresh]);

  async function openDetail(id: number) {
    const d = await apiFetch(`/api/gui/chatgpt/history/${id}`);
    setDetail(d);
    setChartZoom(false);
  }

  async function analyzeNow() {
    setBusy(true);
    setMsg("");
    try {
      await apiFetch("/api/gui/chatgpt/analyze-now", { method: "POST" }, 650_000);
      setMsg("تحلیل انجام شد.");
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "خطا");
    } finally {
      setBusy(false);
    }
  }

  const connected = status?.connected && status?.plan_usage_authorized;

  return (
    <div className="page">
      <header className="page-header">
        <h1>تحلیل ICT</h1>
        <p className="hint">
          BTCUSDT · ICT Trader · gpt-6.1-sol · MAX reasoning · Paper
        </p>
      </header>

      {msg && <p className={`toast ${msg.includes("خطا") || msg.includes("409") ? "toast-error" : "toast-ok"}`}>{msg}</p>}

      <button type="button" className="btn-primary btn-block btn-lg" onClick={analyzeNow} disabled={!connected || busy}>
        {busy ? "در حال تحلیل…" : "Analyze Now"}
      </button>
      {!connected && <p className="hint center-hint">ابتدا ChatGPT را در منوی ChatGPT وصل کنید.</p>}

      {status?.last_run_at && (
        <p className="meta-line">
          آخرین اجرا: {displayTehranTime(status.last_run_at_tehran, status.last_run_at)}
          {status.next_run_at &&
            ` · بعدی (هر ${status.interval_minutes || 120} دقیقه): ${displayTehranTime(
              status.next_run_at_tehran,
              status.next_run_at,
            )}`}
        </p>
      )}
      {status?.last_error && <p className="toast toast-error">{status.last_error}</p>}

      <section className="section">
        <h2 className="section-title">سناریوهای مشروط ICT (WAIT)</h2>
        <p className="hint">این سناریوها سفارش نیستند؛ فقط پس از تأیید کندل‌های بسته‌شده و شروط ریسک، سفارش تمرینی Limit ثبت می‌شود. اجرای خودکار به فعال بودن گزینه Paper Auto نیاز دارد.</p>
        {!plans.length && <p className="hint">هنوز سناریوی مشروط معتبر ذخیره نشده است.</p>}
        <ul className="history-list">
          {plans.map((p) => (
            <li key={p.id} className="history-card">
              <div className="history-card-top">
                <span className={decisionBadge(p.direction)}>{p.direction} · {p.status.toUpperCase()}</span>
                <span className="hint">AI #{p.source_run_id}</span>
              </div>
              <p className="history-preview">ورود: {p.entry_price.toLocaleString()} · ناحیه: {p.entry_zone_low.toLocaleString()}–{p.entry_zone_high.toLocaleString()}</p>
              <p className="history-preview">SL: {p.stop_loss.toLocaleString()} · TP: {p.take_profit.toLocaleString()}</p>
              <p className="hint">وضعیت پایش: {p.last_reason || "—"} · Sweep: {p.evidence?.sweep_ms ? "✓" : "—"} · POI: {p.evidence?.poi_ms ? "✓" : "—"} · Structure: {p.evidence?.structure_ms ? "✓" : "—"}</p>
              <p className="history-time">انقضا: {displayTehranTime(undefined, p.expires_at)}{p.linked_setup_id ? " · ستاپ ثبت شده" : ""}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="section">
        <h2 className="section-title">گزارش‌های قبلی</h2>
        {!history.length && <p className="hint center-hint">هنوز گزارشی ثبت نشده.</p>}
        <ul className="history-list">
          {history.map((r) => (
            <li key={r.id}>
              <button type="button" className="history-card" onClick={() => openDetail(r.id)}>
                <div className="history-card-top">
                  <span className={decisionBadge(r.decision)}>{r.display_decision || r.decision || "—"}</span>
                  <span className="history-icons">
                    {r.has_chart && <span className="chart-dot" title="نمودار">📈</span>}
                    {r.telegram_sent && <span className="tg-dot" title="ارسال به تلگرام">✓</span>}
                  </span>
                </div>
                <p className="history-preview">{r.report_preview || r.status}</p>
                <p className="history-time">{displayTehranTime(r.created_at_tehran, r.created_at)}</p>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {detail && (
        <div className="sheet-backdrop" role="presentation" onClick={() => setDetail(null)}>
          <div className="sheet" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            <div className="sheet-head">
              <h3>گزارش {detail.decision || ""}</h3>
              <button type="button" className="ghost-btn" onClick={() => setDetail(null)}>
                بستن
              </button>
            </div>
            <div className="sheet-body">
              {detail.inference && (
                <p className="hint meta-line">
                  {detail.inference.requested_model || "gpt-6.1-sol"} · reasoning{" "}
                  {(detail.inference.requested_reasoning_effort || "max").toUpperCase()}
                  {detail.inference.confirmed_reasoning_effort
                    ? ` (confirmed: ${detail.inference.confirmed_reasoning_effort.toUpperCase()})`
                    : ""}
                </p>
              )}
              {detail.has_chart && detail.chart_url ? (
                <div className="chart-preview-wrap">
                  <button type="button" className="chart-preview-btn" onClick={() => setChartZoom(true)}>
                    <img
                      src={detail.chart_url}
                      alt="ICT chart"
                      className="chart-preview-img"
                      loading="lazy"
                    />
                  </button>
                  <p className="hint">برای بزرگ‌نمایی روی نمودار بزنید</p>
                </div>
              ) : (
                <p className="hint">نمودار برای این گزارش موجود نیست.</p>
              )}
              {onOpenPaperPosition && paperPrefillFromDetail(detail) && (
                <button
                  type="button"
                  className="btn-primary btn-block"
                  onClick={() => {
                    const p = paperPrefillFromDetail(detail);
                    if (p) onOpenPaperPosition(p);
                  }}
                >
                  ایجاد پوزیشن تمرینی
                </button>
              )}
              <pre>{detail.report_fa || ""}</pre>
            </div>
          </div>
        </div>
      )}

      {chartZoom && detail?.chart_url && (
        <div className="chart-lightbox" role="dialog" aria-modal="true" onClick={() => setChartZoom(false)}>
          <img src={detail.chart_url} alt="ICT chart full" className="chart-lightbox-img" />
          <button type="button" className="ghost-btn chart-lightbox-close" onClick={() => setChartZoom(false)}>
            بستن
          </button>
        </div>
      )}
    </div>
  );
}

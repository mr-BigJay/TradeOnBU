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
  raw_decision?: string;
  final_ai_decision?: string;
  display_decision?: string;
  setup_mode?: string;
  report_preview?: string;
  created_at: string;
  created_at_tehran?: string;
  telegram_sent: boolean;
  has_chart?: boolean;
};

type HistoryDetail = {
  id: number;
  report_fa?: string;
  has_chart?: boolean;
  chart_url?: string | null;
  decision?: string;
  raw_decision?: string;
  final_ai_decision?: string;
  display_decision?: string;
  setup_mode?: string;
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
  const [msg, setMsg] = useState("");
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [chartZoom, setChartZoom] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const [s, h] = await Promise.all([
      apiFetch("/api/gui/chatgpt/status"),
      apiFetch("/api/gui/chatgpt/history?limit=40"),
    ]);
    setStatus(s);
    setHistory(h);
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
            ` · بعدی (هر ${status.interval_minutes || 60} دقیقه): ${displayTehranTime(
              status.next_run_at_tehran,
              status.next_run_at,
            )}`}
        </p>
      )}
      {status?.last_error && <p className="toast toast-error">{status.last_error}</p>}

      <section className="section">
        <h2 className="section-title">گزارش‌های قبلی</h2>
        {!history.length && <p className="hint center-hint">هنوز گزارشی ثبت نشده.</p>}
        <ul className="history-list">
          {history.map((r) => (
            <li key={r.id}>
              <button type="button" className="history-card" onClick={() => openDetail(r.id)}>
                <div className="history-card-top">
                  <span className={decisionBadge(r.display_decision || r.final_ai_decision || r.decision)}>
                    {r.display_decision || r.final_ai_decision || r.decision || "—"}
                  </span>
                  {r.raw_decision && r.raw_decision !== (r.display_decision || r.final_ai_decision) && (
                    <span className="hint"> (raw: {r.raw_decision})</span>
                  )}
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
              <h3>گزارش {detail.display_decision || detail.final_ai_decision || detail.decision || ""}</h3>
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

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  last_run_at?: string;
  last_error?: string;
};

type HistoryRow = {
  id: number;
  status: string;
  decision?: string;
  report_preview?: string;
  created_at: string;
  telegram_sent: boolean;
};

function decisionBadge(d?: string) {
  const u = (d || "—").toUpperCase();
  if (u === "LONG") return "badge badge-long";
  if (u === "SHORT") return "badge badge-short";
  if (u === "NO_TRADE") return "badge badge-neutral";
  return "badge";
}

export function AiAnalysisPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [msg, setMsg] = useState("");
  const [detail, setDetail] = useState("");
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

  async function analyzeNow() {
    setBusy(true);
    setMsg("");
    try {
      await apiFetch("/api/gui/chatgpt/analyze-now", { method: "POST" }, 130_000);
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
        <p className="hint">BTCUSDT · فارسی · Paper</p>
      </header>

      {msg && <p className={`toast ${msg.includes("خطا") || msg.includes("409") ? "toast-error" : "toast-ok"}`}>{msg}</p>}

      <button type="button" className="btn-primary btn-block btn-lg" onClick={analyzeNow} disabled={!connected || busy}>
        {busy ? "در حال تحلیل…" : "Analyze Now"}
      </button>
      {!connected && <p className="hint center-hint">ابتدا ChatGPT را در منوی ChatGPT وصل کنید.</p>}

      {status?.last_run_at && (
        <p className="meta-line">
          آخرین اجرا: {new Date(status.last_run_at).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" })}
        </p>
      )}
      {status?.last_error && <p className="toast toast-error">{status.last_error}</p>}

      <section className="section">
        <h2 className="section-title">گزارش‌های قبلی</h2>
        {!history.length && <p className="hint center-hint">هنوز گزارشی ثبت نشده.</p>}
        <ul className="history-list">
          {history.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                className="history-card"
                onClick={() => apiFetch(`/api/gui/chatgpt/history/${r.id}`).then((d) => setDetail(d.report_fa || ""))}
              >
                <div className="history-card-top">
                  <span className={decisionBadge(r.decision)}>{r.decision || "—"}</span>
                  {r.telegram_sent && <span className="tg-dot" title="ارسال به تلگرام">✓</span>}
                </div>
                <p className="history-preview">{r.report_preview || r.status}</p>
                <p className="history-time">
                  {new Date(r.created_at).toLocaleString("fa-IR", { dateStyle: "medium", timeStyle: "short" })}
                </p>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {detail && (
        <div className="sheet-backdrop" role="presentation" onClick={() => setDetail("")}>
          <div className="sheet" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-handle" />
            <div className="sheet-head">
              <h3>گزارش</h3>
              <button type="button" className="ghost-btn" onClick={() => setDetail("")}>
                بستن
              </button>
            </div>
            <div className="sheet-body">
              <pre>{detail}</pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

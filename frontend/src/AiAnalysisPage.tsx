import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  last_run_at?: string;
  next_run_at?: string;
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
    setMsg("در حال تحلیل…");
    try {
      await apiFetch("/api/gui/chatgpt/analyze-now", { method: "POST" }, 130_000);
      setMsg("انجام شد.");
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
      <h2>تحلیل و گزارش‌ها</h2>
      <p className="hint">BTCUSDT — ICT — Persian — Paper only</p>
      {msg && <p className="hint">{msg}</p>}

      <div className="card row-btns">
        <button type="button" onClick={analyzeNow} disabled={!connected || busy}>
          {busy ? "…" : "Analyze Now"}
        </button>
        <button type="button" className="secondary" onClick={() => refresh()}>
          به‌روزرسانی
        </button>
      </div>

      {status && (
        <p className="hint">
          آخرین: {status.last_run_at ? new Date(status.last_run_at).toLocaleString("fa-IR") : "—"}
          {status.last_error && <span className="stale"> — {status.last_error}</span>}
        </p>
      )}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>زمان</th>
              <th>وضعیت</th>
              <th>تصمیم</th>
              <th>تلگرام</th>
            </tr>
          </thead>
          <tbody>
            {history.map((r) => (
              <tr key={r.id} className="click-row" onClick={() => apiFetch(`/api/gui/chatgpt/history/${r.id}`).then((d) => setDetail(d.report_fa || ""))}>
                <td>{new Date(r.created_at).toLocaleString("fa-IR")}</td>
                <td>{r.status}</td>
                <td>{r.decision || "—"}</td>
                <td>{r.telegram_sent ? "✓" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!history.length && <p className="hint">هنوز گزارشی نیست.</p>}
      </div>

      {detail && (
        <div className="card report-box">
          <button type="button" className="secondary close-report" onClick={() => setDetail("")}>
            بستن
          </button>
          <pre>{detail}</pre>
        </div>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  email?: string;
  hourly_enabled: boolean;
  interval_minutes: number;
  last_run_at?: string;
  last_success_at?: string;
  last_error?: string;
  preferred_model?: string;
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
  const [connectInfo, setConnectInfo] = useState<{ connect_token: string; authorize_url: string } | null>(null);
  const [detail, setDetail] = useState<string>("");

  const refresh = useCallback(async () => {
    const [s, h] = await Promise.all([
      apiFetch("/api/gui/chatgpt/status"),
      apiFetch("/api/gui/chatgpt/history?limit=30"),
    ]);
    setStatus(s);
    setHistory(h);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setMsg(String(e)));
  }, [refresh]);

  async function beginConnect() {
    const data = await apiFetch("/api/gui/chatgpt/connect/begin", { method: "POST" });
    setConnectInfo({ connect_token: data.connect_token, authorize_url: data.authorize_url });
    setMsg("اتصال ChatGPT: اسکript محلی را با CONNECT_TOKEN اجرا کنید (README).");
  }

  async function analyzeNow() {
    setMsg("در حال تحلیل…");
    try {
      await apiFetch("/api/gui/chatgpt/analyze-now", { method: "POST" }, 130_000);
      setMsg("تحلیل AI با موفقیت انجام شد.");
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "خطا");
    }
  }

  return (
    <div className="settings-wrap">
      <h2>تحلیل ICT با ChatGPT Plus (Paper)</h2>
      {msg && <p className="hint">{msg}</p>}
      {status && (
        <div className="card">
          <p>اتصال: {status.connected && status.plan_usage_authorized ? "فعال" : "غیرفعال"}</p>
          {status.email && <p>حساب: {status.email}</p>}
          <p>آخرین اجرا: {status.last_run_at || "—"}</p>
          <p>آخرین موفق: {status.last_success_at || "—"}</p>
          {status.last_error && <p className="stale">{status.last_error}</p>}
          <div className="row-btns">
            <button type="button" onClick={beginConnect}>
              Connect with ChatGPT
            </button>
            <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/chatgpt/disconnect", { method: "POST" }).then(refresh)}>
              قطع اتصال
            </button>
            <button type="button" onClick={analyzeNow} disabled={!status.connected}>
              Analyze Now
            </button>
          </div>
        </div>
      )}
      {connectInfo && (
        <div className="card mono">
          <p>CONNECT_TOKEN={connectInfo.connect_token}</p>
          <p style={{ wordBreak: "break-all" }}>{connectInfo.authorize_url}</p>
          <p className="hint">
            TRADEONBU_URL={window.location.origin} CONNECT_TOKEN=… AUTHORIZE_URL=… python3 scripts/siwc_oauth_helper.py
          </p>
        </div>
      )}
      <div className="card">
        <h3>تاریخچه تحلیل AI</h3>
        <table>
          <thead>
            <tr>
              <th>زمان</th>
              <th>وضعیت</th>
              <th>تصمیم</th>
              <th>تلگرام</th>
              <th>پیش‌نمایش</th>
            </tr>
          </thead>
          <tbody>
            {history.map((r) => (
              <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => apiFetch(`/api/gui/chatgpt/history/${r.id}`).then((d) => setDetail(d.report_fa || ""))}>
                <td>{new Date(r.created_at).toLocaleString("fa-IR")}</td>
                <td>{r.status}</td>
                <td>{r.decision || "—"}</td>
                <td>{r.telegram_sent ? "✓" : "—"}</td>
                <td>{r.report_preview}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="card">
          <h3>گزارش</h3>
          <pre style={{ whiteSpace: "pre-wrap", direction: "rtl" }}>{detail}</pre>
        </div>
      )}
    </div>
  );
}

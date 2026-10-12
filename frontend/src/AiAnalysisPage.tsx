import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  email?: string;
  hourly_enabled: boolean;
  unattended_consent: boolean;
  scheduling_active: boolean;
  scheduling_block_reason?: string;
  interval_minutes: number;
  last_run_at?: string;
  next_run_at?: string;
  last_success_at?: string;
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

type ConnectBundle = {
  connect_token: string;
  connect_secret: string;
  oauth_state: string;
  authorize_url: string;
};

export function AiAnalysisPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [msg, setMsg] = useState("");
  const [connectInfo, setConnectInfo] = useState<ConnectBundle | null>(null);
  const [detail, setDetail] = useState("");
  const [consent, setConsent] = useState(false);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [intervalMin, setIntervalMin] = useState(60);

  const refresh = useCallback(async () => {
    const [s, h] = await Promise.all([
      apiFetch("/api/gui/chatgpt/status"),
      apiFetch("/api/gui/chatgpt/history?limit=30"),
    ]);
    setStatus(s);
    setHistory(h);
    setConsent(!!s.unattended_consent);
    setScheduleEnabled(!!s.hourly_enabled);
    setIntervalMin(s.interval_minutes || 60);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setMsg(String(e)));
  }, [refresh]);

  async function beginConnect() {
    const data = await apiFetch("/api/gui/chatgpt/connect/begin", { method: "POST" });
    setConnectInfo({
      connect_token: data.connect_token,
      connect_secret: data.connect_secret,
      oauth_state: data.oauth_state,
      authorize_url: data.authorize_url,
    });
    setMsg("Connect secret shown once — copy to your Windows helper. Scheduling stays off until you enable it below.");
  }

  async function saveSchedule() {
    if (scheduleEnabled && !consent) {
      setMsg("برای زمان‌بندی خودکار باید رضایت تحلیل بدون حضور را تأیید کنید.");
      return;
    }
    await apiFetch("/api/gui/chatgpt/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: scheduleEnabled,
        unattended_consent: consent,
        interval_minutes: intervalMin,
      }),
    });
    setMsg("تنظیمات زمان‌بندی ذخیره شد.");
    await refresh();
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
          <p>زمان‌بندی خودکار: {status.scheduling_active ? "فعال" : "غیرفعال"}</p>
          {!status.scheduling_active && status.scheduling_block_reason && (
            <p className="hint">دلیل: {status.scheduling_block_reason}</p>
          )}
          <p>اجرای بعدی: {status.next_run_at || "—"}</p>
          <p>آخرین اجرا: {status.last_run_at || "—"}</p>
          {status.last_error && <p className="stale">{status.last_error}</p>}
          <div className="row-btns">
            <button type="button" onClick={beginConnect}>
              Connect with ChatGPT
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => apiFetch("/api/gui/chatgpt/disconnect", { method: "POST" }).then(refresh)}
            >
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
          <p>CONNECT_SECRET={connectInfo.connect_secret}</p>
          <p>OAUTH_STATE={connectInfo.oauth_state}</p>
          <p style={{ wordBreak: "break-all" }}>AUTHORIZE_URL={connectInfo.authorize_url}</p>
          <p className="hint">
            Windows (PowerShell): set env vars then run python scripts/siwc_oauth_helper.py — no browser cookies required.
          </p>
        </div>
      )}
      <div className="card">
        <h3>زمان‌بندی ساعتی (نیاز به رضایت صریح)</h3>
        <label>
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          اجازه می‌دهم TradeOnBU در پس‌زمینه (بدون حضور من) از طرح ChatGPT Plus برای تحلیل ICT استفاده کند.
        </label>
        <label>
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={(e) => setScheduleEnabled(e.target.checked)}
            disabled={!status?.connected}
          />
          فعال‌سازی تحلیل خودکار هر{" "}
          <input
            type="number"
            min={15}
            max={240}
            value={intervalMin}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
            style={{ width: 60 }}
          />{" "}
          دقیقه
        </label>
        <button type="button" onClick={saveSchedule}>
          ذخیره زمان‌بندی
        </button>
      </div>
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
              <tr
                key={r.id}
                style={{ cursor: "pointer" }}
                onClick={() => apiFetch(`/api/gui/chatgpt/history/${r.id}`).then((d) => setDetail(d.report_fa || ""))}
              >
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

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";
import { CopyField } from "./CopyField";

type Status = {
  connected: boolean;
  plan_usage_authorized: boolean;
  email?: string;
  scheduling_active: boolean;
  next_run_at?: string;
  interval_minutes: number;
};

type ConnectBundle = {
  connect_token: string;
  connect_secret: string;
  oauth_state: string;
  authorize_url: string;
};

export function ChatGptSettingsPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [msg, setMsg] = useState("");
  const [connectInfo, setConnectInfo] = useState<ConnectBundle | null>(null);
  const [consent, setConsent] = useState(false);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [intervalMin, setIntervalMin] = useState(60);

  const refresh = useCallback(async () => {
    const s = await apiFetch("/api/gui/chatgpt/status");
    setStatus(s);
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
    setMsg("مقادیر زیر را کپی کنید و روی ویندوز helper را اجرا کنید.");
  }

  async function saveSchedule() {
    if (scheduleEnabled && !consent) {
      setMsg("رضایت تحلیل خودکار را فعال کنید.");
      return;
    }
    await apiFetch("/api/gui/chatgpt/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: scheduleEnabled,
        unattended_consent: consent,
        interval_minutes: intervalMin,
      }),
    });
    setMsg("ذخیره شد.");
    await refresh();
  }

  const connected = status?.connected && status?.plan_usage_authorized;

  return (
    <div className="page">
      <header className="page-header">
        <h1>ChatGPT Plus</h1>
        <p className="hint">اتصال رسمی SIWC · بدون API key</p>
      </header>

      {msg && <p className="toast toast-ok">{msg}</p>}

      <div className="card status-card">
        <span className={`status-pill ${connected ? "on" : "off"}`}>{connected ? "متصل" : "قطع"}</span>
        {status?.email && <p className="status-email">{status.email}</p>}
        <div className="stack-btns">
          <button type="button" className="btn-primary btn-block" onClick={beginConnect}>
            Connect with ChatGPT
          </button>
          <button
            type="button"
            className="secondary btn-block"
            onClick={() => apiFetch("/api/gui/chatgpt/disconnect", { method: "POST" }).then(refresh)}
          >
            قطع اتصال
          </button>
        </div>
      </div>

      {connectInfo && (
        <div className="card connect-panel">
          <h3 className="section-title">اتصال یک‌بارمصرف</h3>
          <CopyField label="CONNECT_TOKEN" value={connectInfo.connect_token} />
          <CopyField label="CONNECT_SECRET" value={connectInfo.connect_secret} />
          <CopyField label="OAUTH_STATE" value={connectInfo.oauth_state} />
          <CopyField label="AUTHORIZE_URL" value={connectInfo.authorize_url} />
        </div>
      )}

      <form
        className="card form-stack"
        onSubmit={(e) => {
          e.preventDefault();
          saveSchedule();
        }}
      >
        <h3 className="section-title">تحلیل خودکار</h3>
        <label className="toggle-row">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>اجازه استفاده از ChatGPT در پس‌زمینه</span>
        </label>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={(e) => setScheduleEnabled(e.target.checked)}
            disabled={!connected}
          />
          <span>فعال‌سازی زمان‌بندی</span>
        </label>
        <label>
          هر چند دقیقه؟ (۱۵–۲۴۰)
          <input
            type="number"
            min={15}
            max={240}
            inputMode="numeric"
            value={intervalMin}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
            disabled={!connected}
          />
        </label>
        {status && (
          <p className="hint">
            {status.scheduling_active ? "زمان‌بندی فعال" : "زمان‌بندی غیرفعال"}
            {status.next_run_at &&
              ` · بعدی: ${new Date(status.next_run_at).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" })}`}
          </p>
        )}
        <button type="submit" className="btn-primary btn-block" disabled={!connected}>
          ذخیره
        </button>
      </form>
    </div>
  );
}

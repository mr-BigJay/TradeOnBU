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
  next_run_at?: string;
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
    setMsg("مقادیر یک‌بارمصرف را کپی کنید و روی ویندوز اسکریپت helper را اجرا کنید.");
  }

  async function saveSchedule() {
    if (scheduleEnabled && !consent) {
      setMsg("رضایت تحلیل خودکار را تیک بزنید.");
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
      <h2>ChatGPT Plus</h2>
      <p className="hint">اتصال SIWC — بدون API key جداگانه. Paper only.</p>
      {msg && <p className="hint">{msg}</p>}

      <div className="card form-stack">
        <p>
          وضعیت: <strong>{connected ? "متصل" : "قطع"}</strong>
          {status?.email ? ` — ${status.email}` : ""}
        </p>
        <div className="row-btns">
          <button type="button" onClick={beginConnect}>
            Connect with ChatGPT
          </button>
          <button type="button" className="secondary" onClick={() => apiFetch("/api/gui/chatgpt/disconnect", { method: "POST" }).then(refresh)}>
            قطع اتصال
          </button>
        </div>
      </div>

      {connectInfo && (
        <details className="card mono connect-box" open>
          <summary>داده‌های اتصال (یک‌بار)</summary>
          <p>CONNECT_TOKEN={connectInfo.connect_token}</p>
          <p>CONNECT_SECRET={connectInfo.connect_secret}</p>
          <p>OAUTH_STATE={connectInfo.oauth_state}</p>
          <p className="break-all">AUTHORIZE_URL={connectInfo.authorize_url}</p>
        </details>
      )}

      <form className="card form-stack" onSubmit={(e) => { e.preventDefault(); saveSchedule(); }}>
        <h3>تحلیل خودکار</h3>
        <label className="check-row">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          اجازه استفاده از ChatGPT Plus در پس‌زمینه
        </label>
        <label className="check-row">
          <input type="checkbox" checked={scheduleEnabled} onChange={(e) => setScheduleEnabled(e.target.checked)} disabled={!connected} />
          هر{" "}
          <input
            type="number"
            min={15}
            max={240}
            value={intervalMin}
            onChange={(e) => setIntervalMin(Number(e.target.value))}
            className="inline-num"
            disabled={!connected}
          />{" "}
          دقیقه
        </label>
        {status && (
          <p className="hint">
            زمان‌بندی: {status.scheduling_active ? "فعال" : "غیرفعال"}
            {status.next_run_at ? ` — بعدی: ${new Date(status.next_run_at).toLocaleString("fa-IR")}` : ""}
          </p>
        )}
        <button type="submit" disabled={!connected}>
          ذخیره
        </button>
      </form>
    </div>
  );
}

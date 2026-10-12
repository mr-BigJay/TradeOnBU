import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch, fetchMe, getApiKey, isAuthError, login, logout, setApiKey, setCsrf } from "./api";
import { ChartPanel } from "./ChartPanel";
import { SettingsPage } from "./SettingsPage";

type Trend = {
  symbol: string;
  trend_4h: string;
  trend_15m: string;
  trend_5m: string;
  ict_bias: string;
  details: Record<string, unknown>;
  recorded_at: string;
  live_price: number | null;
  price_stale: boolean;
  last_price_at: string | null;
};

type Signal = {
  id: number;
  public_id: string;
  symbol: string;
  source: string;
  status: string;
  direction: string;
  entry_type: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  net_rr: number;
  pending_expires_at: string | null;
  active_expires_at: string | null;
  fill_price: number | null;
  estimated_net_pnl_usdt: number | null;
  created_at: string;
};

type RuntimeSettings = {
  max_pending_minutes: number;
  max_missed_entry_pct: number;
  holding_time_hours: number;
  min_net_rr: number;
};

type SignalEvent = {
  id: number;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  note: string | null;
  created_at: string;
};

const statusColor = (s: string) => `status-${s}`;

function pctDiff(a: number, b: number) {
  return ((a - b) / b) * 100;
}

function remainingMs(until: string | null) {
  if (!until) return null;
  return new Date(until).getTime() - Date.now();
}

function fmtRemaining(ms: number | null) {
  if (ms == null) return "—";
  if (ms <= 0) return "0m";
  const m = Math.floor(ms / 60000);
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return h > 0 ? `${h}h ${rm}m` : `${rm}m`;
}

export default function App() {
  const [trend, setTrend] = useState<Trend | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<SignalEvent[]>([]);
  const [importMsg, setImportMsg] = useState<string>("");
  const [apiKeyInput, setApiKeyInput] = useState(getApiKey());
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginErr, setLoginErr] = useState("");
  const [authBoot, setAuthBoot] = useState<"loading" | "done">("loading");
  const [authed, setAuthed] = useState(false);
  const [sessionUser, setSessionUser] = useState<string | null>(null);
  const [page, setPage] = useState<"dashboard" | "settings">("dashboard");
  const [onboarding, setOnboarding] = useState<Record<string, boolean>>({});
  const [bootError, setBootError] = useState<string | null>(null);
  const [dashError, setDashError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const selected = useMemo(
    () => signals.find((s) => s.public_id === selectedId) ?? signals[0] ?? null,
    [signals, selectedId]
  );

  const signalLevels = useMemo(
    () =>
      selected
        ? { entry: selected.entry_price, sl: selected.stop_loss, tp: selected.take_profit }
        : null,
    [selected]
  );

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setDashError(null);
    const errors: string[] = [];
    let authLost = false;

    const loadOne = async <T,>(label: string, path: string, apply: (v: T) => void) => {
      try {
        const data = await apiFetch(path);
        apply(data);
      } catch (e) {
        if (isAuthError(e)) authLost = true;
        else errors.push(`${label}: ${e instanceof Error ? e.message : "خطا"}`);
      }
    };

    await loadOne<Trend>("روند بازار", "/api/trend/current", setTrend);
    await loadOne<Signal[]>("سیگنال‌ها", "/api/signals", (s) => {
      setSignals(s);
      if (!selectedId && s.length) setSelectedId(s[0].public_id);
    });
    await loadOne<RuntimeSettings>("تنظیمات", "/api/settings/runtime", setSettings);

    if (authLost) {
      setAuthed(false);
      setSessionUser(null);
    } else if (errors.length) {
      setDashError(errors.join(" • "));
    }
    setRefreshing(false);
  }, [selectedId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const m = await fetchMe();
      if (cancelled) return;
      if (m.authenticated) {
        setAuthed(true);
        setSessionUser(m.username ?? null);
        if (m.csrf_token) setCsrf(m.csrf_token);
        setOnboarding(m.onboarding || {});
      } else if (getApiKey()) {
        setAuthed(true);
      }
      if (m.error) setBootError(m.error);
      setAuthBoot("done");
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!authed || authBoot !== "done") return;
    refresh().catch(() => undefined);
    const id = setInterval(() => refresh().catch(() => undefined), 20000);
    return () => clearInterval(id);
  }, [refresh, authed, authBoot]);

  useEffect(() => {
    if (!selected || !authed) return;
    apiFetch(`/api/signals/${selected.public_id}/events`)
      .then(setEvents)
      .catch(() => setEvents([]));
  }, [selected, authed]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("signal");
    if (fromUrl) setSelectedId(fromUrl);
  }, []);

  const waiting = signals.filter((s) => s.status === "WAITING_FOR_ENTRY");
  const active = signals.filter((s) => s.status === "ACTIVE");
  const history = signals.filter((s) => !["WAITING_FOR_ENTRY", "ACTIVE", "CREATED"].includes(s.status));

  async function saveSettings(e: React.FormEvent) {
    e.preventDefault();
    if (!settings) return;
    await apiFetch("/api/settings/runtime", { method: "PUT", body: JSON.stringify(settings) });
    await refresh();
  }

  async function importSignal(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const payload = Object.fromEntries(fd.entries());
    const body = {
      symbol: String(payload.symbol || "BTCUSDT"),
      direction: String(payload.direction),
      entry_type: String(payload.entry_type || "LIMIT"),
      entry_price: Number(payload.entry_price),
      stop_loss: Number(payload.stop_loss),
      take_profit: Number(payload.take_profit),
      invalidation_level: payload.invalidation_level ? Number(payload.invalidation_level) : undefined,
      market_bias: payload.market_bias ? String(payload.market_bias) : undefined,
      liquidity_sweep: payload.liquidity_sweep === "on",
      mss_confirmation: payload.mss_confirmation === "on",
      fvg_entry: payload.fvg_entry === "on",
      notes: payload.notes ? String(payload.notes) : undefined,
    };
    try {
      const data = await apiFetch("/api/signals/import", { method: "POST", body: JSON.stringify(body) });
      setImportMsg(`Imported ${data.public_id}`);
    } catch (err) {
      setImportMsg(err instanceof Error ? err.message : "Import failed");
    }
    await refresh();
  }

  async function approve(publicId: string, approved: boolean) {
    await apiFetch("/api/signals/telegram-approval", {
      method: "POST",
      body: JSON.stringify({ public_id: publicId, approved }),
    });
    await refresh();
  }

  if (authBoot === "loading") {
    return (
      <div className="login-wrap">
        <div className="card" style={{ maxWidth: 420, margin: "12vh auto", textAlign: "center" }}>
          <h1>TradeOnBU</h1>
          <p className="hint">در حال بررسی نشست…</p>
        </div>
      </div>
    );
  }

  if (!authed) {
    return (
      <div className="login-wrap">
        <div className="card" style={{ maxWidth: 420, margin: "10vh auto" }}>
          <h1>TradeOnBU</h1>
          <p>ورود مدیر — معاملات کاغذی (Paper Only)</p>
          {bootError && (
            <p className="stale">
              {bootError} — می‌توانید با نام کاربری وارد شوید.
            </p>
          )}
          <label>نام کاربری</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          <label style={{ marginTop: 8 }}>رمز عبور</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
          {loginErr && <p className="stale">{loginErr}</p>}
          <button
            style={{ marginTop: 10 }}
            onClick={() =>
              login(username, password)
                .then((d) => {
                  setAuthed(true);
                  setSessionUser(d.username);
                })
                .catch((e) => setLoginErr(e.message))
            }
          >
            ورود
          </button>
          <details style={{ marginTop: 16 }}>
            <summary>حالت توسعه (API Key)</summary>
            <input value={apiKeyInput} onChange={(e) => setApiKeyInput(e.target.value)} placeholder="X-API-Key" />
            <button
              type="button"
              onClick={() => {
                setApiKey(apiKeyInput.trim());
                setAuthed(true);
              }}
            >
              ورود با کلید
            </button>
          </details>
        </div>
      </div>
    );
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>TradeOnBU</h1>
        <p style={{ color: "#9fb0d0", fontSize: "0.85rem" }}>
          {sessionUser ? `کاربر: ${sessionUser}` : "ICT Paper Trading"}
        </p>
        <nav className="side-nav">
          <button type="button" className={page === "dashboard" ? "active" : ""} onClick={() => setPage("dashboard")}>
            داشبورد
          </button>
          <button type="button" className={page === "settings" ? "active" : ""} onClick={() => setPage("settings")}>
            تنظیمات
          </button>
          <button type="button" className="secondary" onClick={() => logout().finally(() => setAuthed(false))}>
            خروج
          </button>
        </nav>

        {Object.keys(onboarding).length < 3 && (
          <div className="card">
            <h3>راه‌اندازی اولیه</h3>
            <ul className="checklist">
              <li>اتصال تلگرام در تنظیمات</li>
              <li>بررسی داده Bitunix</li>
              <li>تأیید ریسک paper</li>
            </ul>
          </div>
        )}

        {page === "settings" ? null : (
        <>
        <div className="card">
          <h3>Runtime Settings</h3>
          {settings && (
            <form onSubmit={saveSettings}>
              <label>Max Pending (min)</label>
              <input
                type="number"
                value={settings.max_pending_minutes}
                onChange={(e) => setSettings({ ...settings, max_pending_minutes: Number(e.target.value) })}
              />
              <label style={{ marginTop: 8 }}>Missed Entry %</label>
              <input
                type="number"
                step="0.01"
                value={settings.max_missed_entry_pct}
                onChange={(e) => setSettings({ ...settings, max_missed_entry_pct: Number(e.target.value) })}
              />
              <label style={{ marginTop: 8 }}>Holding Limit (h)</label>
              <input
                type="number"
                step="0.5"
                value={settings.holding_time_hours}
                onChange={(e) => setSettings({ ...settings, holding_time_hours: Number(e.target.value) })}
              />
              <label style={{ marginTop: 8 }}>Min Net R:R</label>
              <input
                type="number"
                step="0.1"
                value={settings.min_net_rr}
                onChange={(e) => setSettings({ ...settings, min_net_rr: Number(e.target.value) })}
              />
              <button type="submit" style={{ marginTop: 10 }}>
                Save
              </button>
            </form>
          )}
        </div>

        <div className="card">
          <h3>ChatGPT Import</h3>
          <form onSubmit={importSignal}>
            <label>Symbol</label>
            <input name="symbol" defaultValue="BTCUSDT" />
            <label style={{ marginTop: 8 }}>Direction</label>
            <select name="direction">
              <option>LONG</option>
              <option>SHORT</option>
            </select>
            <label style={{ marginTop: 8 }}>Entry Type</label>
            <select name="entry_type">
              <option>LIMIT</option>
              <option>STOP</option>
              <option>CONFIRMATION</option>
            </select>
            <label style={{ marginTop: 8 }}>Entry</label>
            <input name="entry_price" type="number" step="0.01" required />
            <label style={{ marginTop: 8 }}>SL / TP</label>
            <div className="grid-2">
              <input name="stop_loss" type="number" step="0.01" required />
              <input name="take_profit" type="number" step="0.01" required />
            </div>
            <label style={{ marginTop: 8 }}>Invalidation (optional)</label>
            <input name="invalidation_level" type="number" step="0.01" />
            <label style={{ marginTop: 8 }}>Notes</label>
            <textarea name="notes" rows={2} />
            <button type="submit" style={{ marginTop: 10 }}>
              Import Manual Review
            </button>
          </form>
          {importMsg && <p className="mono">{importMsg}</p>}
        </div>
        </>
        )}
      </aside>

      <main className="main">
        {dashError && (
          <div className="card banner-warn">
            <strong>برخی داده‌ها بارگذاری نشد</strong>
            <p className="hint">{dashError}</p>
            <button type="button" className="secondary" style={{ width: "auto" }} onClick={() => refresh()}>
              تلاش مجدد
            </button>
          </div>
        )}
        {refreshing && <p className="hint">به‌روزرسانی داشبورد…</p>}
        {page === "settings" ? (
          <SettingsPage />
        ) : (
        <>
        <div className="grid-2">
          <div className="card">
            <h2>Current Market Trend</h2>
            {trend && (
              <>
                <p>
                  4H: <span className="badge">{trend.trend_4h}</span> • 15M:{" "}
                  <span className="badge">{trend.trend_15m}</span> • 5M:{" "}
                  <span className="badge">{trend.trend_5m}</span>
                </p>
                <p>
                  ICT Bias: <strong>{trend.ict_bias}</strong>
                </p>
                <p className={trend.price_stale ? "stale" : ""}>
                  Live: {trend.live_price ?? "—"} {trend.price_stale ? "(stale — not used for false live display)" : ""}
                </p>
              </>
            )}
          </div>
          <div className="card">
            <h2>Selected Signal</h2>
            {selected ? (
              <>
                <p>
                  <span className={`badge ${statusColor(selected.status)}`}>{selected.status}</span>{" "}
                  {selected.direction} • Net R:R {selected.net_rr}
                </p>
                <p className="mono">{selected.public_id}</p>
                {selected.status === "WAITING_FOR_ENTRY" && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <button onClick={() => approve(selected.public_id, true)}>Approve Monitoring</button>
                    <button className="secondary" onClick={() => approve(selected.public_id, false)}>
                      Reject
                    </button>
                  </div>
                )}
              </>
            ) : (
              <p>No signals yet.</p>
            )}
          </div>
        </div>

        <ChartPanel symbol={trend?.symbol || "BTCUSDT"} direction={selected?.direction} signalLevels={signalLevels} />

        <SignalTable
          title="Waiting Signals"
          rows={waiting}
          live={trend?.live_price ?? null}
          onSelect={setSelectedId}
          selectedId={selected?.public_id ?? null}
        />
        <SignalTable
          title="Active Simulated Signals"
          rows={active}
          live={trend?.live_price ?? null}
          onSelect={setSelectedId}
          selectedId={selected?.public_id ?? null}
          active
        />
        <SignalTable title="Signal History / Outcomes" rows={history} live={trend?.live_price ?? null} onSelect={setSelectedId} selectedId={selected?.public_id ?? null} />

        <div className="card">
          <h3>Signal Event History {selected ? `(${selected.public_id.slice(0, 8)}…)` : ""}</h3>
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Event</th>
                <th>From</th>
                <th>To</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => (
                <tr key={ev.id}>
                  <td>{new Date(ev.created_at).toLocaleString()}</td>
                  <td>{ev.event_type}</td>
                  <td>{ev.from_status ?? "—"}</td>
                  <td>{ev.to_status ?? "—"}</td>
                  <td>{ev.note ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        </>
        )}
      </main>
    </div>
  );
}

function SignalTable({
  title,
  rows,
  live,
  onSelect,
  selectedId,
  active,
}: {
  title: string;
  rows: Signal[];
  live: number | null;
  onSelect: (id: string) => void;
  selectedId: string | null;
  active?: boolean;
}) {
  return (
    <div className="card">
      <h3>{title}</h3>
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Dir</th>
            <th>Entry Dist %</th>
            <th>Validity Left</th>
            <th>Live vs TP/SL</th>
            <th>Unrealized P&L</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const dist = live != null ? pctDiff(live, s.entry_price) : null;
            const until = active ? s.active_expires_at : s.pending_expires_at;
            const unreal =
              live != null && (s.status === "ACTIVE" || s.status === "WAITING_FOR_ENTRY")
                ? estimatePnl(s.direction, s.fill_price ?? s.entry_price, live)
                : s.estimated_net_pnl_usdt;
            return (
              <tr
                key={s.public_id}
                style={{ cursor: "pointer", background: selectedId === s.public_id ? "#18213a" : undefined }}
                onClick={() => onSelect(s.public_id)}
              >
                <td>
                  <span className={`badge ${statusColor(s.status)}`}>{s.status}</span>
                </td>
                <td>{s.direction}</td>
                <td>{dist != null ? dist.toFixed(3) : "—"}</td>
                <td>{fmtRemaining(remainingMs(until))}</td>
                <td>
                  {live != null
                    ? `TP ${pctDiff(live, s.take_profit).toFixed(2)}% / SL ${pctDiff(live, s.stop_loss).toFixed(2)}%`
                    : "—"}
                </td>
                <td>{unreal != null ? `${unreal.toFixed(2)} USDT` : "—"}</td>
                <td>{s.estimated_net_pnl_usdt != null ? `${s.estimated_net_pnl_usdt.toFixed(2)} USDT` : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {!rows.length && <p style={{ color: "#9fb0d0" }}>None</p>}
    </div>
  );
}

function estimatePnl(direction: string, entry: number, live: number) {
  const notional = 50 * 10;
  const raw = direction === "LONG" ? ((live - entry) / entry) * notional : ((entry - live) / entry) * notional;
  return raw - notional * 0.0006 * 2;
}

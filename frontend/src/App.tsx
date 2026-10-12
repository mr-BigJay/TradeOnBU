import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, LineStyle } from "lightweight-charts";

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
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const chartContainerRef = useRef<HTMLDivElement | null>(null);

  const selected = useMemo(
    () => signals.find((s) => s.public_id === selectedId) ?? signals[0] ?? null,
    [signals, selectedId]
  );

  const refresh = useCallback(async () => {
    const [t, s, r] = await Promise.all([
      fetch("/api/trend/current").then((x) => x.json()),
      fetch("/api/signals").then((x) => x.json()),
      fetch("/api/settings/runtime").then((x) => x.json()),
    ]);
    setTrend(t);
    setSignals(s);
    setSettings(r);
    if (!selectedId && s.length) setSelectedId(s[0].public_id);
  }, [selectedId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 15000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    fetch(`/api/signals/${selected.public_id}/events`)
      .then((x) => x.json())
      .then(setEvents);
  }, [selected]);

  useEffect(() => {
    if (!chartContainerRef.current) return;
    chartRef.current?.remove();
    chartRef.current = createChart(chartContainerRef.current, {
      layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
      grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
      height: 420,
    });
    seriesRef.current = chartRef.current.addLineSeries({ color: "#5da2ff", lineWidth: 2 });
    const price = trend?.live_price ?? selected?.entry_price ?? 0;
    if (price) {
      const now = Math.floor(Date.now() / 1000);
      seriesRef.current.setData([
        { time: now - 3600, value: price * 0.998 },
        { time: now - 1800, value: price * 1.001 },
        { time: now, value: price },
      ]);
    }
    if (selected) {
      for (const m of [
        { price: selected.entry_price, color: "#ffd76a", title: "Entry" },
        { price: selected.stop_loss, color: "#ff6b6b", title: "SL" },
        { price: selected.take_profit, color: "#6dffb5", title: "TP" },
      ]) {
        seriesRef.current.createPriceLine({
          price: m.price,
          color: m.color,
          lineWidth: 2,
          lineStyle: LineStyle.Dashed,
          title: m.title,
        });
      }
    }
    return () => chartRef.current?.remove();
  }, [selected, trend]);

  const waiting = signals.filter((s) => s.status === "WAITING_FOR_ENTRY");
  const active = signals.filter((s) => s.status === "ACTIVE");
  const history = signals.filter((s) => !["WAITING_FOR_ENTRY", "ACTIVE", "CREATED"].includes(s.status));

  async function saveSettings(e: React.FormEvent) {
    e.preventDefault();
    if (!settings) return;
    await fetch("/api/settings/runtime", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
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
    const resp = await fetch("/api/signals/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    setImportMsg(resp.ok ? `Imported ${data.public_id}` : data.detail || "Import failed");
    await refresh();
  }

  async function approve(publicId: string, approved: boolean) {
    await fetch("/api/signals/telegram-approval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ public_id: publicId, approved }),
    });
    await refresh();
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>Trade Monitor</h1>
        <p style={{ color: "#9fb0d0", fontSize: "0.85rem" }}>
          ICT lifecycle • Paper trading only • No auto ChatGPT API
        </p>

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
      </aside>

      <main className="main">
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

        <div className="card">
          <h2>Chart — Entry / SL / TP</h2>
          <div className="chart-wrap" ref={chartContainerRef} />
        </div>

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

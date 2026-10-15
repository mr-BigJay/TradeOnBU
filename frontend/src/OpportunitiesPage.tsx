import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

type SetupRow = {
  setup_id: string;
  direction: string;
  lifecycle_state: string;
  status_label_fa: string;
  entry_zone_low?: number;
  entry_zone_high?: number;
  stop_loss?: number;
  take_profit?: number;
  missing_confirmations?: string[];
  completed_confirmations?: string[];
  rejection_label_fa?: string;
  age_minutes?: number;
  expires_at_tehran?: string;
};

type Dashboard = {
  live_price?: number;
  wait_setups: SetupRow[];
  ready_setups: SetupRow[];
  pending_orders: { public_id: string; status_label_fa: string }[];
  open_positions: { public_id: string; status_label_fa: string }[];
  invalidated_setups: SetupRow[];
  expired_setups: SetupRow[];
  no_trade_reports: { run_id: number; preview?: string; created_at_tehran?: string }[];
  stats: Record<string, number>;
};

export function OpportunitiesPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setErr("");
    setData(await apiFetch("/api/gui/opportunities/dashboard"));
  }, []);

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
    const t = setInterval(() => load().catch(() => {}), 60_000);
    return () => clearInterval(t);
  }, [load]);

  if (err) return <p className="toast toast-error">{err}</p>;
  if (!data) return <p className="hint">در حال بارگذاری فرصت‌ها…</p>;

  function renderSetups(title: string, rows: SetupRow[]) {
    return (
      <section className="section">
        <h2 className="section-title">{title}</h2>
        {rows.length === 0 && <p className="hint">موردی نیست.</p>}
        {rows.map((s) => (
          <div key={s.setup_id} className="card position-card">
            <div className="history-card-top">
              <span className={s.direction === "LONG" ? "badge badge-long" : "badge badge-short"}>{s.direction}</span>
              <span className="badge">{s.lifecycle_state}</span>
            </div>
            <p>{s.status_label_fa}</p>
            <p className="meta-line">
              Zone {s.entry_zone_low} – {s.entry_zone_high} · SL {s.stop_loss} · TP {s.take_profit}
            </p>
            {s.missing_confirmations && s.missing_confirmations.length > 0 && (
              <p className="hint">باقی‌مانده: {s.missing_confirmations.join(", ")}</p>
            )}
            {s.completed_confirmations && s.completed_confirmations.length > 0 && (
              <p className="hint">تأیید شده: {s.completed_confirmations.join(", ")}</p>
            )}
            {s.rejection_label_fa && <p className="toast">{s.rejection_label_fa}</p>}
            <p className="meta-line">
              سن {s.age_minutes ?? "—"} دقیقه · انقضا {s.expires_at_tehran ?? "—"}
            </p>
          </div>
        ))}
      </section>
    );
  }

  return (
    <div className="page">
      <h1>فرصت‌های ICT</h1>
      <p className="hint">Paper only · مانیتور ~۶۰ ثانیه · AI تحلیل در بازه اشتراک</p>
      <p className="meta-line">قیمت زنده: {data.live_price ?? "—"}</p>

      {renderSetups("WAIT — در انتظار تأیید", data.wait_setups)}
      {renderSetups("READY — آماده Paper", data.ready_setups)}

      <section className="section">
        <h2 className="section-title">سفارش / پوزیشن Paper</h2>
        {data.pending_orders.map((p) => (
          <div key={p.public_id} className="card">
            {p.status_label_fa} · {p.public_id.slice(0, 8)}
          </div>
        ))}
        {data.open_positions.map((p) => (
          <div key={p.public_id} className="card">
            {p.status_label_fa} · {p.public_id.slice(0, 8)}
          </div>
        ))}
      </section>

      {renderSetups("باطل‌شده", data.invalidated_setups)}
      {renderSetups("منقضی", data.expired_setups)}

      <section className="section">
        <h2 className="section-title">NO_TRADE اخیر</h2>
        {data.no_trade_reports.map((r) => (
          <div key={r.run_id} className="card">
            <p className="meta-line">{r.created_at_tehran}</p>
            <p>{r.preview}</p>
          </div>
        ))}
      </section>
    </div>
  );
}

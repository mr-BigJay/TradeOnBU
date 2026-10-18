import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";
import { ForecastChart } from "./ForecastChart";

type Status = {
  enabled: boolean;
  interval_minutes: number;
  model: string;
  reasoning_effort: string;
  max_output_tokens: number;
  daily_request_limit: number;
  monthly_budget_usd: number;
  last_run_at: string | null;
  last_success_at: string | null;
  next_run_at: string | null;
  last_error: string | null;
  running_status: string;
  runs_today: number;
  month_cost_usd: number;
};

export function PriceForecastPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [chartRefresh, setChartRefresh] = useState(0);

  const loadStatus = useCallback(async () => {
    const s = (await apiFetch("/api/gui/forecast/status")) as Status;
    setStatus(s);
  }, []);

  useEffect(() => {
    loadStatus().catch((e) => setToast(e.message));
  }, [loadStatus]);

  async function saveEnabled(enabled: boolean) {
    setBusy(true);
    try {
      const s = (await apiFetch("/api/gui/forecast/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      })) as Status;
      setStatus(s);
      setToast(enabled ? "پیش‌بینی خودکار فعال شد" : "پیش‌بینی خودکار غیرفعال است");
    } catch (e) {
      setToast(e instanceof Error ? e.message : "خطا");
    } finally {
      setBusy(false);
    }
  }

  async function saveInterval(minutes: number) {
    setBusy(true);
    try {
      const s = (await apiFetch("/api/gui/forecast/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interval_minutes: minutes }),
      })) as Status;
      setStatus(s);
    } catch (e) {
      setToast(e instanceof Error ? e.message : "خطا");
    } finally {
      setBusy(false);
    }
  }

  async function generateNow() {
    setBusy(true);
    setToast("");
    try {
      await apiFetch("/api/gui/forecast/generate", { method: "POST" }, 200_000);
      setToast("پیش‌بینی جدید با موفقیت ثبت شد");
      await loadStatus();
      setChartRefresh((k) => k + 1);
    } catch (e) {
      setToast(e instanceof Error ? e.message : "تولید پیش‌بینی ناموفق");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <h1>پیش‌بینی قیمت</h1>
        <p className="hint">مسیر احتمالی BTCUSDT — مستقل از تحلیل ICT و پوزیشن‌ها</p>
      </header>

      {toast && <p className="toast">{toast}</p>}

      <div className="card">
        <h2>تنظیمات</h2>
        {status && (
          <div className="forecast-settings-grid">
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={status.enabled}
                disabled={busy}
                onChange={(e) => saveEnabled(e.target.checked)}
              />
              پیش‌بینی خودکار (پیش‌فرض خاموش)
            </label>
            <label>
              فاصله اجرا (دقیقه)
              <input
                type="number"
                min={15}
                max={360}
                value={status.interval_minutes}
                disabled={busy}
                onChange={(e) => saveInterval(Number(e.target.value))}
              />
            </label>
            <p className="hint">
              وضعیت: {status.running_status} · امروز: {status.runs_today}/{status.daily_request_limit} · هزینه ماه: $
              {status.month_cost_usd.toFixed(4)}
            </p>
            {status.last_success_at && (
              <p className="hint">آخرین موفق: {new Date(status.last_success_at).toLocaleString("fa-IR")}</p>
            )}
            {status.last_error && <p className="stale">{status.last_error}</p>}
          </div>
        )}
        <button type="button" className="btn-primary" disabled={busy} onClick={generateNow}>
          تولید پیش‌بینی جدید
        </button>
      </div>

      <ForecastChart refreshKey={chartRefresh} />
    </div>
  );
}

import type { ReactNode } from "react";
import { displayTehranTime } from "./displayTime";

type Tone = "profit" | "loss" | "neutral";
type PositionRecord = Record<string, unknown>;

export function numericValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function numberText(value: unknown, decimals = 2): string {
  const number = numericValue(value);
  if (number === null) return "—";
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(number);
}

function pnlTone(value: unknown): Tone {
  const n = numericValue(value);
  return n === null || n === 0 ? "neutral" : n > 0 ? "profit" : "loss";
}

export function MoneyValue({
  value,
  signed = false,
  decimals = 2,
  unit = "USDT",
  tone,
}: {
  value: unknown;
  signed?: boolean;
  decimals?: number;
  unit?: string;
  tone?: Tone;
}) {
  const number = numericValue(value);
  const selectedTone = tone || (signed ? pnlTone(value) : "neutral");
  const formatted = number === null ? "—" : (signed && number > 0 ? "+" : "") + numberText(number, decimals);
  return (
    <span className={"financial-value financial-value--" + selectedTone} dir="ltr">
      <bdi>{formatted}</bdi>
      {number !== null && unit && <small>{unit}</small>}
    </span>
  );
}

export function StatTile({
  label,
  children,
  emphasis = false,
  footnote,
}: {
  label: string;
  children: ReactNode;
  emphasis?: boolean;
  footnote?: string;
}) {
  return (
    <div className={"position-stat-tile" + (emphasis ? " position-stat-tile--featured" : "")}>
      <span className="position-stat-title">{label}</span>
      <strong className="position-stat-value">{children}</strong>
      {footnote && <span className="position-stat-note">{footnote}</span>}
    </div>
  );
}

export function EquityCurve({ history }: { history: unknown }) {
  const validPoints = (Array.isArray(history) ? history : [])
    .map((point) => ({
      value: numericValue(point?.equity_usdt),
      timestamp: String(point?.recorded_at_tehran || ""),
    }))
    .filter((point): point is { value: number; timestamp: string } => point.value !== null);
  // Limit SVG vertices without dropping the first/last real snapshots.
  const stride = Math.max(1, Math.ceil((validPoints.length - 1) / 500));
  const points = validPoints.filter((_, i) => i === validPoints.length - 1 || i % stride === 0);

  if (points.length < 2) {
    return (
      <div className="position-equity-empty">
        <span aria-hidden="true">◇</span>
        <strong>هنوز داده کافی برای نمودار موجودی وجود ندارد</strong>
        <p>نمودار پس از ثبت حداقل دو Snapshot واقعی نمایش داده می‌شود.</p>
      </div>
    );
  }

  let min = Infinity;
  let max = -Infinity;
  for (const point of points) {
    if (point.value < min) min = point.value;
    if (point.value > max) max = point.value;
  }
  const range = Math.max(max - min, Math.max(Math.abs(max), 1) * 0.02);
  const yAt = (v: number) => 176 - ((v - min) / range) * 145;
  const coords = points.map((point, index) => ({
    x: 16 + (index / (points.length - 1)) * 608,
    y: yAt(point.value),
  }));
  const line = coords.map((point) => numberText(point.x, 2) + "," + numberText(point.y, 2)).join(" ");
  const fill = "16,190 " + line + " 624,190";
  const delta = points[points.length - 1].value - points[0].value;

  return (
    <div className="position-equity-chart">
      <div className="position-equity-chart__heading">
        <div>
          <span className="position-stat-title">روند اکوییتی</span>
          <strong><MoneyValue value={points[points.length - 1].value} /></strong>
        </div>
        <div className="position-equity-chart__change">
          <span className="position-stat-title">تغییر در بازه</span>
          <strong><MoneyValue value={delta} signed /></strong>
        </div>
      </div>
      <svg className="position-equity-chart__svg" viewBox="0 0 640 200" role="img"
        aria-label={"روند اکوییتی، از " + numberText(points[0].value) + " تا " + numberText(points[points.length - 1].value) + " تتر"}>
        <defs>
          <linearGradient id="position-equity-gradient" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" stopColor="#3b82f6" stopOpacity=".2" />
            <stop offset="1" stopColor="#3b82f6" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[38, 83, 128, 173].map((y) =>
          <line key={y} x1="16" x2="624" y1={y} y2={y} stroke="currentColor" opacity=".13" strokeDasharray="4 7" />
        )}
        <polygon points={fill} fill="url(#position-equity-gradient)" />
        <polyline points={line} fill="none" stroke="#3b82f6"
          strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        <circle cx={coords[coords.length - 1].x} cy={coords[coords.length - 1].y}
          r="4.5" stroke="#111827" strokeWidth="2" fill="#60a5fa" />
      </svg>
      <div className="position-equity-chart__axis">
        <span>{points[0].timestamp || "شروع بازه"}</span>
        <span>{points[points.length - 1].timestamp || "آخرین ثبت"}</span>
      </div>
    </div>
  );
}

function PositionDetail({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="position-trade-detail">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function PositionTradeCard({
  position: p,
  onCancel,
  onClose,
}: {
  position: PositionRecord;
  onCancel: () => void;
  onClose: () => void;
}) {
  const status = String(p.status || "");
  const pending = status === "pending_entry";
  const open = status === "open";
  const metrics = p.metrics && typeof p.metrics === "object" ? p.metrics as PositionRecord : null;
  const pnl = !pending && metrics ? metrics.net_unrealized_pnl : null;
  const rr = metrics ? numericValue(metrics.net_rr) : null;
  const direction = String(p.direction || "").toUpperCase();
  const entry = p.fill_price ?? p.entry_price ?? p.limit_entry_price;
  const stateText = pending ? "در انتظار ورود" : open ? "باز" : status || "نامشخص";
  const pnlPct = !pending && metrics ? numericValue(metrics.pnl_pct_on_margin) : null;
  const opened = p.opened_at_tehran || p.created_at_tehran;
  return (
    <article className="position-trade-card">
      <div className="position-trade-card__top">
        <div className="position-trade-card__symbol">
          <strong dir="ltr">{String(p.symbol || "BTCUSDT")}</strong>
          <span className={"position-direction position-direction--" + (direction === "LONG" ? "long" : "short")}>
            {direction === "LONG" ? "LONG" : "SHORT"}
          </span>
          <span className="position-leverage" dir="ltr">{numberText(p.leverage, 0)}×</span>
        </div>
        <span className={"position-order-status" + (pending ? " position-order-status--pending" : "")}>
          <i aria-hidden="true" />{stateText}
        </span>
      </div>
      <div className="position-trade-card__summary">
        <div>
          <span className="position-stat-title">{pending ? "سود / زیان بعد از ورود" : "سود و زیان خالص تخمینی"}</span>
          <strong className="position-trade-card__pnl">
            {pending ? <span className="financial-value financial-value--neutral">—</span> : <MoneyValue value={pnl} signed />}
          </strong>
          {!pending && pnlPct !== null &&
            <span className={"position-trade-card__percent " + (pnlPct > 0 ? "position-percent--profit" : pnlPct < 0 ? "position-percent--loss" : "")}
              dir="ltr">{pnlPct > 0 ? "+" : ""}{numberText(pnlPct)}%</span>}
          {!pending && pnl === null && <span className="position-stat-note">قیمت معتبر برای محاسبه موجود نیست</span>}
        </div>
        <div className="position-trade-card__rr">
          <span className="position-stat-title">R:R خالص</span>
          <strong dir="ltr">{rr !== null && rr > 0 ? "1 : " + numberText(rr) : "—"}</strong>
        </div>
      </div>
      <div className="position-trade-card__details">
        <PositionDetail label="ورود" value={<span dir="ltr">{numberText(entry)}</span>} />
        <PositionDetail label="قیمت بازار" value={<span dir="ltr">{pending ? "—" : numberText(p.mark_price)}</span>} />
        <PositionDetail label="حد ضرر" value={<span dir="ltr">{numberText(p.stop_loss)}</span>} />
        <PositionDetail label="حد سود" value={<span dir="ltr">{numberText(p.take_profit)}</span>} />
        <PositionDetail label="مارجین" value={<MoneyValue value={p.margin_usdt} />} />
        <PositionDetail label="نوع سفارش" value={String(p.order_type || "—").toUpperCase()} />
        <PositionDetail label="ارزش اسمی پوزیشن" value={<MoneyValue value={metrics?.notional_usdt} />} />
        <PositionDetail label="کارمزد خروج تخمینی" value={<MoneyValue value={metrics?.estimated_exit_fee_usdt} decimals={4} />} />
        <PositionDetail label="زمان ثبت / ورود" value={opened ? displayTehranTime(String(opened)) : "—"} />
      </div>
      <div className="position-trade-card__footer">
        <span>معامله آزمایشی · {p.execution_state === "suspended" ? "بررسی داده بازار متوقف شده" : "Bitunix VIP1"}</span>
        {pending && <button className="secondary" type="button" onClick={onCancel}>لغو سفارش</button>}
        {open && <button className="secondary position-trade-card__close" type="button" onClick={onClose}>بستن دستی</button>}
      </div>
    </article>
  );
}

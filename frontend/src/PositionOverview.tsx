import { EquityCurve, MoneyValue, numberText, StatTile } from "./PositionVisuals";

type Data = Record<string, unknown>;

export function PositionWalletOverview({ wallet }: { wallet: Data | null }) {
  return (
    <section className="position-overview" aria-labelledby="position-overview-title">
      <div className="position-section-heading position-overview__heading">
        <div>
          <h2 id="position-overview-title">خلاصه حساب</h2>
          <p>موجودی و وضعیت مالی حساب شبیه‌سازی‌شده</p>
        </div>
      </div>
      {!wallet ? (
        <div className="position-empty-list position-empty-list--card" role="status">در حال دریافت خلاصه کیف پول…</div>
      ) : (
        <div className="position-stat-grid position-stat-grid--overview">
          <StatTile label="اکوییتی حساب" emphasis><MoneyValue value={wallet.account_equity} /></StatTile>
          <StatTile label="موجودی آزاد"><MoneyValue value={wallet.available_balance} /></StatTile>
          <StatTile label="مارجین در استفاده"><MoneyValue value={wallet.margin_in_use} /></StatTile>
          <StatTile label="سود / زیان تحقق‌نیافته">
            <MoneyValue value={wallet.unrealized_pnl} signed />
          </StatTile>
          <StatTile label="سود / زیان تحقق‌یافته">
            <MoneyValue value={wallet.realized_pnl} signed />
          </StatTile>
          <StatTile label="کارمزد کل"><MoneyValue value={wallet.total_fees} /></StatTile>
        </div>
      )}
    </section>
  );
}

export function PositionPerformancePreview({
  report,
  onOpenReport,
}: {
  report: Data | null;
  onOpenReport: () => void;
}) {
  return (
    <section className="position-performance-preview" aria-labelledby="position-performance-title">
      <div className="position-section-heading">
        <div>
          <h2 id="position-performance-title">گزارش عملکرد</h2>
          <p>خلاصه عملکرد ۳۰ روز گذشته، بر اساس معاملات آزمایشی ثبت‌شده</p>
        </div>
        <button type="button" className="position-view-report secondary" onClick={onOpenReport}>گزارش کامل</button>
      </div>
      {!report ? (
        <div className="position-empty-list position-empty-list--card" role="status">در حال دریافت گزارش عملکرد…</div>
      ) : (
        <>
          <div className="position-stat-grid position-stat-grid--performance-preview">
            <StatTile label="سود / زیان خالص" emphasis footnote="نتیجه معاملات بسته‌شده">
              <MoneyValue value={report.net_profit_loss} signed />
            </StatTile>
            <StatTile label="نرخ برد" footnote="معاملات بسته‌شده قابل گزارش">
              <span dir="ltr">{numberText(report.win_rate, 1)}%</span>
            </StatTile>
            <StatTile label="Profit Factor">
              <span dir="ltr">{numberText(report.profit_factor)}</span>
            </StatTile>
            <StatTile label="حداکثر افت سرمایه">
              <MoneyValue value={report.max_drawdown_pct} unit="%" tone="loss" />
            </StatTile>
            <StatTile label="کارمزد کل">
              <MoneyValue value={report.fees} />
            </StatTile>
          </div>
          <div className="card position-chart-panel">
            <EquityCurve history={report.equity_history} />
          </div>
        </>
      )}
    </section>
  );
}

import { displayTehranTime } from './displayTime';

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function number(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
}
function delta(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${number(value)}` : '—';
}
export function IndicatorContext({ features }: { features?: unknown }) {
  const frames = record(record(features).timeframes);
  if (!Object.keys(frames).length || record(features).version !== 2) return <p className="hint">اندیکاتورهای ورودی API برای این گزارش ثبت نشده‌اند.</p>;
  const relations: Record<string,string> = { above:'EMA9 بالاتر از EMA20', below:'EMA9 پایین‌تر از EMA20', equal:'دو EMA برابرند' };
  const crosses: Record<string,string> = { up:'کراس رو به بالا', down:'کراس رو به پایین', none:'بدون کراس جدید' };
  return <section className="card" aria-label="اندیکاتورهای ورودی API">
    <h3>اندیکاتورهای ورودی API</h3>
    <p className="hint">مقادیر ثبت‌شده هنگام تحلیل از کندل‌های بستهٔ Bitunix؛ قیمت زنده نیستند. تغییرات نسبت به یک کندل قبل است. این اطلاعات شرط اجباری ورود نیستند.</p>
    <div style={{overflowX:'auto'}}>
      <table style={{minWidth:880,width:'100%'}}>
        <thead><tr>{['تایم‌فریم / زمان بازشدن آخرین کندل (تهران)','EMA9 / تغییر','EMA20 / تغییر','RSI14 / تغییر','وضعیت EMA','فاصله قیمت از EMA9 / EMA20 (ATR)','ATR14 / ADX14 / RVOL20'].map(label=><th key={label}>{label}</th>)}</tr></thead>
        <tbody>{Object.entries(frames).map(([tf,raw])=>{
          const f=record(raw);
          const timestamp=typeof f.last_closed_open_ms==='number' && Number.isFinite(f.last_closed_open_ms) && Math.abs(f.last_closed_open_ms)<8.64e15 ? new Date(f.last_closed_open_ms).toISOString() : null;
          return <tr key={tf}>
            <th><bdi>{tf}</bdi>{timestamp && <div className="hint"><bdi>{displayTehranTime(null,timestamp)}</bdi></div>}</th>
            {f.available !== true ? <td colSpan={6}>دادهٔ کافی یا معتبر ثبت نشده است.</td> : <>
              <td><bdi>{number(f.ema9)}</bdi><div className="hint">تغییر: <bdi>{delta(f.ema9_change)}</bdi></div></td>
              <td><bdi>{number(f.ema20)}</bdi><div className="hint">تغییر: <bdi>{delta(f.ema20_change)}</bdi></div></td>
              <td><bdi>{number(f.rsi14)}</bdi><div className="hint">تغییر: <bdi>{delta(f.rsi14_change)}</bdi></div></td>
              <td>{relations[String(f.ema9_ema20_relation)] || '—'}<div className="hint">{crosses[String(f.ema9_ema20_cross)] || '—'}</div></td>
              <td><bdi>{delta(f.close_ema9_atr)} / {delta(f.close_ema_atr)}</bdi></td>
              <td><bdi>{number(f.atr14)} / {number(f.adx14)} / {number(f.relative_volume20)}</bdi></td>
            </>}
          </tr>;
        })}</tbody>
      </table>
    </div>
    <p className="hint">خط تیره یعنی مقدار موجود یا قابل محاسبه نیست؛ اختلاف سابقهٔ کندل‌ها و منبع قیمت می‌تواند باعث تفاوت با TradingView شود.</p>
  </section>;
}

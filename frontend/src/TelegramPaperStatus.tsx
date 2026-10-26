import { useEffect, useState } from "react";
import { apiFetch } from "./api";

type Status = {enabled:boolean; reader_fresh:boolean; events:{id:string;symbol:string|null;status:string;reason:string}[]};
const reasons:Record<string,string> = {
  signal_already_invalidated:"قیمت قبل از ورود از حدضرر یا هدف عبور کرده",
  outside_entry_range:"منتظر رسیدن قیمت به بازهٔ ورود", slippage_outside_range:"قیمت اجرا خارج از بازه است",
  stale_message:"مهلت سیگنال تمام شده", edited_or_baseline:"پیام قدیمی یا ویرایش‌شده",
  unsupported_or_invalid_signal:"پیام، سیگنال کامل با قالب پشتیبانی‌شده نیست",
  paper_trading_paused:"معاملات آزمایشی متوقف شده", position_limit:"سقف تعداد معاملات پر شده",
  symbol_already_active:"این ارز معاملهٔ فعال دارد", insufficient_balance:"موجودی کافی نیست",
  rr_below_minimum:"نسبت سود به زیان کافی نیست", wallet_or_risk_rejected:"محدودیت سرمایه یا ریسک",
  market_unavailable:"قیمت معتبر در دسترس نیست", fill_rejected:"شرایط ورود برقرار نیست",
};
export function TelegramPaperStatus() {
  const [data,setData]=useState<Status|null>(null);
  const [error,setError]=useState(false);
  useEffect(()=>{
    let active=true;
    const load=()=>apiFetch('/api/gui/position/telegram-status').then(d=>{if(active){setData(d);setError(false);}}).catch(()=>{if(active)setError(true);});
    load(); const timer=setInterval(load,15000);
    return ()=>{active=false;clearInterval(timer);};
  },[]);
  if(!data?.enabled && !error) return null;
  return <details className="card"><summary>سیگنال‌های BehDark · {error?"خطا در دریافت وضعیت":data?.reader_fresh?"خواننده متصل است":"دریافت پیام تأیید نشده"}</summary>
    <p>فقط معاملات آزمایشی؛ هدف اول و حدضرر اولیه. پیام‌های تغییر حدضرر یا بستن دستی معامله خودکار اجرا نمی‌شوند.</p>
    {!data?.events.length && <p>هنوز پیام تازه‌ای برای بررسی ورود دریافت نشده است.</p>}
    {data?.events.map(e=><p key={e.id}><b dir="ltr">{e.symbol||"پیام کانال"}</b> · {e.status==='opened'?'معامله ثبت شد':e.status==='waiting'?'در انتظار':'ورود انجام نشد'}{e.reason?' — '+(reasons[e.reason]||'پیام قابل پردازش نیست'):''}</p>)}
  </details>;
}

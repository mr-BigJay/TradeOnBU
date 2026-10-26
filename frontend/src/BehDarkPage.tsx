import { useEffect, useRef, useState } from "react";
import { apiFetch, getApiKey, getCsrf } from "./api";
import "./behdark.css";

type Signal = {
  id: string; symbol?: string; direction?: string; status: string; reason: string;
  received: number; opened_at?: number; closed_at?: number; entry?: number; low?: number; high?: number;
  stop?: number; targets?: number[]; target_hits: number[]; pnl: number; fees: number;
  margin?: number; leverage?: number; remaining: number; quantity?: number; uncertain: boolean;
  body: string; revisions: { at: number; body: string }[];
};
type Wallet = {capital:number; balance:number; available:number; reserved:number; equity:number; unrealized:number;
  margin:number; leverage:number; fee_bps:number; expiry_hours:number; enabled:boolean; activated_at:number|null};
type Snapshot = {wallet:Wallet; signals:Signal[]; quotes:Record<string,{price:number;at:number}>;
  report:{total:number;closed:number;certain_closed:number;wins:number;losses:number;breakeven:number;winrate:number|null;
    uncertain_closed:number;net:number;fees:number;realized_drawdown:number;by_symbol:Record<string,{closed:number;wins:number;net:number}>};
  status:{heartbeat?:number;reader_fresh?:boolean;active_symbols?:string[];queued_symbols?:number;errors?:Record<string,string>;error?:string};history_limit:number};
const labels:Record<string,string>={waiting:"منتظر ورود",open:"باز",closed:"بسته‌شده",review:"نیازمند بررسی",cancelled:"لغوشده",expired:"منقضی",invalidated:"باطل‌شده"};
const reasons:Record<string,string>={unsupported_template:"قالب پیام قابل پردازش نیست",reply_or_edited_message:"پیام ویرایش‌شده یا پاسخ به پیام",late_receipt:"دریافت با تأخیر؛ ورود خودکار انجام نشد",message_edited:"پیام تغییر کرده؛ شرایط اولیه حفظ شده است",ambiguous_entry_candle:"ترتیب ورود و برخورد به هدف یا حدضرر نامعلوم است",stop_before_entry:"حدضرر پیش از ورود لمس شد",insufficient_balance:"موجودی آزاد کافی نیست",paused:"ورود جدید متوقف است",capacity_queue:"در صف پایش؛ سقف ارزهای هم‌زمان پر است",configured_expiry:"پایان زمان اعتبار تنظیم‌شده",manual_cancel:"لغو دستی"};
const fmt=(n:number|undefined|null,d=2)=>n==null?"—":n.toLocaleString("en-US",{maximumFractionDigits:d});
const date=(n?:number|null)=>n?new Date(n*1000).toLocaleString("fa-IR",{timeZone:"Asia/Tehran"}):"—";
const TABS=[['signals','سیگنال‌ها'],['open','پوزیشن‌های باز'],['wallet','کیف پول'],['report','گزارش']] as const;

export function BehDarkPage(){
  const [tab,setTab]=useState<string>('signals'); const [data,setData]=useState<Snapshot|null>(null);
  const [error,setError]=useState(''); const [busy,setBusy]=useState(false); const [notice,setNotice]=useState('');
  const [form,setForm]=useState({margin:20,leverage:3,fee_bps:6,expiry_hours:0,enabled:false});
  const initialForm=useRef(false);
  const [capital,setCapital]=useState(1000); const [filter,setFilter]=useState('all');
  const refresh=async()=>{const d=await apiFetch('/api/gui/behdark/snapshot');setData(d);return d as Snapshot;};
  useEffect(()=>{let alive=true; let pending=false;
    const load=async()=>{if(pending)return;pending=true;try{const d=await apiFetch('/api/gui/behdark/snapshot');if(alive){setData(d);setError('');if(!initialForm.current){const {margin,leverage,fee_bps,expiry_hours,enabled}=d.wallet;setForm({margin,leverage,fee_bps,expiry_hours,enabled});initialForm.current=true;}}}catch(e){if(alive)setError((e as Error).message);}finally{pending=false;}};
    void load();const t=window.setInterval(load,10000);return()=>{alive=false;clearInterval(t);};},[]);
  const action=async(path:string,body?:unknown,method='POST')=>{setBusy(true);setError('');setNotice('');try{await apiFetch('/api/gui/behdark/'+path,{method,body:body===undefined?undefined:JSON.stringify(body)});await refresh();setNotice('ذخیره شد.');}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
  const openWallet=()=>{if(data){const {margin,leverage,fee_bps,expiry_hours,enabled}=data.wallet;setForm({margin,leverage,fee_bps,expiry_hours,enabled});}setTab('wallet');};
  const download=async()=>{setBusy(true);try{const h:Record<string,string>={};if(getApiKey())h['X-API-Key']=getApiKey();if(getCsrf())h['X-CSRF-Token']=getCsrf();const r=await fetch('/api/gui/behdark/report.csv',{credentials:'include',headers:h});if(!r.ok)throw Error('دریافت گزارش ناموفق بود.');const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download='behdark.csv';a.click();URL.revokeObjectURL(url);}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
  const cards=(rows:Signal[])=>rows.length?<div className="bd-list">{rows.map(s=>{
    const q=s.symbol?data?.quotes[s.symbol]:undefined;
    const live=s.status==='open'&&q&&s.entry&&s.quantity?(q.price-s.entry)*s.quantity*s.remaining*(s.direction==='LONG'?1:-1):null;
    return <article className="card bd-signal" key={s.id}>
      <div className="bd-row"><strong dir="ltr">{s.symbol||'پیام کانال'} · {s.direction||'—'}</strong><span className={'bd-badge '+s.status}>{labels[s.status]||s.status}</span></div>
      <p className="hint">دریافت: {date(s.received)} · پیام #{s.id.split(':')[1]}</p>
      {s.symbol&&<div className="bd-grid"><div>محدودهٔ ورود <b dir="ltr">{fmt(s.low,8)} – {fmt(s.high,8)}</b></div><div>ورود ثبت‌شده <b>{fmt(s.entry,8)}</b></div><div>حدضرر <b>{fmt(s.stop,8)}</b></div><div>قیمت آخر <b>{fmt(q?.price,8)}</b>{q&&<small>{date(q.at)}</small>}</div></div>}
      {s.targets&&<p className="bd-targets">{s.targets.map((t,i)=><span key={i} dir="ltr">TP{i+1}: <b dir="ltr">{fmt(t,8)}</b>{s.target_hits.includes(i)?' ✓':''}</span>)}</p>}
      {s.margin!=null&&<p>مارجین: {fmt(s.margin)} USDT · لوریج: {s.leverage}× · حجم باقی‌مانده: {fmt(s.remaining*100)}٪</p>}
      {live!=null&&<p>سودوزیان شناور: <b dir="ltr">{fmt(live)} USDT</b> · تحقق‌یافته پس از کارمزد: <b dir="ltr">{fmt(s.pnl)} USDT</b></p>}
      {s.status==='closed'&&<p>نتیجهٔ خالص: <b dir="ltr">{fmt(s.pnl)} USDT</b> · بسته‌شدن: {date(s.closed_at)}</p>}
      {s.reason&&<p className="hint">{reasons[s.reason]||s.reason}</p>}{s.uncertain&&<p className="bd-warning">داده یا ترتیب رخدادها نامطمئن است؛ از وین‌ریت قطعی کنار گذاشته می‌شود.</p>}
      <details><summary>پیام اصلی و سابقهٔ ویرایش</summary><pre dir="auto">{s.body}</pre>{s.revisions.map((r,i)=><div key={i}><small>{date(r.at)}</small><pre dir="auto">{r.body}</pre></div>)}</details>
      {['waiting','review'].includes(s.status)&&<button disabled={busy} className="secondary" onClick={()=>void action('cancel/'+s.id.split(':')[1])}>لغو این سیگنال</button>}
    </article>;
  })}</div>:<p className="card hint">در این بخش موردی ثبت نشده است.</p>;
  return <section className="bd-page" dir="rtl">
    <div className="bd-row"><div><h1>BehDark</h1><p className="hint">معاملات آزمایشی و کیف پول مستقل</p></div><span className="bd-badge">Paper only</span></div>
    <nav className="bd-tabs" aria-label="بخش‌های BehDark">{TABS.map(([id,label])=><button key={id} className={tab===id?'active':''} onClick={()=>id==='wallet'?openWallet():setTab(id)}>{label}</button>)}</nav>
    {error&&<p role="alert" className="toast toast-error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {!data&&!error&&<p>در حال دریافت وضعیت…</p>}
    {data&&<>
      <p className="hint">ورود جدید: {data.wallet.enabled?'فعال':'متوقف'} · کانال: {data.status.reader_fresh&&Date.now()/1000-(data.status.heartbeat||0)<120?'متصل':'دادهٔ تازه دریافت نشده'} · ارزهای در حال پایش: {data.status.active_symbols?.length||0} · آخرین پایش: {date(data.status.heartbeat)}</p>
      {!!data.status.queued_symbols&&<p className="bd-warning">{data.status.queued_symbols} ارز در صف پایش است.</p>}
      {!!Object.keys(data.status.errors||{}).length&&<p className="bd-warning">دریافت داده برای {Object.keys(data.status.errors||{}).join('، ')} مشکل دارد؛ قیمت قدیمی برای ورود استفاده نمی‌شود.</p>}
      {data.status.error&&<p className="bd-warning">پایش آماده نیست؛ وضعیت سرویس را بررسی کنید.</p>}
      {tab==='signals'&&<><label>وضعیت <select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">همهٔ سیگنال‌های بدون پوزیشن</option>{['waiting','review','cancelled','expired','invalidated'].map(x=><option key={x} value={x}>{labels[x]}</option>)}</select></label>{cards(data.signals.filter(s=>!['open','closed'].includes(s.status)&&(filter==='all'||filter===s.status)))}</>}
      {tab==='open'&&cards(data.signals.filter(s=>s.status==='open'))}
      {tab==='wallet'&&<div className="card">
        <div className="bd-grid">{[['موجودی',data.wallet.balance],['آزاد',data.wallet.available],['درگیر',data.wallet.reserved],['ارزش حساب',data.wallet.equity]].map(([k,v])=><div key={k}><span>{k}</span><b dir="ltr">{fmt(v as number)} USDT</b></div>)}</div>
        {!data.wallet.capital&&<form onSubmit={e=>{e.preventDefault();void action('capital',{amount:capital});}}><label>موجودی اولیهٔ مجازی (USDT)<input type="number" min="1" max="100000000" required value={capital} onChange={e=>setCapital(Number(e.target.value))}/></label><button disabled={busy}>ثبت موجودی اولیه</button></form>}
        <form onSubmit={e=>{e.preventDefault();void action('settings',form,'PUT');}} className="bd-form">
          <label>مارجین هر معامله (USDT)<input type="number" min="0.01" step="0.01" required value={form.margin} onChange={e=>setForm({...form,margin:Number(e.target.value)})}/></label>
          <label>لوریج<input type="number" min="1" max="125" required value={form.leverage} onChange={e=>setForm({...form,leverage:Number(e.target.value)})}/></label>
          <label>کارمزد هر سمت (bps؛ هر ۱ واحد = ۰٫۰۱٪)<input type="number" min="0" max="100" step="0.1" required value={form.fee_bps} onChange={e=>setForm({...form,fee_bps:Number(e.target.value)})}/></label>
          <label>اعتبار سیگنال، ساعت (۰ = بدون انقضای زمانی)<input type="number" min="0" max="720" required value={form.expiry_hours} onChange={e=>setForm({...form,expiry_hours:Number(e.target.value)})}/></label>
          <label className="bd-check"><input type="checkbox" checked={form.enabled} onChange={e=>setForm({...form,enabled:e.target.checked})}/>فعال‌سازی ورودهای جدید</label>
          <p className="hint">توقف ورود جدید، پایش پوزیشن‌های باز را متوقف نمی‌کند. مارجین و لوریج از این تنظیمات گرفته می‌شود؛ تغییر آن‌ها روی پوزیشن‌های باز اثر ندارد.</p>
          <button disabled={busy||!data.wallet.capital}>ذخیره تنظیمات</button>
        </form>
        <p className="hint">آغاز ثبت: {date(data.wallet.activated_at)}. تقسیم مساوی حجم بین اهداف؛ حدضرر ثابت. کارمزد پیش‌فرض یک فرض قابل تنظیم است. فاندینگ در نسخهٔ اول محاسبه نمی‌شود.</p>
      </div>}
      {tab==='report'&&<><div className="card"><div className="bd-grid">{[['رکوردهای کانال',data.report.total],['بسته‌شده',data.report.closed],['برد / باخت',`${data.report.wins} / ${data.report.losses}`],['وین‌ریت قطعی',data.report.winrate==null?'—':fmt(data.report.winrate)+'٪'],['خالص معاملات بسته‌شده',fmt(data.report.net)+' USDT'],['افت از سود تحقق‌یافته',fmt(data.report.realized_drawdown)+' USDT']].map(([k,v])=><div key={k}><span>{k}</span><b dir="ltr">{v}</b></div>)}</div>
        <p className="hint">وین‌ریت بر پایهٔ {data.report.certain_closed} معاملهٔ بسته‌شده با نتیجهٔ قابل اتکا است؛ {data.report.uncertain_closed} نتیجهٔ نامطمئن جدا شده. لمس TP1 به‌تنهایی برد کامل نیست. نمایش آخرین {data.history_limit} رکورد پایان‌یافته؛ خروجی شامل کل تاریخچه است.</p>
        <button disabled={busy} onClick={()=>void download()}>دانلود گزارش CSV</button>
        <div className="bd-table"><table><thead><tr><th>ارز</th><th>بسته‌شدهٔ قطعی</th><th>برد</th><th>خالص USDT</th></tr></thead><tbody>{Object.entries(data.report.by_symbol).map(([s,r])=><tr key={s}><td>{s}</td><td>{r.closed}</td><td>{r.wins}</td><td>{fmt(r.net)}</td></tr>)}</tbody></table></div>
      </div>{cards(data.signals.filter(s=>s.status==='closed'))}</>}
    </>}
  </section>;
}

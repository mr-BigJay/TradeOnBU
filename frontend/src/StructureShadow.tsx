import { useEffect, useState } from "react";
import { apiFetch } from "./api";

type Evidence = {key:string;variant:string;direction:string;reference:number;reference_ms:number;
  pivot:number;pivot_ms:number;extreme:number;sweep_ms:number;break_ms:number;fvg_ms:number;
  zone:number[];bars:number[][];token:string};
type Item = {evidence:Evidence;state:string;observed_ms:number;
  tracking:{entry:number;stop:number;tp?:number;net_usdt?:number;fill_ms?:number;touch_ms?:number;ready_ms?:number};
  review?:{verdict:string;reason:string;at_ms:number}};
type Data = {enabled:boolean;collector_status?:string;checked_ms?:string;items:Item[];counts:{variant:string;state:string;count:number;net_usdt:number|null}[]};
const labels:Record<string,string> = {ok:"داده آماده",not_run:"هنوز اجرا نشده",inactive_settings:"نیاز به تنظیمات Paper، استراتژی A و STRICT",invalid_or_incomplete_candles:"کندل نامعتبر یا تاریخچه ناقص",stale_candles:"کندل تازه دریافت نشده",collection_failed:"خطای جمع‌آوری داده",await_touch:"منتظر بازگشت",limit:"سفارش فرضی در انتظار",filled:"ورود فرضی",
  registration_ambiguous:"برخورد در دقیقهٔ ثبت؛ ترتیب نامشخص",late_touch:"برخورد پیش از شناسایی",invalidated:"ابطال ساختار",expired:"انقضا",cost_cap:"هزینه‌ها و سقف خام",
  obstacle:"مانع پیش از ۲R",data_gap:"شکاف یا کندل نامعتبر",target_before_fill:"هدف پیش از ورود",
  tp:"حد سود",sl:"حد ضرر",ambiguous:"ترتیب مبهم؛ خارج از سود قطعی"};
const time = (t:number) => new Date(t).toLocaleString("fa-IR",{timeZone:"Asia/Tehran"});
const variant = (v:string) => v==="swing"?"ساختار Swing":"تعریف پنجره‌ای فعلی";

function EvidenceChart({e}:{e:Evidence}) {
 const prices=[...e.bars.flatMap(b=>[b[2],b[3]]),e.reference,e.pivot,...e.zone];
 const low=Math.min(...prices),range=Math.max(...prices)-low || 1;
 const x=(t:number)=>45+(t-e.bars[0][0])/(e.bars[e.bars.length-1][0]-e.bars[0][0] || 1)*580;
 const y=(p:number)=>250-(p-low)/range*220;
 return <svg viewBox="0 0 820 300" role="img" aria-label="کندل‌های مرجع ستاپ" style={{width:"100%",background:"#111827",color:"#e5e7eb"}}>
  <rect x={x(e.fvg_ms)} y={y(e.zone[1])} width={Math.max(1,630-x(e.fvg_ms))} height={Math.max(2,y(e.zone[0])-y(e.zone[1]))} fill="#38bdf8" opacity=".25"/>
  {e.bars.map(b=><g key={b[0]} stroke={b[4]>=b[1]?"#34d399":"#fb7185"}>
   <line x1={x(b[0])} x2={x(b[0])} y1={y(b[2])} y2={y(b[3])}/>
   <rect x={x(b[0])-2} y={y(Math.max(b[1],b[4]))} width="4" height={Math.max(1,Math.abs(y(b[1])-y(b[4])))} fill="currentColor"/>
  </g>)}
  {([["Sweep level",e.reference],["Break level",e.pivot],["FVG low",e.zone[0]],["FVG high",e.zone[1]]] as [string,number][]).map(([label,p])=>
    <g key={label}><line x1="30" x2="630" y1={y(p)} y2={y(p)} stroke="#94a3b8" strokeDasharray="4 4"/>
     <text x="640" y={y(p)+4} fill="white" fontSize="12">{label} {p.toFixed(1)}</text></g>)}
  {[[e.sweep_ms,"Sweep"],[e.break_ms,e.token],[e.fvg_ms,"FVG"]] .map(([t,label],i)=>
   <g key={i}><line x1={x(Number(t))} x2={x(Number(t))} y1="20" y2="255" stroke="#fbbf24" strokeDasharray="2 5"/>
    <text x={x(Number(t))} y={270+i*12} fill="#fbbf24" fontSize="11" textAnchor="middle">{label}</text></g>)}
 </svg>;
}
export function StructureShadow(){
 const [data,setData]=useState<Data|null>(null),[error,setError]=useState(""),[selected,setSelected]=useState<Item|null>(null);
 useEffect(()=>{
  let alive=true;
  async function refresh(){try{const d=await apiFetch("/api/gui/opportunities/structure-shadow") as Data;
    if (!Array.isArray(d.items) || !Array.isArray(d.counts)) throw new Error("پاسخ آزمایش ساختار معتبر نیست");
    if(alive){setData(d);setError("");}}catch(e){if(alive)setError(String(e));}}
  void refresh();const id=setInterval(()=>void refresh(),60000);
  return ()=>{alive=false;clearInterval(id);};
 },[]);
 return <section className="card">
  <h2>آزمایش ساختار A</h2>
  <p>دفتر مشاهدهٔ موازی · ساعت تهران · فرضیهٔ مستقل برای هر ستاپ؛ سود تجمیعی معادل بازده حساب نیست.</p>
  {error&&<p role="alert">{error}</p>}
  {data&&!data.enabled&&<p>ثبت آزمایشی غیرفعال است. برای فعال‌سازی ICT_STRUCTURE_SHADOW=1 تنظیم شود.</p>}
  {data&&<><p>وضعیت داده · {labels[data.collector_status||"not_run"]||data.collector_status} {data.checked_ms?time(Number(data.checked_ms)):""}</p><p>شواهد با کندل‌های بسته ثبت می‌شوند. «برخورد پیش از شناسایی» ورود محسوب نمی‌شود. ابهام و شکاف داده سود قطعی ندارند.</p>
   <table><thead><tr><th>تعریف</th><th>نتیجه</th><th>تعداد</th><th>خالص USDT</th></tr></thead>
    <tbody>{data.counts?.map(c=><tr key={c.variant+c.state}><td>{variant(c.variant)}</td><td>{labels[c.state]||c.state}</td>
     <td>{c.count}</td><td>{c.net_usdt?.toFixed(3)??"—"}</td></tr>)}</tbody></table>
   {!data.items?.length&&<p>هنوز شواهدی ثبت نشده است.</p>}
   {data.items?.map(item=><button type="button" key={item.evidence.key} onClick={()=>setSelected(item)} style={{display:"block",margin:"8px 0"}}>
    {variant(item.evidence.variant)} · {item.evidence.direction} · {labels[item.state]||item.state} · {time(item.observed_ms)}
   </button>)}
  </>}
  {selected&&<article>
   <h3>{variant(selected.evidence.variant)} · {selected.evidence.direction}</h3>
   <EvidenceChart e={selected.evidence}/>
   <p>Sweep {time(selected.evidence.sweep_ms)} · شکست {time(selected.evidence.break_ms)} · ثبت {time(selected.observed_ms)}</p>
   <p>محدوده {selected.evidence.zone.join(" – ")} · ورود فرضی {selected.tracking.entry} · SL {selected.tracking.stop} · TP {selected.tracking.tp??"منتظر تأیید"}</p>
   <p>AI {selected.review?.verdict??"بررسی نشده"} · {selected.review?.reason}</p>
   {selected.review&&<p>زمان دریافت نظر {time(selected.review.at_ms)} ·
    {selected.tracking.touch_ms&&selected.review.at_ms>=selected.tracking.touch_ms?"نظر پس از برخورد دریافت شده است":"نظر به آینده اعمال می‌شود"}</p>}
   <button type="button" onClick={()=>setSelected(null)}>بستن جزئیات</button>
  </article>}
 </section>;
}

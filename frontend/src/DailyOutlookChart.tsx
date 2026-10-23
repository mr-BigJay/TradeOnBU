import { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, LineStyle, type UTCTimestamp } from 'lightweight-charts';
import { chartCandles, scenarioPath, DAY_SECONDS, type ChartScenario, type OutlookChartContext } from './dailyOutlookChartData';

export function DailyOutlookChart({ context, dominant, alternative }: {
  context: OutlookChartContext; dominant: ChartScenario; alternative: ChartScenario;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [showAlternative, setShowAlternative] = useState(true);
  const [error, setError] = useState('');
  // Polling returns fresh objects every 15s. Stable content avoids resetting user pan/zoom.
  const snapshot = JSON.stringify({context, dominant, alternative});
  useEffect(() => {
    if (!host.current) return;
    const data = JSON.parse(snapshot) as {context: OutlookChartContext; dominant: ChartScenario; alternative: ChartScenario};
    const {candles, interval} = chartCandles(data.context);
    if (!candles.length) { setError('کندل معتبر برای این گزارش موجود نیست.'); return; }
    let chart: ReturnType<typeof createChart> | undefined;
    let observer: ResizeObserver | undefined;
    try {
      setError('');
      chart = createChart(host.current, {
        width: host.current.clientWidth, height: 460,
        layout: {background:{type:ColorType.Solid,color:'#0f192a'},textColor:'#bccbe0',fontFamily:'Tahoma, sans-serif'},
        grid:{vertLines:{color:'#1d2b40'},horzLines:{color:'#1d2b40'}},
        rightPriceScale:{borderColor:'#33445d'},
        timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#33445d'},
        localization:{locale:'fa-IR',timeFormatter:(t:number)=>new Date(t*1000).toLocaleString('fa-IR',{timeZone:'Asia/Tehran',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})},
      });
      const series = chart.addCandlestickSeries({upColor:'#52d5ae',downColor:'#f27e91',wickUpColor:'#52d5ae',wickDownColor:'#f27e91',borderVisible:false,priceLineVisible:false});
      series.setData(candles.map(b=>({...b,time:b.time as UTCTimestamp})));
      const anchor = Math.floor(Date.parse(data.context.as_of)/1000);
      const end = anchor+7*DAY_SECONDS;
      if (Number.isFinite(anchor)) {
        // Whitespace preserves the 4h/day cadence across the future region.
        const timeline = chart.addLineSeries({visible:false});
        const times = new Set<number>([anchor,end]);
        for(let t=candles[candles.length-1].time+interval;t<=end;t+=interval) times.add(t);
        timeline.setData([...times].sort((a,b)=>a-b).map(time=>({time:time as UTCTimestamp})));
      }
      const scenarios = [{s:data.dominant,color:'#58dcba',name:'غالب'}, ...(showAlternative?[{s:data.alternative,color:'#e3b16a',name:'جایگزین'}]:[])];
      for (const {s,color,name} of scenarios) {
        const path = scenarioPath(data.context,s);
        if (!path.length) continue;
        const line = chart.addLineSeries({color,lineWidth:2,lineStyle:LineStyle.Dashed,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:true,title:name});
        line.setData(path.map(p=>({time:p.time as UTCTimestamp,value:p.value})));
        line.setMarkers([{time:path[0].time as UTCTimestamp,position:'inBar',shape:'circle',color,text:'شروع سناریو'},
          {time:path[path.length-1].time as UTCTimestamp,position:'aboveBar',shape:'circle',color,text:`${name} · افق ۷ روز`}]);
      }
      const references = new Set([
        ...data.dominant.activation_level_ids,...data.dominant.invalidation_level_ids,...data.dominant.target_level_ids,
        ...(showAlternative?[...data.alternative.activation_level_ids,...data.alternative.invalidation_level_ids,...data.alternative.target_level_ids]:[]),
      ]);
      const prices = new Set<number>();
      for (const level of data.context.levels) {
        if (!references.has(level.id) || prices.has(level.price) || !Number.isFinite(level.price) || level.price<=0) continue;
        prices.add(level.price);
        series.createPriceLine({price:level.price,color:'#8d9fbb',lineWidth:1,lineStyle:LineStyle.Dotted,axisLabelVisible:true,title:level.label_fa});
      }
      chart.timeScale().fitContent();
      const liveChart=chart;
      observer=new ResizeObserver(entries=>liveChart.applyOptions({width:entries[0].contentRect.width}));
      observer.observe(host.current);
    } catch { setError('نمایش چارت ممکن نشد؛ صفحه را دوباره بارگذاری کنید.'); }
    return ()=>{observer?.disconnect();chart?.remove();};
  }, [snapshot,showAlternative]);
  const fourHour=!!context.four_hour_candles?.length;
  return <div className="outlook-trading-chart">
    <div className="outlook-chart-toolbar"><strong>چارت واقعی Bitunix · {fourHour?'۴ساعته':'روزانه'}</strong>
      <label><input type="checkbox" checked={showAlternative} onChange={e=>setShowAlternative(e.target.checked)}/> نمایش مسیر جایگزین</label></div>
    <div ref={host} dir="ltr" className="outlook-chart-canvas" role="img" aria-label="کندل‌های واقعی و مسیر مشروط سناریوها تا افق هفت‌روزه"/>
    {error && <p role="alert">{error}</p>}
    <p className="hint"><span style={{color:'#58dcba'}}>خط سبز: سناریوی غالب</span> · <span style={{color:'#e3b16a'}}>خط نارنجی: جایگزین</span> · خطوط افقی: سطح‌های تحلیل</p>
    <p className="hint">دادهٔ واقعیِ ذخیره‌شده هنگام این گزارش؛ چارت زنده نیست. خط‌چین‌ها اتصال سطح‌های سناریو تا افق ۷ روز از زمان گزارش‌اند. فاصلهٔ زمانی نقاط شماتیک است و زمان رسیدن قیمت به هدف را پیش‌بینی نمی‌کند.</p>
    <p className="hint">نمودار با <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a> · محور زمان UTC؛ زمان نشانگر به وقت تهران</p>
    <p className="hint">TradingView Lightweight Charts™ · Copyright (c) 2025 TradingView, Inc.</p>
  </div>;
}

/**
 * Browser proof: overlay box matches plot area; shade stays above time scale.
 * Run: node scripts/verify-forecast-overlay-bounds.mjs
 */
import { chromium } from "playwright";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const PRICE_SCALE_W = 56;
const TIME_SCALE_H = 28;

const html = `<!doctype html><html><head><meta charset="utf-8"/>
<script src="https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js"></script>
<style>
body{margin:0;background:#0c1224}
#wrap{position:relative;height:420px;width:960px;margin:0 auto}
#chart{position:relative;z-index:1;height:420px;width:100%}
#overlays{position:absolute;top:0;left:0;z-index:5;pointer-events:none;overflow:hidden}
#shade{position:absolute;top:0;bottom:0;background:rgba(0,229,255,.12)}
#boundary{position:absolute;top:0;bottom:0;border-left:1px dashed rgba(0,229,255,.65)}
</style></head><body>
<div id="wrap"><div id="chart"></div><div id="overlays"><div id="shade"></div><div id="boundary"></div></div></div>
<script>
const PRICE_SCALE_W = ${PRICE_SCALE_W};
const TIME_SCALE_H = ${TIME_SCALE_H};
const { createChart, LineStyle } = LightweightCharts;
const el = document.getElementById('chart');
const chart = createChart(el, { height:420, layout:{background:{color:'#0c1224'}}, timeScale:{rightOffset:4, barSpacing:12}});
const cs = chart.addCandlestickSeries();
const anchorSec = Math.floor(new Date('2026-06-01T10:30:00Z').getTime()/1000);
const candles = Array.from({length:120},(_,i)=>({time:anchorSec-(119-i)*3600, open:8e4, high:80100, low:79900, close:80000}));
cs.setData(candles);
const line = chart.addLineSeries({ color:'#00e5ff', lineWidth:2, lineStyle: LineStyle.LargeDashed });
const fps = [11,12,13,14,15,16].map((h,i)=>({time:Math.floor(new Date('2026-06-01T'+String(h).padStart(2,'0')+':00:00Z').getTime()/1000), value:80000+(i+1)*20}));
line.setData([{time:anchorSec,value:80000},...fps]);
chart.timeScale().setVisibleLogicalRange({ from: 106, to: 127 });
function layout(){
  const overlays = document.getElementById('overlays');
  const pw = chart.priceScale('right').width();
  const th = chart.timeScale().height();
  overlays.style.top='0';
  overlays.style.left='0';
  overlays.style.right = pw+'px';
  overlays.style.bottom = th+'px';
  const plotW = chart.timeScale().width();
  const ax = chart.timeScale().timeToCoordinate(anchorSec);
  const shade = document.getElementById('shade');
  if(ax==null){ shade.style.width='0'; return; }
  shade.style.left = ax+'px';
  shade.style.width = (overlays.clientWidth - ax)+'px';
  document.getElementById('boundary').style.left = ax+'px';
}
layout();
chart.timeScale().subscribeVisibleLogicalRangeChange(layout);
window.__chart = chart;
window.__ready = true;
</script></body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 980, height: 760 } });
await page.setContent(html, { waitUntil: "networkidle" });
await page.waitForFunction(() => window.__ready === true);
await page.waitForTimeout(400);

const metrics = await page.evaluate(() => {
  const chart = window.__chart;
  const wrap = document.getElementById("wrap");
  const overlays = document.getElementById("overlays");
  const shade = document.getElementById("shade");
  const w = wrap.getBoundingClientRect();
  const o = overlays.getBoundingClientRect();
  const s = shade.getBoundingClientRect();
  const pw = chart.priceScale("right").width();
  const th = chart.timeScale().height();
  return {
    overlayBottom: o.bottom,
    overlayRight: o.right,
    shadeBottom: s.bottom,
    shadeRight: s.right,
    expectedOverlayBottom: w.bottom - th,
    expectedOverlayRight: w.right - pw,
    priceScaleWidth: pw,
    timeScaleHeight: th,
  };
});

await browser.close();

const tol = 2;
const okOverlayBottom = Math.abs(metrics.overlayBottom - metrics.expectedOverlayBottom) <= tol;
const okOverlayRight = Math.abs(metrics.overlayRight - metrics.expectedOverlayRight) <= tol;
const okShadeAboveTime = metrics.shadeBottom <= metrics.overlayBottom + tol;
const okShadeInsidePlot = metrics.shadeRight <= metrics.overlayRight + tol;

if (!okOverlayBottom || !okOverlayRight || !okShadeAboveTime || !okShadeInsidePlot) {
  console.error("Overlay bounds verification failed:", metrics);
  process.exit(1);
}
console.log("Overlay bounds OK (getBoundingClientRect):", metrics);

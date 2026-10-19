/**
 * Visual smoke: headless Chromium screenshot of forecast logical range.
 * Run: node scripts/capture-forecast-chart.mjs
 */
import { chromium } from "playwright";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outDir = join(__dirname, "../../artifacts/forecast-chart");
mkdirSync(outDir, { recursive: true });

const anchor = "2026-06-01T10:30:00Z";
const anchorSec = Math.floor(new Date(anchor).getTime() / 1000);
const candles = Array.from({ length: 120 }, (_, i) => ({
  time_ms: (anchorSec - (119 - i) * 3600) * 1000,
  open: 80000 + i * 2,
  high: 80100 + i * 2,
  low: 79900 + i * 2,
  close: 80000 + i * 2,
}));
const forecast_points = [11, 12, 13, 14, 15, 16].map((hour, i) => ({
  time: `2026-06-01T${String(hour).padStart(2, "0")}:00:00Z`,
  price: 80000 + (i + 1) * 25,
}));

const html = `<!doctype html><html><head><meta charset="utf-8"/>
<script src="https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js"></script>
<style>
body{margin:0;background:#0c1224}
#wrap{position:relative;height:420px;max-width:960px;margin:0 auto}
#chart{position:relative;z-index:1;height:420px}
#overlays{position:absolute;inset:0;z-index:5;pointer-events:none;padding-right:56px;padding-bottom:28px;box-sizing:border-box}
#shade{position:absolute;top:0;bottom:0;background:linear-gradient(90deg,rgba(0,229,255,.05),rgba(0,120,180,.14))}
#boundary{position:absolute;top:0;bottom:0;border-left:1px dashed rgba(0,229,255,.65)}
#label{position:absolute;top:10px;font:600 10px sans-serif;letter-spacing:.14em;color:rgba(0,229,255,.82)}
</style></head><body>
<div id="wrap"><div id="chart"></div><div id="overlays"><div id="shade"></div><div id="boundary"></div><div id="label">AI FORECAST</div></div></div>
<script>
const payload = ${JSON.stringify({ candles, anchor_price: 80000, anchor_time: anchor, forecast_points })};
const { createChart, LineStyle } = LightweightCharts;
const el = document.getElementById('chart');
const chart = createChart(el, { height:420, layout:{background:{color:'#0c1224'}, textColor:'#d5def5'}, timeScale:{rightOffset:4, barSpacing:12}});
const cs = chart.addCandlestickSeries({ upColor:'#26a69a', downColor:'#ef5350', borderVisible:false });
cs.setData(payload.candles.map(c=>({time:Math.floor(c.time_ms/1000), open:c.open, high:c.high, low:c.low, close:c.close})));
const line = chart.addLineSeries({ color:'#00e5ff', lineWidth:2, lineStyle: LineStyle.LargeDashed, lastValueVisible:false, priceLineVisible:false });
const anchorT = Math.floor(new Date(payload.anchor_time).getTime()/1000);
const path = [{time:anchorT, value:payload.anchor_price}, ...payload.forecast_points.map(p=>({time:Math.floor(new Date(p.time).getTime()/1000), value:p.price}))];
line.setData(path);
line.setMarkers(payload.forecast_points.map(p=>({time:Math.floor(new Date(p.time).getTime()/1000), position:'inBar', shape:'circle', color:'#00e5ff', size:1})));
const last = payload.candles.length - 1;
chart.timeScale().setVisibleLogicalRange({ from: last - 13, to: last + 8 });
function layout(){
  const plotW = chart.timeScale().width();
  const x = chart.timeScale().timeToCoordinate(anchorT);
  if(x==null||x<0||x>plotW){ document.getElementById('shade').style.width='0'; return; }
  document.getElementById('shade').style.left = x+'px';
  document.getElementById('shade').style.width = (plotW-x)+'px';
  document.getElementById('boundary').style.left = x+'px';
  document.getElementById('label').style.left = (x+10)+'px';
}
layout();
chart.timeScale().subscribeVisibleLogicalRangeChange(layout);
window.__chartReady = true;
</script></body></html>`;

const browser = await chromium.launch();
for (const [name, width] of [
  ["desktop", 1280],
  ["mobile", 390],
]) {
  const page = await browser.newPage({ viewport: { width, height: 720 } });
  await page.setContent(html, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__chartReady === true);
  await page.waitForTimeout(500);
  await page.locator("#wrap").screenshot({ path: join(outDir, `${name}-forecast-chart.png`) });
  await page.close();
}
await browser.close();
writeFileSync(join(outDir, "README.txt"), "Playwright capture of forecast chart layout (logical range + overlays).\n");
console.log("Saved screenshots to", outDir);

export type OutlookLevel = { id: string; label_fa: string; price: number };
export type ChartScenario = {
  activation_level_ids: string[]; target_level_ids: string[];
  invalidation_level_ids: string[]; path_level_ids?: string[];
};
export type OutlookChartContext = {
  as_of: string; live_price: number; levels: OutlookLevel[];
  four_hour_candles?: number[][]; daily_candles: number[][];
};
export const DAY_SECONDS = 86400;
export function chartCandles(context: OutlookChartContext) {
  const fourHour = !!context.four_hour_candles?.length;
  const source = fourHour ? context.four_hour_candles! : context.daily_candles;
  const unique = new Map<number, {time: number; open: number; high: number; low: number; close: number}>();
  const anchor = Date.parse(context.as_of) / 1000;
  for (const b of source || []) {
    if (b.length < 5 || !b.slice(0,5).every(Number.isFinite)) continue;
    const [ms, open, high, low, close] = b;
    if (Math.min(open, high, low, close) <= 0 || low > Math.min(open, close) || high < Math.max(open, close)) continue;
    const time = Math.floor(ms / 1000);
    if (time + (fourHour ? 14400 : DAY_SECONDS) > anchor) continue;
    unique.set(time, { time, open, high, low, close });
  }
  return { candles: [...unique.values()].sort((a,b)=>a.time-b.time), interval: fourHour ? 14400 : DAY_SECONDS };
}
export function scenarioPath(context: OutlookChartContext, scenario: ChartScenario) {
  const anchor = Math.floor(Date.parse(context.as_of) / 1000);
  if (!Number.isFinite(anchor) || !Number.isFinite(context.live_price) || context.live_price <= 0) return [];
  const levels = new Map(context.levels.filter(l=>Number.isFinite(l.price) && l.price>0).map(l=>[l.id,l]));
  // Older archived reports have no explicit sequence: clearly labelled schematic fallback.
  const ids = scenario.path_level_ids?.length ? scenario.path_level_ids : [...scenario.activation_level_ids, ...scenario.target_level_ids];
  const selected: OutlookLevel[] = [];
  for (const id of ids) {
    const l = levels.get(id);
    if (!l) return []; // Never silently invent or skip an unknown waypoint.
    if (selected[selected.length-1]?.price !== l.price) selected.push(l);
  }
  if (!selected.length) return [];
  return [{time:anchor, value:context.live_price, label:'قیمت هنگام گزارش'},
    ...selected.map((l,i)=>({time:anchor+Math.round((i+1)/selected.length*7*DAY_SECONDS), value:l.price, label:l.label_fa}))];
}

import { describe,it,expect } from 'vitest';
import {chartCandles,scenarioPath,DAY_SECONDS} from './dailyOutlookChartData';
const anchor=Date.parse('2026-10-23T12:00:00Z')/1000;
const context={as_of:'2026-10-23T12:00:00Z',live_price:100,levels:[{id:'a',label_fa:'تأیید',price:110},{id:'b',label_fa:'هدف',price:120}],daily_candles:[],four_hour_candles:[[ (anchor-14400)*1000,100,110,90,105,10]]};
const scenario={activation_level_ids:['a'],target_level_ids:['b'],invalidation_level_ids:[]};
describe('daily outlook chart data',()=>{
 it('uses actual OHLC without adding future candles',()=>{
  const r=chartCandles(context); expect(r.interval).toBe(14400);expect(r.candles).toEqual([{time:anchor-14400,open:100,high:110,low:90,close:105}]);
 });
 it('rejects forming, malformed and nonfinite bars and deduplicates timestamps',()=>{
  const b=context.four_hour_candles[0];
  const r=chartCandles({...context,four_hour_candles:[b,b,[anchor*1000,100,110,90,105],[(anchor-28800)*1000,100,90,80,105],[NaN,1,2,1,2]]});
  expect(r.candles).toHaveLength(1);
 });
 it('anchors to report time/quote, uses only referenced levels, ends exactly seven days later',()=>{
  const path=scenarioPath(context,scenario);expect(path.map(p=>p.value)).toEqual([100,110,120]);expect(path[0].time).toBe(anchor);expect(path.at(-1)?.time).toBe(anchor+7*DAY_SECONDS);
 });
 it('preserves explicit AI level order and retracements',()=>{
  expect(scenarioPath(context,{...scenario,path_level_ids:['a','b','a','b']}).map(p=>p.value)).toEqual([100,110,120,110,120]);
 });
 it('does not manufacture values for missing levels or quotes',()=>{
  expect(scenarioPath(context,{...scenario,target_level_ids:['unknown']})).toEqual([]);
  expect(scenarioPath({...context,live_price:NaN},scenario)).toEqual([]);
 });
 it('falls back to archived daily candles',()=>{
  const r=chartCandles({...context,four_hour_candles:undefined,daily_candles:[[(anchor-DAY_SECONDS)*1000,100,110,90,105]]});
  expect(r.interval).toBe(DAY_SECONDS);expect(r.candles).toHaveLength(1);
 });
});

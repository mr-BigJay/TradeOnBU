import {render,screen,fireEvent,cleanup} from '@testing-library/react';
import {vi,it,expect,afterEach} from 'vitest';
import {DailyOutlookChart} from './DailyOutlookChart';
const mocks=vi.hoisted(()=>({candles:vi.fn(),lines:vi.fn(),remove:vi.fn(),create:vi.fn(),price:vi.fn()}));
vi.mock('lightweight-charts',()=>({ColorType:{Solid:'solid'},LineStyle:{Dashed:2,Dotted:1},createChart:(...args:unknown[])=>{mocks.create(...args);return {
 addCandlestickSeries:()=>({setData:mocks.candles,createPriceLine:mocks.price}),
 addLineSeries:()=>({setData:mocks.lines,setMarkers:vi.fn()}),timeScale:()=>({fitContent:vi.fn()}),applyOptions:vi.fn(),remove:mocks.remove,
};}}));
const context={as_of:'2026-10-23T12:00:00Z',live_price:100,levels:[{id:'a',label_fa:'سطح',price:110}],daily_candles:[],four_hour_candles:[[1792732800000,100,110,90,105]]};
const scenario={activation_level_ids:['a'],target_level_ids:['a'],invalidation_level_ids:['a']};
afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals();});
it('renders snapshot candles, paths and horizontal levels; preserves zoom across identical polling and cleans up',()=>{
 vi.stubGlobal('ResizeObserver',class {observe(){} disconnect(){}});
 const props={context,dominant:scenario,alternative:scenario};
 const view=render(<DailyOutlookChart {...props}/>);
 expect(mocks.candles).toHaveBeenCalledTimes(1);expect(mocks.price).toHaveBeenCalledTimes(1);expect(mocks.lines).toHaveBeenCalledTimes(3);
 expect(screen.getByText(/دادهٔ واقعیِ ذخیره‌شده/)).toBeInTheDocument();
 view.rerender(<DailyOutlookChart {...JSON.parse(JSON.stringify(props))}/>);
 expect(mocks.create).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('checkbox'));
 expect(mocks.remove).toHaveBeenCalledTimes(1);expect(mocks.lines).toHaveBeenCalledTimes(5);
 view.unmount();expect(mocks.remove).toHaveBeenCalledTimes(2);
});

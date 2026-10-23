import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DailyOutlookPage } from './DailyOutlookPage';
const apiFetch=vi.fn();
vi.mock('./api',()=>({apiFetch:(...args:unknown[])=>apiFetch(...args)}));
const scenario={title_fa:'بررسی حفظ حمایت',direction:'bullish',thesis_fa:'سناریوی مشروط',activation_fa:'بسته شدن روزانه بالای سطح',invalidation_fa:'بسته شدن زیر سطح',activation_level_ids:['level'],invalidation_level_ids:['level'],target_level_ids:['level'],path_fa:['تأیید شکست','بازآزمایی سطح']};
const run={id:1,report_date:'2026-10-22',symbol:'BTCUSDT',status:'completed',stale:true,model:'test',report:{summary_fa:'خلاصه گزارش آزمایشی',weekly_context_fa:'ساختار هفتگی',daily_structure_fa:'ساختار روزانه',dominant:scenario,alternative:{...scenario,direction:'bearish'},uncertainty_fa:'ابهام',watch_next_fa:['بررسی کندل'],change_since_previous_fa:'بدون تغییر'},context:{as_of:'2026-10-22T06:00:00Z',live_price:81000,levels:[{id:'level',label_fa:'کف روز قبل',price:80000}],daily_candles:[]}};
const empty=()=>({settings:{enabled:false,hour:8,minute:5,next_run_at:null},latest:null,latest_completed:null,history:[],attempted_today:false});
beforeEach(()=>{ apiFetch.mockReset(); window.location.hash=""; });
afterEach(cleanup);
it('shows a timeline without automatically opening the latest report',async()=>{
 apiFetch.mockResolvedValue({...empty(),latest_completed:run,history:[run]});
 render(<DailyOutlookPage/>);
 expect(await screen.findByRole('link',{name:'مشاهده گزارش و چارت ←'})).toHaveAttribute('href','#/outlook/reports/1');
 expect(screen.queryByText('سناریوی غالب')).not.toBeInTheDocument();
 expect(apiFetch).not.toHaveBeenCalledWith('/api/gui/daily-outlook/runs/1/chart-image');
});
it('opens a deep linked report with its static chart and both scenarios',async()=>{
 window.location.hash='#/outlook/reports/1';
 apiFetch.mockImplementation((url:string)=>Promise.resolve(url.endsWith('/chart-image')?{data_url:'data:image/png;base64,abc',filename:'report.png'}:url.endsWith('/runs/1')?run:empty()));
 render(<DailyOutlookPage/>);
 expect(await screen.findByText('سناریوی غالب')).toBeInTheDocument();
 expect(screen.getByText('سناریوی جایگزین')).toBeInTheDocument();
 expect(await screen.findByRole('img')).toHaveAttribute('src','data:image/png;base64,abc');
 expect(screen.getByRole('link',{name:'دانلود تصویر چارت'})).toHaveAttribute('download','report.png');
 expect(screen.getByText(/این گزارش مربوط به روز گذشته/)).toBeInTheDocument();
 window.location.hash='#/outlook'; window.dispatchEvent(new HashChangeEvent('hashchange'));
 await waitFor(()=>expect(screen.queryByRole('img')).not.toBeInTheDocument());
});
it('does not substitute a different report when a detail request fails',async()=>{
 window.location.hash='#/outlook/reports/99';
 apiFetch.mockImplementation((url:string)=>url.endsWith('/runs/99')?Promise.reject(new Error('گزارش پیدا نشد')):Promise.resolve({...empty(),latest_completed:run,history:[run]}));
 render(<DailyOutlookPage/>);
 expect(await screen.findByText('گزارش پیدا نشد')).toBeInTheDocument();
 expect(screen.queryByText('سناریوی غالب')).not.toBeInTheDocument();
});
it('retains report text when image rendering fails and offers retry',async()=>{
 window.location.hash='#/outlook/reports/1';
 apiFetch.mockImplementation((url:string)=>url.endsWith('/chart-image')?Promise.reject(new Error('تصویر نامعتبر')):Promise.resolve(url.endsWith('/runs/1')?run:empty()));
 render(<DailyOutlookPage/>);
 expect(await screen.findByText('سناریوی غالب')).toBeInTheDocument();
 fireEvent.click(await screen.findByRole('button',{name:'دریافت دوباره تصویر'}));
 await waitFor(()=>expect(apiFetch.mock.calls.filter(c=>c[0].endsWith('/chart-image'))).toHaveLength(2));
});
it('saves editable daily time independently and starts one manual generation',async()=>{
 apiFetch.mockResolvedValue(empty()); render(<DailyOutlookPage/>);
 await screen.findByText('اولین چشم‌انداز را بسازید');
 fireEvent.click(screen.getByRole('checkbox'));
 fireEvent.change(screen.getByLabelText('ساعت تهران'),{target:{value:'09:15'}});
 fireEvent.click(screen.getByRole('button',{name:'ذخیره زمان‌بندی'}));
 await waitFor(()=>expect(apiFetch).toHaveBeenCalledWith('/api/gui/daily-outlook/settings',{method:'PUT',body:JSON.stringify({enabled:true,hour:9,minute:15})}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'تولید گزارش امروز'})).not.toBeDisabled());
 fireEvent.click(screen.getByRole('button',{name:'تولید گزارش امروز'}));
 await waitFor(()=>expect(apiFetch).toHaveBeenCalledWith('/api/gui/daily-outlook/generate',{method:'POST'}));
});
it('allows manual retry when backend confirms a pre-AI failure',async()=>{
 apiFetch.mockResolvedValue({...empty(),attempted_today:true,can_retry_today:true,latest:{status:'failed',report_date:'2026-10-23',error_code:'invalid_candle_range'}});
 render(<DailyOutlookPage/>);
 const button=await screen.findByRole('button',{name:'تلاش مجدد گزارش امروز'});
 expect(button).not.toBeDisabled(); fireEvent.click(button);
 await waitFor(()=>expect(apiFetch).toHaveBeenCalledWith('/api/gui/daily-outlook/generate',{method:'POST'}));
});

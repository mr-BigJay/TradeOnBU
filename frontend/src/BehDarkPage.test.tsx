import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { BehDarkPage } from './BehDarkPage';
import { apiFetch } from './api';
vi.mock('./api',()=>({apiFetch:vi.fn(),getApiKey:()=>'',getCsrf:()=>''}));
afterEach(cleanup);
const snapshot={wallet:{capital:1000,balance:1000,available:1000,reserved:0,equity:1000,unrealized:0,margin:30,leverage:4,fee_bps:6,expiry_hours:0,enabled:true,activated_at:1800000000},signals:[],quotes:{},report:{total:0,closed:0,certain_closed:0,wins:0,losses:0,breakeven:0,winrate:null,uncertain_closed:0,net:0,fees:0,realized_drawdown:0,by_symbol:{}},status:{reader_fresh:true,active_symbols:[],heartbeat:1800000000},history_limit:200};
beforeEach(()=>{vi.mocked(apiFetch).mockReset().mockResolvedValue(snapshot);});
it('shows four independent tabs and keeps empty winrate unknown',async()=>{
 render(<BehDarkPage/>);await screen.findByText('ورود جدید: فعال',{exact:false});
 for(const t of ['سیگنال‌ها','پوزیشن‌های باز','کیف پول','گزارش'])expect(screen.getByRole('button',{name:t})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'گزارش'}));expect(screen.getByText('وین‌ریت قطعی')).toBeTruthy();
 expect(screen.getByText('—')).toBeTruthy();
});
it('saves wallet settings only through the BehDark endpoint',async()=>{
 render(<BehDarkPage/>);await screen.findByText('ورود جدید: فعال',{exact:false});fireEvent.click(screen.getByRole('button',{name:'کیف پول'}));
 expect(screen.getByLabelText('لوریج')).toHaveValue(4);
 fireEvent.change(screen.getByLabelText('لوریج'),{target:{value:'5'}});fireEvent.click(screen.getByRole('button',{name:'ذخیره تنظیمات'}));
 await waitFor(()=>expect(apiFetch).toHaveBeenCalledWith('/api/gui/behdark/settings',expect.objectContaining({method:'PUT',body:expect.stringContaining('"leverage":5')})));
 expect(vi.mocked(apiFetch).mock.calls.every(([path])=>path.startsWith('/api/gui/behdark/'))).toBe(true);
});
it('shows service unavailable without hiding the page',async()=>{
 vi.mocked(apiFetch).mockRejectedValue(new Error('سرویس در دسترس نیست'));render(<BehDarkPage/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('سرویس در دسترس نیست');expect(screen.getByRole('heading',{name:'BehDark'})).toBeTruthy();
});

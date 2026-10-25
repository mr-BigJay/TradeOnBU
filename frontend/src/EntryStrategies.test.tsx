import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { EntryStrategies } from './EntryStrategies';
const apiFetch=vi.fn();
vi.mock('./api',()=>({apiFetch:(...args:unknown[])=>apiFetch(...args)}));
const all={ict_reversal:true,trend_continuation:true,breakout_retest:true};
beforeEach(()=>apiFetch.mockReset());afterEach(cleanup);
it('saves independent switches without changing frequency',async()=>{
 apiFetch.mockResolvedValue(all);render(<EntryStrategies/>);
 const b=await screen.findByLabelText(/B · Trend Continuation/);
 fireEvent.click(b);fireEvent.click(screen.getByRole('button',{name:'ذخیره استراتژی‌ها'}));
 await waitFor(()=>expect(apiFetch).toHaveBeenCalledWith('/api/gui/chatgpt/strategies/settings',{method:'PUT',body:JSON.stringify({...all,trend_continuation:false})}));
 expect(apiFetch.mock.calls.every(([url])=>String(url).includes('/strategies'))).toBe(true);
});
it('requires at least one strategy',async()=>{
 apiFetch.mockResolvedValue(all);render(<EntryStrategies/>);await screen.findByLabelText(/B · Trend Continuation/);
 screen.getAllByRole('checkbox').forEach(box=>fireEvent.click(box));
 expect(screen.getByRole('button',{name:'ذخیره استراتژی‌ها'})).toBeDisabled();
});
it('shows save failure without claiming success',async()=>{
 apiFetch.mockResolvedValueOnce(all).mockRejectedValueOnce(new Error('دسترسی مدیر لازم است'));
 render(<EntryStrategies/>);await screen.findByLabelText(/B · Trend Continuation/);
 fireEvent.click(screen.getByRole('button',{name:'ذخیره استراتژی‌ها'}));
 expect(await screen.findByRole('status')).toHaveTextContent('دسترسی مدیر لازم است');
});

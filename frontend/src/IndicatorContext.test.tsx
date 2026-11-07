import { cleanup,render,screen } from '@testing-library/react';
import { afterEach,it,expect } from 'vitest';
import { IndicatorContext } from './IndicatorContext';
afterEach(cleanup);
it('shows the exact saved context without buy/sell permission',()=>{
 render(<IndicatorContext features={{version:2,timeframes:{'15m':{available:true,ema9:100,ema20:99,rsi14:72.5,rsi14_change:-2,ema9_ema20_relation:'above',ema9_ema20_cross:'none',last_closed_open_ms:1794060000000}}}}/>);
 expect(screen.getByText('72.5')).toBeInTheDocument();
 expect(screen.getByText('EMA9 بالاتر از EMA20')).toBeInTheDocument();
 expect(screen.getByText(/شرط اجباری ورود نیستند/)).toBeInTheDocument();
});
it('handles old reports and unavailable frames without inventing readings',()=>{
 const {rerender}=render(<IndicatorContext/>);
 expect(screen.getByText(/برای این گزارش ثبت نشده‌اند/)).toBeInTheDocument();
 rerender(<IndicatorContext features={{version:2,timeframes:{'4h':{available:false}}}}/>);
 expect(screen.getByText('دادهٔ کافی یا معتبر ثبت نشده است.')).toBeInTheDocument();
});

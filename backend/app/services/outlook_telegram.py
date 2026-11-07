"""Compact Persian report text; executable levels only from the saved engine card."""
import math


def format_outlook_message(row, close, public_url):
    report = row.report or {}
    plan = report.get('trade_plan') or {}
    scenario = report.get('dominant') or {}
    def number(value):
        return '\u2066' + f'{float(value):,.1f}'.rstrip('0').rstrip('.') + '\u2069'
    def positive(value):
        return isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0
    label = 'تحلیل چهار‌ساعته' if row.slot_close_ms else 'چشم‌انداز روزانه'
    valid_plan = (plan.get('source')=='ict_engine' and plan.get('action') in ('long','short')
                  and all(positive(plan.get(k)) for k in ('entry_price','stop_loss'))
                  and bool(plan.get('take_profits')) and all(positive(t) for t in plan['take_profits']))
    status = ('سفارش آزمایشی ' + ('لانگ' if plan['action']=='long' else 'شورت') + '؛ در انتظار ورود') if valid_plan else 'انتظار؛ ورود تأیید نشده'
    price=(row.context or {}).get('live_price')
    direction={'bullish':'صعودی','bearish':'نزولی','range':'رنج'}.get(scenario.get('direction'),'نامشخص')
    lines=[f'{row.symbol} | {label}',status,
           f'قیمت: {number(price) if positive(price) else "نامشخص"} | زمان تهران: {close:%Y-%m-%d %H:%M}',
           f'جهت سناریوی اصلی: {direction}؛ مشروط',
           'دلیل: '+report.get('summary_fa',''),
           'شرط فعال‌شدن سناریو: '+scenario.get('activation_fa',''),
           'ابطال سناریو: '+scenario.get('invalidation_fa','')]
    if valid_plan:
        lines += ['شرط اجرای سفارش: '+plan.get('trigger_fa',''),
                  'ورود پیشنهادی: '+number(plan['entry_price']),
                  'حدضرر: '+number(plan['stop_loss'])+' | هدف: '+ ' / '.join(number(t) for t in plan['take_profits'])]
        rr=plan.get('risk_reward') or []
        if rr and positive(rr[0]):lines.append(f'سود به زیان خالص: 1:{rr[0]:.2f}')
    else:
        lines.append('ورود / حدضرر / هدف اجرایی: هنوز ستاپ تأییدشده‌ای وجود ندارد.')
    lines += ['ابهام‌ها: '+report.get('uncertainty_fa',''), 'معامله آزمایشی؛ گزارش کامل و سناریوی جایگزین در پنل.']
    if public_url.startswith('https://'):
        lines.append(public_url.rstrip('/')+f'/#/{"analysis" if row.slot_close_ms else "outlook"}/reports/{row.id}')
    return '\n\n'.join(lines)

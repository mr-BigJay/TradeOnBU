"""Compact Persian report text; executable levels only from the saved engine card."""
import math


def format_outlook_message(row, close, public_url):
    report = row.report or {}
    if report.get("analysis_mode") == "independent_htf_v1":
        return format_independent_outlook(row, close, public_url)
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
    names = {'funding':'Funding Rate','open_interest':'Open Interest','trade_flow':'Trade Flow',
             'order_book':'Order Book','liquidations':'Liquidations'}
    effects = {'supports':'همسو','opposes':'مخالف','neutral':'خنثی','not_assessed':'توضیح داده نشده'}
    evidence = report.get('supplemental_evidence') or []
    if evidence:
        lines.append('شواهد تکمیلی نسبت به سناریوی غالب:\n' + '\n'.join(
            f"{names.get(e['source'], e['source'])}: {effects.get(e['effect'], e['effect'])}؛ {e['explanation_fa']}"
            for e in evidence))
    missing = ((row.context or {}).get('market_intelligence') or {}).get('missing_sources') or {}
    if missing:
        lines.append('داده‌های ارسال‌نشده: ' + '، '.join(names.get(k,k) for k in missing))
    lines += ['ابهام‌ها: '+report.get('uncertainty_fa',''), 'معامله آزمایشی؛ گزارش کامل و سناریوی جایگزین در پنل.']
    if public_url.startswith('https://'):
        lines.append(public_url.rstrip('/')+f'/#/{"analysis" if row.slot_close_ms else "outlook"}/reports/{row.id}')
    return '\n\n'.join(lines)



def format_independent_outlook(row, close, public_url):
    report = row.report or {}
    trend = report.get("trend_assessment") or {}
    scenario = report.get("dominant") or {}
    review = report.get("previous_plan_review") or {}
    plan = report.get("swing_plan") or {}
    def brief(value, limit=180):
        value = str(value or "")
        return value if len(value) <= limit else value[:limit-1]+"…"
    def price(value):
        return f"{float(value):,.1f}"
    lines = [f"{row.symbol} | چشم‌انداز روزانه با افق چندروزه",
             f"زمان تهران: {close:%Y-%m-%d %H:%M}", brief(report.get("summary_fa"), 300)]
    if review:
        lines += ["پیگیری سناریوی قبلی: "+brief(review.get("assessment_fa"), 230),
                  "ورود تازه: "+brief(review.get("late_entry_fa"), 200)]
    lines += ["هفتگی: "+brief(report.get("weekly_context_fa")),
              "روزانه: "+brief(report.get("daily_structure_fa")),
              "مرحله روند: "+brief(trend.get("phase_fa")),
              "مسیر محتمل: "+brief(trend.get("expected_path_fa"), 300),
              "ناحیه حساس: "+brief(trend.get("reversal_watch_fa"), 200),
              "شرط سناریو: "+brief(scenario.get("activation_fa")),
              "ابطال: "+brief(scenario.get("invalidation_fa"))]
    levels = {l["id"]: l["price"] for l in (row.context or {}).get("levels", [])}
    retests = [levels[k] for k in scenario.get("retest_level_ids", []) if k in levels]
    if retests:
        lines += ["بازآزمایی، نه هدف: "+" / ".join(price(p) for p in retests)]
    if not scenario.get("target_level_ids"):
        lines += ["هدف: "+(scenario.get("target_unavailable_reason_fa") or "هدف معتبر مشخص نشده است.")]
    if plan.get("source") == "independent_swing" and plan.get("action") in ("long", "short"):
        from datetime import datetime
        expires = datetime.fromisoformat(plan["entry_expires_at"])
        lines += ["پیشنهاد مشروط سوئینگ: "+("لانگ" if plan["action"] == "long" else "شورت"),
                  "شرط ورود: "+brief(plan.get("trigger_fa"), 200),
                  f"ورود: {price(plan['entry_price'])} | حدضرر: {price(plan['stop_loss'])}",
                  "اهداف: "+" / ".join(price(t) for t in plan['take_profits']),
                  "سود به زیان تخمینی پس از هزینه‌ها: "+" / ".join(f"1:{r:.2f}" for r in plan['risk_reward']),
                  f"افق نگهداری: حدود {plan['holding_days']} روز | اعتبار ورود تا {expires.astimezone(close.tzinfo):%Y-%m-%d %H:%M} تهران",
                  "فاندینگ آینده لحاظ نشده؛ این پیشنهاد سفارش خودکار نیست."]
    else:
        lines += ["پیشنهاد سوئینگ: "+brief(plan.get("rationale_fa") or "فرصت مناسبی ارائه نشده است.")]
    lines += ["تغییر نسبت به قبل: "+brief(report.get("change_since_previous_fa")),
              "محدودیت‌ها: "+brief(report.get("uncertainty_fa")),
              "پیگیری سناریو به معنی تأیید ورود واقعی یا سود معامله نیست. جزئیات کامل در پنل."]
    if public_url.startswith("https://"):
        lines.append(public_url.rstrip('/')+f'/#/outlook/reports/{row.id}')
    return "\n\n".join(lines)

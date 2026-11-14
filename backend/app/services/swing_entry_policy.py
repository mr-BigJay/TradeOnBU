"""Advisory daily-entry validation only; never places orders or changes levels."""
import math


class EntryPolicyError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.details = details


def assess_entry_policy(plan, prices, context, model, entry, stop, targets):
    execution, obstacle = plan.execution, plan.first_obstacle
    strict = context.get("swing_entry_policy") == "conditional_entry_v2"
    if execution is None and obstacle is None and not strict:
        return {"entry_policy_status": "legacy_unassessed",
                "entry_policy_note_fa": "روش ورود و مانع اول در این گزارش قدیمی اعتبارسنجی نشده‌اند."}
    if execution is None or obstacle is None:
        raise EntryPolicyError("swing_execution_missing")
    sign = 1 if plan.action == "long" else -1
    confirmation = prices.get(execution.confirmation_level_id)
    if (confirmation is None or not math.isfinite(confirmation) or confirmation <= 0
            or execution.close_side != ("above" if sign == 1 else "below")
            or sign * (confirmation-entry) < 0
            or sign * (targets[0]-confirmation) <= 0):
        raise EntryPolicyError("swing_execution_geometry")

    obstacle_price, obstacle_rr = None, None
    if obstacle.assessment == "none_in_path":
        if obstacle.level_id is not None:
            raise EntryPolicyError("swing_obstacle_invalid")
    else:
        obstacle_price = prices.get(obstacle.level_id)
        if (obstacle_price is None or not math.isfinite(obstacle_price)
                or sign * (obstacle_price-entry) <= 0
                or sign * (targets[-1]-obstacle_price) < 0):
            raise EntryPolicyError("swing_obstacle_invalid")
        # Use the existing conservative cost model, including adverse entry
        # slippage; do not assume a maker fill merely because the plan is LIMIT.
        result = model.calc_rr(plan.action.upper(), entry, stop, obstacle_price,
                               "STOP", min_net_rr=1.5)
        obstacle_rr = result.net_rr
        if not result.acceptable or not math.isfinite(obstacle_rr):
            raise EntryPolicyError("swing_obstacle_rr_below_minimum",
                                   obstacle_price=obstacle_price,
                                   obstacle_net_rr=obstacle_rr, minimum_net_rr=1.5)
    tf = "۴ساعته" if execution.timeframe == "4h" else "روزانه"
    side = "بالای" if sign == 1 else "زیر"
    trigger = (f"پس از یک بسته‌شدن {tf} {side} {confirmation:,.2f} دلار، "
               f"سفارش Limit در {entry:,.2f} دلار برای بازآزمایی بعدی؛ "
               "بدون نیاز به بسته‌شدن دوم. لمس قبل از تأیید ورود نیست.")
    return {
        "entry_policy_status": "validated",
        "original_trigger_fa": plan.trigger_fa,
        "trigger_fa": trigger,
        "confirmation_price": confirmation,
        "confirmation_status": "not_evaluated",
        "order_type": "LIMIT",
        "obstacle_price": obstacle_price,
        "obstacle_net_rr": obstacle_rr,
        "entry_policy_note_fa": "این ترتیب پیشنهادی است؛ وقوع تأیید یا پرشدن سفارش بررسی نشده است.",
    }

"""Daily reports run every day; new position advice follows the Tehran calendar."""
from datetime import datetime
from app.domain.trading_calendar import new_entries_allowed


def proposals_allowed(context):
    # Use the frozen snapshot date, not the time an archive is revalidated.
    # Do not trust a model-supplied flag to authorize a weekend proposal.
    at = datetime.fromisoformat(context["as_of"].replace("Z", "+00:00"))
    return new_entries_allowed(at)


def weekend_plan():
    return {
        "action": "wait",
        "rationale_fa": "شنبه و یکشنبه به وقت تهران فقط تحلیل بازار ارائه می‌شود؛ پیشنهاد پوزیشن جدید غیرفعال است.",
        "trigger_fa": "در روزهای مجاز، برنامهٔ ورود با دادهٔ تازه دوباره ارزیابی می‌شود.",
        "entry_level_id": None, "stop_level_id": None, "target_level_ids": [],
        "execution": None, "first_obstacle": None,
        "structural_reason_fa": "این محدودیت تقویمی است و به معنی نبود فرصت در بازار نیست.",
    }

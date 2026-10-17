from __future__ import annotations

import re

# Unicode BiDi controls (invisible); safe for Telegram plain text.
RLI = "\u2067"  # FSI — first strong isolate (RTL context for Persian runs)
PDI = "\u2069"  # PDI — pop directional isolate

_PERSIAN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

# English legacy and Persian report headings (longest labels first).
_REPORT_LABELS: tuple[str, ...] = (
    "ریسک به بازده پیشنهادی (R:R):",
    "حد ضرر پیشنهادی (SL):",
    "هدف اول پیشنهادی (TP1):",
    "بررسی ستاپ قبلی:",
    "دلایل تحلیل ICT:",
    "تأیید لازم:",
    "ابطال سناریو:",
    "تأیید ورود:",
    "روند بازار:",
    "محدوده ورود:",
    "جهت احتمالی:",
    "دلایل عدم ورود:",
    "در انتظار:",
    "تصمیم:",
    "قیمت:",
    "ورود:",
    "Confirmation required:",
    "ICT Reason:",
    "Invalidation:",
    "Decision:",
    "Bias:",
    "Entry:",
    "SL:",
    "TP:",
)


def strip_telegram_bidi_control_chars(text: str) -> str:
    """Remove BiDi controls for content-equivalence checks."""
    return text.replace(RLI, "").replace(PDI, "").replace("\u200f", "").replace("\u200e", "")


def _contains_persian(text: str) -> bool:
    return bool(_PERSIAN_RE.search(text))


def _match_report_label(line: str) -> tuple[str, str] | None:
    """Return (label, remainder_after_label) when line begins with a known label."""
    stripped = line.lstrip()
    for label in _REPORT_LABELS:
        if stripped == label:
            return label, ""
        if stripped.startswith(label):
            rest = stripped[len(label) :]
            if rest == "" or rest[0] in " \t":
                return label, rest.strip()
    return None


def _isolate_persian_run(text: str) -> str:
    if not text or not _contains_persian(text):
        return text
    return f"{RLI}{text}{PDI}"


def apply_telegram_persian_bidi(report_fa: str) -> str:
    """
    Add invisible RTL isolates for Persian narrative lines that follow English labels.

    Visible characters are unchanged; only BiDi control characters may be inserted.
    """
    if not report_fa:
        return report_fa

    lines = report_fa.split("\n")
    out: list[str] = []
    after_label = False

    for line in lines:
        matched = _match_report_label(line)
        if matched is not None:
            label, remainder = matched
            # Preserve original leading whitespace on the line.
            lead = line[: len(line) - len(line.lstrip())]
            if remainder == "":
                out.append(line)
                after_label = True
            else:
                if _contains_persian(remainder):
                    out.append(f"{lead}{label} {_isolate_persian_run(remainder)}")
                else:
                    out.append(line)
                after_label = False
            continue

        if after_label and _contains_persian(line):
            out.append(_isolate_persian_run(line))
        else:
            out.append(line)
            if line.strip() and not _contains_persian(line):
                after_label = False

    return "\n".join(out)

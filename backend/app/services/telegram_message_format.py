"""Central Telegram outbound text: Persian RTL/BiDi, parse_mode escaping, safe chunking."""

from __future__ import annotations

import re
from typing import Literal

ParseMode = Literal["HTML", "Markdown", "MarkdownV2"] | None

TELEGRAM_MESSAGE_MAX_LENGTH = 4096
TELEGRAM_CAPTION_MAX_LENGTH = 1024

# Unicode BiDi isolates (invisible on clients that honor BiDi).
LRI = "\u2066"  # left-to-right isolate — embedded tickers, numbers, timestamps
RLI = "\u2067"  # right-to-left isolate — Persian narrative runs
PDI = "\u2069"  # pop directional isolate

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

# LTR islands inside Persian prose (order: longer / specific patterns first).
_LTR_ISLAND_RE = re.compile(
    r"(?:"
    r"[A-Z]{2,15}(?:USDT|USD|PERP)\b|"
    r"\b(?:LONG|SHORT|NO_TRADE|WAIT|BULLISH|BEARISH|MSS|BOS|FVG|OB|SSL|BSL|ICT|SL|TP|RR|R:R)\b|"
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|"
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b|"
    r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
    r"-?\d+(?:\.\d+)?%|"
    r"\b\d+(?:\.\d+)?[mhdHDM]\b|"
    r"-?\d+\.\d+|"
    r"1:\d+"
    r")"
)

_HTML_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})

_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def strip_telegram_bidi_control_chars(text: str) -> str:
    """Remove BiDi controls for content-equivalence checks."""
    return (
        text.replace(LRI, "")
        .replace(RLI, "")
        .replace(PDI, "")
        .replace("\u200f", "")
        .replace("\u200e", "")
    )


def escape_telegram_html(text: str) -> str:
    return text.translate(_HTML_ESCAPE)


def escape_telegram_markdown_legacy(text: str) -> str:
    """Escape Telegram legacy Markdown (not MarkdownV2) special chars in user/model text."""
    out: list[str] = []
    for ch in text:
        if ch in ("_", "*", "`", "["):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def escape_telegram_markdown_v2(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _MARKDOWN_V2_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def escape_for_parse_mode(text: str, parse_mode: ParseMode) -> str:
    if not parse_mode:
        return text
    mode = parse_mode.upper()
    if mode == "HTML":
        return escape_telegram_html(text)
    if mode == "MARKDOWNV2":
        return escape_telegram_markdown_v2(text)
    if mode == "MARKDOWN":
        return escape_telegram_markdown_legacy(text)
    return text


def _contains_persian(text: str) -> bool:
    return bool(_PERSIAN_RE.search(text))


def _match_report_label(line: str) -> tuple[str, str] | None:
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
    inner = isolate_ltr_tokens_in_persian(text)
    return f"{RLI}{inner}{PDI}"


def isolate_ltr_tokens_in_persian(text: str) -> str:
    """
    Wrap embedded LTR tokens (symbols, directions, signed prices, %, timestamps) in LRI…PDI
    when the line contains Persian script. Visible characters unchanged.
    """
    if not text or not _contains_persian(text):
        return text
    parts: list[str] = []
    last = 0
    for match in _LTR_ISLAND_RE.finditer(text):
        before = text[last : match.start()]
        if before:
            parts.append(before)
        token = match.group()
        parts.append(f"{LRI}{token}{PDI}")
        last = match.end()
    tail = text[last:]
    if tail:
        parts.append(tail)
    return "".join(parts) if parts else text


def format_persian_report_for_telegram(report_fa: str) -> str:
    """
    Apply RTL isolates for Persian narrative and LTR isolates for embedded trading tokens.
    English label lines and pure LTR value lines are unchanged.
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
        elif _contains_persian(line):
            out.append(_isolate_persian_run(line))
        else:
            out.append(line)
            if line.strip():
                after_label = False

    return "\n".join(out)


def prepare_telegram_outbound_text(text: str, *, parse_mode: ParseMode = None) -> str:
    """Escape for parse_mode when set; always apply Persian BiDi for plain AI-style reports."""
    escaped = escape_for_parse_mode(text, parse_mode)
    if parse_mode:
        return escaped
    return format_persian_report_for_telegram(escaped)


def chunk_telegram_text(text: str, *, limit: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> list[str]:
    """
    Split text into Telegram length limits, preferring newline boundaries.
    Never returns empty strings; a single over-long line is hard-split as last resort.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:limit]
        split_at = window.rfind("\n")
        if split_at <= 0:
            chunks.append(rest[:limit])
            rest = rest[limit:]
            continue
        piece = rest[: split_at + 1]
        chunks.append(piece)
        rest = rest[split_at + 1 :]
    return chunks


def split_header_and_body_chunks(
    *,
    header: str,
    body: str,
    limit: int = TELEGRAM_MESSAGE_MAX_LENGTH,
) -> list[str]:
    """First chunk includes header; following chunks are body-only with safe splitting."""
    body = body or ""
    header = header or ""
    if not body:
        return chunk_telegram_text(header.rstrip(), limit=limit) if header.strip() else []

    if len(header) + len(body) <= limit:
        return [header + body]

    messages: list[str] = []
    first_budget = limit - len(header)
    if first_budget <= 0:
        messages.extend(chunk_telegram_text(header, limit=limit))
        messages.extend(chunk_telegram_text(body, limit=limit))
        return messages

    first_body_parts = chunk_telegram_text(body, limit=first_budget)
    messages.append(header + first_body_parts[0])
    remainder = body[len(first_body_parts[0]) :]
    if remainder.startswith("\n"):
        remainder = remainder[1:]
    if remainder:
        messages.extend(chunk_telegram_text(remainder, limit=limit))
    return messages

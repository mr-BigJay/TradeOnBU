"""Central Telegram outbound text: Persian RTL/BiDi, parse_mode escaping, safe chunking."""

from __future__ import annotations

import re
from typing import Literal

ParseMode = Literal["HTML", "Markdown", "MarkdownV2"] | None

TELEGRAM_MESSAGE_MAX_LENGTH = 4096
TELEGRAM_CAPTION_MAX_LENGTH = 1024

# Unicode BiDi isolates (invisible on clients that honor BiDi).
LRI = "\u2066"  # left-to-right isolate — embedded tickers, numbers, timestamps
RLI = "\u2067"  # right-to-left isolate — reserved; avoid whole-line wraps for readability
PDI = "\u2069"  # pop directional isolate

_BIDI_OPEN = frozenset({LRI, RLI})
_BIDI_ALL = frozenset({LRI, RLI, PDI})

_PERSIAN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

# Report field labels (longest first). Trailing colon required for standalone label lines.
_REPORT_LABELS: tuple[str, ...] = (
    "ریسک به بازده پیشنهادی (R:R):",
    "حد ضرر پیشنهادی (SL):",
    "هدف اول پیشنهادی (TP1):",
    "ریسک به بازده (R:R):",
    "حد ضرر (SL):",
    "هدف اول (TP1):",
    "هدف دوم (TP2):",
    "بررسی ستاپ قبلی:",
    "دلایل تحلیل ICT:",
    "وضعیت کلی بازار:",
    "معامله خلاف روند غالب:",
    "سناریوی مشروط:",
    "تأیید لازم:",
    "ابطال سناریو:",
    "تأیید ورود:",
    "جهت احتمالی:",
    "دلایل عدم ورود:",
    "روند بازار:",
    "محدوده ورود:",
    "در انتظار:",
    "تصمیم:",
    "قیمت:",
    "ستاپ:",
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

_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}."


def strip_telegram_bidi_control_chars(text: str) -> str:
    """Remove BiDi controls for content-equivalence checks."""
    return (
        text.replace(LRI, "")
        .replace(RLI, "")
        .replace(PDI, "")
        .replace("\u200f", "")
        .replace("\u200e", "")
    )


def _already_has_bidi_controls(text: str) -> bool:
    return any(ch in _BIDI_ALL for ch in text)


def escape_telegram_html(text: str) -> str:
    return text.translate(_HTML_ESCAPE)


def escape_telegram_markdown_legacy(text: str) -> str:
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


def _is_label_only_line(line: str) -> bool:
    matched = _match_report_label(line)
    return matched is not None and matched[1] == ""


def _should_not_merge_as_value(next_line: str) -> bool:
    stripped = next_line.strip()
    if not stripped:
        return True
    if stripped.startswith("- "):
        return True
    if _match_report_label(stripped) is not None:
        return True
    if stripped.startswith("تحلیل ICT"):
        return True
    return False


def join_label_value_lines(lines: list[str]) -> list[str]:
    """Merge standalone label lines with the following value line (server report layout)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if _is_label_only_line(stripped):
            label_match = _match_report_label(stripped)
            assert label_match is not None
            label, _ = label_match
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and not _should_not_merge_as_value(lines[j]):
                lead = line[: len(line) - len(line.lstrip())]
                value = lines[j].strip()
                out.append(f"{lead}{label} {value}")
                i = j + 1
                continue
        out.append(line)
        i += 1
    return out


def normalize_report_layout_for_telegram(report_fa: str) -> str:
    """
    Collapse excessive blank lines and keep each field label on the same line as its value.
    Visible text is preserved; only whitespace layout changes.
    """
    if not report_fa:
        return report_fa
    lines = [ln.rstrip() for ln in report_fa.split("\n")]
    lines = join_label_value_lines(lines)
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                out.append("")
            continue
        blank_run = 0
        out.append(line)
    return "\n".join(out).strip("\n")


def isolate_ltr_tokens_in_persian(text: str) -> str:
    if not text or not _contains_persian(text) or _already_has_bidi_controls(text):
        return text
    parts: list[str] = []
    last = 0
    for match in _LTR_ISLAND_RE.finditer(text):
        before = text[last : match.start()]
        if before:
            parts.append(before)
        token = match.group()
        if _already_has_bidi_controls(token):
            parts.append(token)
        else:
            parts.append(f"{LRI}{token}{PDI}")
        last = match.end()
    tail = text[last:]
    if tail:
        parts.append(tail)
    return "".join(parts) if parts else text


def apply_line_bidi(line: str) -> str:
    if not line or _already_has_bidi_controls(line):
        return line
    if _contains_persian(line):
        return isolate_ltr_tokens_in_persian(line)
    return line


def format_persian_report_for_telegram(report_fa: str) -> str:
    """Normalize layout, then isolate embedded LTR tokens in Persian lines (no double-wrap)."""
    if not report_fa:
        return report_fa
    normalized = normalize_report_layout_for_telegram(report_fa)
    return "\n".join(apply_line_bidi(ln) for ln in normalized.split("\n"))


def prepare_telegram_outbound_text(text: str, *, parse_mode: ParseMode = None) -> str:
    escaped = escape_for_parse_mode(text, parse_mode)
    if parse_mode:
        return escaped
    return format_persian_report_for_telegram(escaped)


def _bidi_depth(text: str) -> int:
    depth = 0
    for ch in text:
        if ch in _BIDI_OPEN:
            depth += 1
        elif ch == PDI:
            depth = max(0, depth - 1)
    return depth


def _find_bidi_safe_newline_split(window: str) -> int:
    pos = len(window)
    while pos > 0:
        split_at = window.rfind("\n", 0, pos)
        if split_at <= 0:
            return -1
        if _bidi_depth(window[: split_at + 1]) == 0:
            return split_at
        pos = split_at
    return -1


def chunk_telegram_text(text: str, *, limit: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> list[str]:
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
        split_at = _find_bidi_safe_newline_split(window)
        if split_at <= 0:
            split_at = window.rfind("\n\n")
            if split_at <= 0:
                split_at = limit
                if _bidi_depth(rest[:split_at]) != 0:
                    split_at = max(1, limit - 64)
            piece = rest[:split_at]
            if not piece:
                piece = rest[:limit]
                split_at = len(piece)
            chunks.append(piece)
            rest = rest[split_at:]
            if rest.startswith("\n"):
                rest = rest[1:]
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

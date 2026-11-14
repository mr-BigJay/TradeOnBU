"""Reconcile uniquely referenced condition prices; retain an audit of edits."""
import math
import re

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUMBER = re.compile(r"(?<![\d.])[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?")


def _tokens(text):
    normalized = text.translate(_DIGITS).replace("٬", ",").replace("٫", ".")
    return [(m, float(m.group().replace(",", ""))) for m in _NUMBER.finditer(normalized)]


def _equal(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=.01)


def _replace(text, token, price):
    value = f"{price:,.8f}".rstrip("0").rstrip(".")
    if any(c in text[token.start():token.end()] for c in "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"):
        value = value.translate(str.maketrans("0123456789,.", "۰۱۲۳۴۵۶۷۸۹٬٫"))
    return text[:token.start()] + value + text[token.end():]


def reconcile_condition_prices(output, levels):
    prices = {level["id"]: level["price"] for level in levels}
    cases = []
    proposals = {}
    ambiguous = set()
    corrections = []
    warnings = []
    for key in ("dominant", "alternative"):
        for field, refs in (("activation_fa", "activation_level_ids"),
                            ("invalidation_fa", "invalidation_level_ids")):
            text = output[key][field]
            tokens = [(m, n) for m, n in _tokens(text) if n >= 1000]
            expected = list(dict.fromkeys(prices[i] for i in output[key][refs]))
            bad = [(m, n) for m, n in tokens if not any(_equal(n, p) for p in expected)]
            if not bad:
                continue
            # Never guess between multiple levels/numbers or replace another
            # valid structural price. Direction/timeframe/other words stay intact.
            candidate = len(tokens) == len(expected) == 1 and not any(
                _equal(bad[0][1], p) for p in prices.values())
            cases.append((key, field, text, expected, bad, candidate))
            if not candidate:
                ambiguous.update(n for _, n in bad)
            if candidate:
                proposals.setdefault(bad[0][1], set()).add(expected[0])

    resolved = {}
    for key, field, text, expected, bad, candidate in cases:
        old = bad[0][1]
        if candidate and old not in ambiguous and len(proposals[old]) == 1:
            updated = _replace(text, bad[0][0], expected[0])
            output[key][field] = updated
            resolved[old] = expected[0]
            corrections.append({"field": f"{key}.{field}", "original_text": text,
                                "corrected_text": updated, "referenced_prices": expected})
        else:
            warnings.append({"field": f"{key}.{field}", "code": "condition_price_mismatch",
                             "referenced_prices": expected})

    # The reversal-watch paragraph repeats the same condition in this report.
    # Reconcile only exact erroneous numbers with an unambiguous mapping above.
    trend = output.get("trend_assessment") or {}
    text = trend.get("reversal_watch_fa", "")
    updated = text
    for token, number in reversed(_tokens(text)):
        if number in resolved:
            updated = _replace(updated, token, resolved[number])
    if updated != text:
        trend["reversal_watch_fa"] = updated
        corrections.append({"field": "trend_assessment.reversal_watch_fa",
                            "original_text": text, "corrected_text": updated})
    if corrections:
        output["text_price_corrections"] = corrections
    if warnings:
        output["text_validation_warnings"] = warnings

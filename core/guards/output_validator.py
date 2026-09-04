"""
Output validator — enforces project rule 4: "No LLM output is trusted. The
LLM never authors a number, price, amount, order ID or URL."

HOW THE RULE IS ACTUALLY ENFORCED
----------------------------------
A naive reading ("reject any digit in LLM output") is unimplementable for an
obligation compiler: a criterion like `quantity_sum >= 4` obviously contains
a number. The rule is not "no digits exist" — it is "the model never
ORIGINATES a value." Two different guards implement that, and every field of
the compiler's output goes through exactly one of them:

  assert_quoted_span(span, source_text)
      For any field that carries a value (quantities, budgets, deadlines,
      ingredient names). The model may only return a VERBATIM SUBSTRING of
      the user's instruction. The numeric value is then parsed from that
      substring by deterministic Python (see core/obligation/compiler.py's
      _parse_* functions), not read out of the model's own prose. A model
      that hallucinates "under Rs 2000" for an instruction that said
      "under Rs 1500" fails this check loudly, because its span is not
      present in the instruction.

  assert_no_authored_values(text)
      For genuinely free-text fields (reasoning, ambiguity descriptions).
      These may contain no digit spans and no URLs at all, because there is
      no source text to anchor them to.

The span mechanism is strictly stronger than digit-rejection: it also
catches an authored value that happens to contain no digits (a hallucinated
merchant name, a prohibited ingredient the user never mentioned).

WHITESPACE AND CASE
-------------------
Span matching normalises Unicode (NFKC), collapses runs of whitespace, and
lower-cases before comparing. It does NOT strip punctuation or currency
symbols — "Rs 1500" must genuinely appear; the model cannot round-trip
"1500" into "Rs 1,500" and have it accepted.
"""

from __future__ import annotations

import re
import unicodedata

# A run of digits, optionally with separators/decimals — i.e. anything that
# could be read as a quantity or an amount.
DIGIT_SPAN_RE = re.compile(r"\d[\d,._]*")

# Deliberately broad: bare-host forms ("example.com/x") count as URLs too.
URL_RE = re.compile(
    r"(?:(?:https?|ftp)://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|in|co|dev|app|ai)\b(?:/\S*)?",
    re.IGNORECASE,
)


class OutputValidationError(Exception):
    """Raised when LLM output violates the never-author-a-value rule.

    This is always a hard failure. It is never downgraded to a warning and
    never results in the offending field being silently dropped — a dropped
    criterion is an undetected violation later, which is the exact failure
    mode core/models/fields.py exists to prevent.
    """


def normalise(text: str) -> str:
    """NFKC-normalise, collapse whitespace runs, lower-case. Used on both
    sides of every span comparison so the check is about content, not
    incidental spacing or Unicode form."""
    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", folded).strip().lower()


def find_digit_spans(text: str) -> list[str]:
    return DIGIT_SPAN_RE.findall(text)


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def assert_no_authored_values(text: str, *, context: str) -> None:
    """
    For free-text LLM fields with no source text to anchor against.
    Rejects any digit span or URL outright.
    """
    urls = find_urls(text)
    if urls:
        raise OutputValidationError(
            f"{context}: LLM output contains URL(s) {urls!r}. The model must never "
            f"author a URL. Offending text: {text!r}"
        )
    digits = find_digit_spans(text)
    if digits:
        raise OutputValidationError(
            f"{context}: LLM output contains free-floating digit span(s) {digits!r}. "
            f"The model must never author a number — values are parsed from quoted "
            f"spans of the user instruction instead. Offending text: {text!r}"
        )


def is_quoted_span(span: str, source_text: str) -> bool:
    """True iff `span` appears verbatim (modulo whitespace/case/Unicode form)
    in `source_text`."""
    if not span.strip():
        return False
    return normalise(span) in normalise(source_text)


def assert_quoted_span(span: str, source_text: str, *, context: str) -> None:
    """
    For any LLM field carrying a value. The span must be a verbatim substring
    of the user's instruction; the value is parsed from it afterwards by
    deterministic code.
    """
    if not span.strip():
        raise OutputValidationError(
            f"{context}: empty span. A value-bearing field must quote the instruction."
        )
    if not is_quoted_span(span, source_text):
        raise OutputValidationError(
            f"{context}: span {span!r} does not appear in the instruction. The model "
            f"may only quote the user's own words, never author a value. "
            f"Instruction: {source_text!r}"
        )


def assert_no_urls(text: str, *, context: str) -> None:
    urls = find_urls(text)
    if urls:
        raise OutputValidationError(f"{context}: LLM output contains URL(s) {urls!r}.")

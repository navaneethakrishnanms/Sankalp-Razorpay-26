"""
Underspecification detection -> the CLARIFY path.

An ambiguous instruction is one where a correct order is not determined by
what the user said. The right response is to ask one bounded question, not to
guess and then enforce the guess as if the user had stated it.

WHY THIS IS A HYBRID, NOT PURE LLM
-----------------------------------
Two independent detectors, unioned:

  * Deterministic lexical rules (below) catch the mechanical cases —
    vague quantifiers, items named with no quantity — with no model call and
    no variance. These are cheap and they never regress.
  * The compiler's own LLM-emitted `ambiguity_flags` catch the semantic cases
    a word list cannot (a requirement that is subjective, two requirements
    that conflict).

Union rather than intersection because the cost asymmetry is clear: a missed
ambiguity silently converts a guess into an enforced requirement, while a
spurious ambiguity costs one clarifying question. Under-detection is the
expensive direction.

A NOTE ON THE GROUND-TRUTH LABEL
---------------------------------
`instruction_ambiguous` in the eval corpus was assigned per-seed by hand
during authoring, from an intuitive reading rather than a written-down rule
(unlike `violation_class`, which is mechanically determined by the mutation
applied). It is therefore the NOISIEST label in the corpus, and
ambiguity precision/recall measured against it should be read as
"agreement with the corpus author's intuition", not as ground truth. This is
stated plainly in eval/results/ and the README rather than being quietly
absorbed into a headline number.
"""

from __future__ import annotations

import dataclasses
import re

# Vague quantifiers, English and Hinglish. Word-boundary matched.
VAGUE_QUANTIFIERS = (
    "some", "a few", "few", "a couple", "couple", "maybe", "perhaps",
    "several", "a bit of", "or so", "roughly", "around",
    "kuch", "thoda", "thodi", "kuchh",
)

# Subjective qualifiers with no objective threshold.
SUBJECTIVE_MARKERS = (
    "too spicy", "not too", "nothing too", "must be fresh", "fresh not frozen",
    "unsalted", "artificial colour", "artificial color", "served hot",
    "garam garam", "zyada oily", "oily nahi", "extra fried", "fried na ho",
    "good quality", "nice", "decent", "proper",
)

AMBIGUITY_CODES = (
    "UNSTATED_QUANTITY",
    "VAGUE_QUANTIFIER",
    "SUBJECTIVE_CONSTRAINT",
    "UNSTATED_MERCHANT",
    "CONFLICTING_REQUIREMENT",
)


@dataclasses.dataclass(frozen=True)
class AmbiguityFlag:
    code: str
    span: str
    detector: str   # "lexical" | "llm"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "span": self.span, "detector": self.detector}


def _contains_word(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", haystack) is not None


def detect_lexical(instruction: str) -> list[AmbiguityFlag]:
    """Deterministic detectors. No model call, no variance."""
    lowered = instruction.lower()
    flags: list[AmbiguityFlag] = []

    for marker in VAGUE_QUANTIFIERS:
        if _contains_word(lowered, marker):
            flags.append(AmbiguityFlag("VAGUE_QUANTIFIER", marker, "lexical"))
            break

    for marker in SUBJECTIVE_MARKERS:
        if marker in lowered:
            flags.append(AmbiguityFlag("SUBJECTIVE_CONSTRAINT", marker, "lexical"))
            break

    return flags


def detect_unstated_quantity(instruction: str, *, item_count: int) -> list[AmbiguityFlag]:
    """
    Items were named but the instruction carries no digit at all — so no
    per-item quantity can have been stated.

    Deliberately conservative: only fires when there is NO digit anywhere in
    the instruction. An instruction with a digit somewhere may still leave one
    item's quantity unstated, but distinguishing "the 4 in 'for 4 people'" from
    "the 500 in 'under Rs 500'" is exactly the reasoning the LLM detector is
    better at, so that case is left to it rather than guessed at here.
    """
    if item_count > 0 and not re.search(r"\d", instruction):
        return [AmbiguityFlag("UNSTATED_QUANTITY", instruction.strip()[:60], "lexical")]
    return []


def merge_flags(
    lexical: list[AmbiguityFlag], llm: list[AmbiguityFlag]
) -> list[AmbiguityFlag]:
    """Union, de-duplicated by (code, normalised span). Lexical wins ties so
    the more reproducible detector is the one attributed."""
    seen: set[tuple[str, str]] = set()
    merged: list[AmbiguityFlag] = []
    for flag in list(lexical) + list(llm):
        key = (flag.code, flag.span.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(flag)
    return merged


def is_ambiguous(flags: list[AmbiguityFlag]) -> bool:
    return bool(flags)


def clarifying_question(flags: list[AmbiguityFlag]) -> str | None:
    """
    One bounded question for the highest-priority flag.

    Bounded on purpose: a CLARIFY that asks the user to restate their whole
    order is a worse experience than the wrong order it prevents. Priority
    order puts the flags a user can answer in a word first.
    """
    if not flags:
        return None
    priority = ("UNSTATED_QUANTITY", "VAGUE_QUANTIFIER", "UNSTATED_MERCHANT",
                "CONFLICTING_REQUIREMENT", "SUBJECTIVE_CONSTRAINT")
    by_code = {f.code: f for f in flags}
    for code in priority:
        if code not in by_code:
            continue
        span = by_code[code].span
        if code == "UNSTATED_QUANTITY":
            return f"How many of each item should I order? Your instruction ({span!r}) didn't say."
        if code == "VAGUE_QUANTIFIER":
            return f"You said {span!r} — how many exactly should I add?"
        if code == "UNSTATED_MERCHANT":
            return "Which shop or restaurant should I order from?"
        if code == "CONFLICTING_REQUIREMENT":
            return f"Two of your requirements conflict ({span!r}). Which should I follow?"
        return f"You asked for {span!r} — can you give me a specific rule I can check against?"
    return None

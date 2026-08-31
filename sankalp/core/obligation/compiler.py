"""
Obligation compiler — natural-language instruction -> bound Obligation.

The one genuinely hard AI component. Everything downstream of it is
deterministic Python.

THE SPAN CONTRACT (why the LLM never authors a value)
-------------------------------------------------------
The model does not emit values. It emits *spans*: verbatim substrings of the
user's instruction. Deterministic code in this module then parses the value
out of the span:

    instruction : "Order dinner for 4 people ... under Rs 1500"
    model emits : value_span="4 people",  budget_ceiling_span="under Rs 1500"
    this module : _parse_int("4 people") -> 4
                  _parse_money("under Rs 1500") -> Decimal("1500")

core/guards/output_validator.assert_quoted_span rejects any span that is not
literally present in the instruction, so a hallucinated "Rs 2000" cannot
survive: it isn't in the user's text. This satisfies project rule 4 in the
only way that is actually implementable for a component whose output contains
numbers — see that module's docstring for the full argument.

FAILURE POSTURE
---------------
Every failure mode here is loud:
  * unparseable JSON               -> CompilerError
  * span not found in instruction  -> OutputValidationError (from the guard)
  * field not in closed registry   -> recorded as unresolvable, and the
                                       criterion is DROPPED FROM THE OUTPUT but
                                       COUNTED in CompilationResult.unresolvable_paths
  * unparseable value in a span    -> criterion dropped, counted

Dropping-and-counting is deliberate and is not the "silent skip" the field
registry warns about: the count is a first-class reported metric
(unresolvable-path rate), and bind() would reject the obligation outright if a
bad path reached it. What must never happen — a bad path quietly becoming an
ABSTAIN inside the verifier — cannot happen, because the path never reaches
the verifier.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from agent import catalogue
from core.guards.output_validator import (
    OutputValidationError,
    assert_quoted_span,
    is_quoted_span,
    normalise,
)
from core.llm.client import DEFAULT_MODEL, LLMClient, LLMRequest, LLMResponse
from core.llm.prompts import load_prompt
from core.models.enums import CriterionOperator, CriterionSource
from core.models.fields import all_paths, get_field_spec
from core.models.obligation import (
    AcceptanceCriterion,
    DeliveryWindow,
    MerchantScope,
    Obligation,
)
from core.obligation.ambiguity import (
    AmbiguityFlag,
    clarifying_question,
    detect_lexical,
    detect_unstated_quantity,
    merge_flags,
)
from core.obligation.binder import bind

# Prompt versions are selectable so that iterating on the prompt produces a
# SIDE-BY-SIDE comparison rather than overwriting the previous result. Prompt
# iteration against a measured set is a form of fitting, and the only honest way
# to do it is to keep every version's numbers visible.
#
# v1 -> v2 changes exactly one thing: it documents the corpus convention that
# quantity criteria are floors (`gte`), with over-ordering caught by
# budget_ceiling rather than by the quantity check. v1 had no way to know this,
# and compiled "Order 2 Margherita" as `eq 2`, which false-blocked every clean
# record that added a unit. Keeping the change to one axis is what makes any
# metric movement attributable to it.
PROMPT_VERSIONS: dict[str, str] = {
    "v1": "obligation_compiler_v1.md",
    "v2": "obligation_compiler_v2.md",
}
DEFAULT_PROMPT_VERSION = "v1"

PROMPT_FILE = PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]
PROMPT_VERSION = f"obligation_compiler/{DEFAULT_PROMPT_VERSION}"


def resolve_prompt(version: str) -> tuple[str, str]:
    """Return (filename, cache-key version string) for a prompt version."""
    if version not in PROMPT_VERSIONS:
        raise CompilerError(
            f"Unknown prompt version {version!r}. Available: {sorted(PROMPT_VERSIONS)}"
        )
    return PROMPT_VERSIONS[version], f"obligation_compiler/{version}"

# Providers reserve (prompt tokens + max_tokens) against a per-minute budget,
# so this is not a free ceiling — on Groq's free tier (8,000 TPM) a value of
# 8192 makes a ~2,300-token prompt reserve 10,475 and the request is rejected
# outright, before the model ever runs.
#
# 4096 leaves ample room for the largest corpus output (S18 declares 5 criteria;
# the JSON runs ~700 tokens) plus gpt-oss reasoning tokens, while keeping the
# whole reservation near 6,400 — inside the free tier. Raise it on a paid tier
# via --max-tokens if a compilation ever truncates.
DEFAULT_MAX_TOKENS = 4096

SYSTEM_PROMPT = (
    "You are a precise structured-data extractor for a payment-clearing system. "
    "You return only JSON. You never invent values — you only quote the user's own words."
)


class CompilerError(Exception):
    """The compiler could not produce a usable obligation."""


@dataclasses.dataclass
class CompilationResult:
    obligation:          Obligation
    ambiguity_flags:      list[AmbiguityFlag]
    clarify:               bool
    clarifying_question:    str | None
    unresolvable_paths:      list[str]      # field paths the model invented
    dropped_criteria:         list[str]      # human-readable reasons
    raw_response:              str
    llm_response:               LLMResponse

    @property
    def criteria_emitted(self) -> int:
        return len(self.obligation.acceptance_criteria)


# ── Value parsing (deterministic; the model never does this) ───────────────

_INT_RE = re.compile(r"\d+")
# Money: an optional currency marker then a number with optional separators.
_MONEY_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje|bje)?", re.IGNORECASE
)


def _parse_int(span: str) -> int:
    match = _INT_RE.search(span)
    if match is None:
        raise ValueError(f"no integer found in span {span!r}")
    return int(match.group())


def _parse_money(span: str) -> Decimal:
    match = _MONEY_RE.search(span)
    if match is None:
        raise ValueError(f"no monetary amount found in span {span!r}")
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount in span {span!r}") from exc


def _parse_deadline(span: str, reference_date: datetime) -> datetime:
    """
    Parse a clock time out of a span and place it on `reference_date`.

    `reference_date` is supplied by the caller, never inferred from the system
    clock: an eval that read `datetime.now()` would produce different
    obligations on different days and silently break reproducibility.

    Hinglish note: `raat 9 baje` = 9pm, `sham 6 baje` = 6pm, `subah 9 baje` = 9am.
    An hour of 1-11 with no am/pm marker is read as PM, which is the correct
    default for food-delivery and grocery deadlines.
    """
    lowered = span.lower()
    match = _TIME_RE.search(lowered)
    if match is None:
        raise ValueError(f"no time found in span {span!r}")

    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    marker = (match.group(3) or "").lower()

    if hour > 23 or minute > 59:
        raise ValueError(f"implausible time in span {span!r}")

    is_morning = "subah" in lowered or marker == "am"
    is_evening = "raat" in lowered or "sham" in lowered or "shaam" in lowered or marker == "pm"

    if is_morning:
        if hour == 12:
            hour = 0
    elif is_evening:
        if hour < 12:
            hour += 12
    elif 1 <= hour <= 11:
        hour += 12   # bare "by 9" for a dinner order means 9pm

    return reference_date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _resolve_merchant_ids(span: str) -> list[str]:
    """
    Map a quoted merchant name to catalogue merchant IDs.

    The model quotes the user's words ("Biryani House"); this function resolves
    them to an ID. The model never emits an ID — an invented merchant ID would
    be an authored value and would silently widen or narrow the merchant scope.
    """
    target = normalise(span)
    exact = [m.id for m in catalogue.MERCHANTS.values() if normalise(m.name) == target]
    if exact:
        return sorted(exact)
    partial = [
        m.id for m in catalogue.MERCHANTS.values()
        if normalise(m.name) in target or target in normalise(m.name)
    ]
    return sorted(partial)


def _resolve_merchant_category(span: str) -> str | None:
    target = normalise(span)
    known = {normalise(m.category.replace("_", " ")): m.category for m in catalogue.MERCHANTS.values()}
    for label, category in known.items():
        if label in target or target in label:
            return category
    if "grocery" in target or "supermarket" in target or "store" in target:
        return "grocery"
    if "food" in target or "restaurant" in target or "delivery" in target:
        return "food_delivery"
    return None


# ── Response parsing ──────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """
    Pull the JSON object out of a model response.

    Tolerates markdown fences and incidental prose around the object (the
    prompt forbids both, but a hard failure on a recoverable formatting slip
    would inflate the error rate with something that isn't a compiler defect).
    Does NOT tolerate malformed JSON — that is a real failure.
    """
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise CompilerError(f"No JSON object found in model response: {text[:400]!r}")

    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CompilerError(f"Malformed JSON in model response: {exc}. Raw: {text[:400]!r}") from exc

    if not isinstance(parsed, dict):
        raise CompilerError(f"Model returned {type(parsed).__name__}, expected a JSON object.")
    return parsed


def _coerce_value(field: str, operator: CriterionOperator, value_span: str) -> object:
    """
    Turn a quoted span into a typed criterion value using the registry's
    declared python_type. This is where "the LLM never authors a number"
    actually becomes true: the integer comes from re-reading the user's text.
    """
    spec = get_field_spec(field)
    if operator in (CriterionOperator.excludes, CriterionOperator.contains):
        return value_span.strip().lower()
    if operator == CriterionOperator.semantic:
        return value_span.strip().lower()
    if spec.python_type is int:
        return _parse_int(value_span)
    if spec.python_type is Decimal:
        # AcceptanceCriterion.value is typed `str | int | float | list` — it has
        # no Decimal member, and coercing money through float loses exactness in
        # a system that decides payments. Monetary limits are carried on
        # Obligation.budget_ceiling (a real Decimal field) instead, which is
        # where the corpus models them too, so nothing is lost by refusing here.
        raise ValueError(
            f"monetary criteria are carried on Obligation.budget_ceiling, not as an "
            f"AcceptanceCriterion (field {field!r})"
        )
    if spec.python_type is datetime:
        # Same reasoning: deadlines live on Obligation.delivery_window.
        raise ValueError(
            f"datetime-valued criteria are expressed via delivery_window, not a criterion "
            f"(field {field!r})"
        )
    return value_span.strip().lower()


# ── Compiler ──────────────────────────────────────────────────────────────

def build_prompt(instruction: str, prompt_file: str = PROMPT_FILE) -> str:
    template = load_prompt(prompt_file)
    registry_lines = "\n".join(
        f"- `{path}` — {get_field_spec(path).description}" for path in all_paths()
    )
    return template.replace("{FIELD_REGISTRY}", registry_lines).replace(
        "{INSTRUCTION}", instruction
    )


def compile_obligation(
    instruction: str,
    *,
    client: LLMClient,
    user_id: str = "unknown",
    reference_date: datetime | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    effort: str = "medium",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    created_at: datetime | None = None,
) -> CompilationResult:
    """
    Compile a natural-language instruction into a bound Obligation.

    `reference_date` anchors relative deadlines ("by 9pm"). It defaults to the
    corpus reference date rather than `datetime.now()` so that compiling the
    same instruction twice always yields the same obligation hash — a
    now()-dependent compiler cannot have a reproducible eval.
    """
    reference = reference_date or datetime(2026, 8, 28, tzinfo=timezone.utc)
    frozen_created_at = created_at or datetime(2026, 8, 28, tzinfo=timezone.utc)

    prompt_file, prompt_version_string = resolve_prompt(prompt_version)

    request = LLMRequest(
        system=SYSTEM_PROMPT,
        prompt=build_prompt(instruction, prompt_file),
        max_tokens=max_tokens,
        # temperature=0 for reproducibility. Providers that reject sampling
        # parameters (current Anthropic models) ignore this field — see
        # core/llm/client.py.
        temperature=temperature,
        effort=effort,
        prompt_version=prompt_version_string,
        model=model,
    )
    response = client.complete(request)
    payload = _extract_json(response.text)

    unresolvable_paths: list[str] = []
    dropped: list[str] = []
    criteria: list[AcceptanceCriterion] = []

    for index, raw in enumerate(payload.get("criteria") or []):
        if not isinstance(raw, dict):
            dropped.append(f"criteria[{index}]: not an object")
            continue

        field = str(raw.get("field", "")).strip()
        operator_raw = str(raw.get("operator", "")).strip()
        value_span = str(raw.get("value_span", "") or "")
        source_raw = str(raw.get("source", "")).strip()
        evidence_span = str(raw.get("evidence_span", "") or "")

        # 1. Closed-registry check. An invented path is counted, never used.
        try:
            get_field_spec(field)
        except KeyError:
            unresolvable_paths.append(field)
            dropped.append(f"criteria[{index}]: field {field!r} not in registry")
            continue

        try:
            operator = CriterionOperator(operator_raw)
        except ValueError:
            dropped.append(f"criteria[{index}]: unknown operator {operator_raw!r}")
            continue

        # 2. Span guard — the model may only quote the user.
        try:
            assert_quoted_span(value_span, instruction, context=f"criteria[{index}].value_span")
        except OutputValidationError as exc:
            dropped.append(f"criteria[{index}]: {exc}")
            continue

        # 3. Deterministic value extraction from the quoted span.
        try:
            value = _coerce_value(field, operator, value_span)
        except (ValueError, KeyError) as exc:
            dropped.append(f"criteria[{index}]: could not parse value from {value_span!r} ({exc})")
            continue

        # 4. source labelling. `stated` requires evidence that is genuinely in
        #    the instruction; an unbacked `stated` is demoted to `inferred`
        #    rather than trusted, because a false `stated` causes a false block.
        try:
            source = CriterionSource(source_raw)
        except ValueError:
            dropped.append(f"criteria[{index}]: unknown source {source_raw!r}")
            continue

        if source == CriterionSource.stated and not is_quoted_span(evidence_span, instruction):
            source = CriterionSource.inferred
            dropped.append(
                f"criteria[{index}]: 'stated' demoted to 'inferred' — evidence_span "
                f"{evidence_span!r} is not present in the instruction"
            )

        criteria.append(
            AcceptanceCriterion(
                id=f"compiled-{index}",
                field=field,
                operator=operator,
                value=value,   # type: ignore[arg-type]
                source=source,
                confidence=1.0,
            )
        )

    # ── Obligation-level fields (also span-guarded) ────────────────────────
    budget_ceiling: Decimal | None = None
    budget_span = payload.get("budget_ceiling_span")
    if budget_span:
        try:
            assert_quoted_span(str(budget_span), instruction, context="budget_ceiling_span")
            budget_ceiling = _parse_money(str(budget_span))
        except (OutputValidationError, ValueError) as exc:
            dropped.append(f"budget_ceiling_span: {exc}")

    delivery_window: DeliveryWindow | None = None
    deadline_span = payload.get("delivery_deadline_span")
    if deadline_span:
        try:
            assert_quoted_span(str(deadline_span), instruction, context="delivery_deadline_span")
            delivery_window = DeliveryWindow(latest_by=_parse_deadline(str(deadline_span), reference))
        except (OutputValidationError, ValueError) as exc:
            dropped.append(f"delivery_deadline_span: {exc}")

    merchant_ids: list[str] = []
    merchant_category: str | None = None
    merchant_span = payload.get("merchant_span")
    if merchant_span:
        try:
            assert_quoted_span(str(merchant_span), instruction, context="merchant_span")
            merchant_ids = _resolve_merchant_ids(str(merchant_span))
            if not merchant_ids:
                dropped.append(f"merchant_span: {merchant_span!r} matched no catalogue merchant")
        except OutputValidationError as exc:
            dropped.append(f"merchant_span: {exc}")

    category_span = payload.get("merchant_category_span")
    if category_span and not merchant_ids:
        try:
            assert_quoted_span(str(category_span), instruction, context="merchant_category_span")
            merchant_category = _resolve_merchant_category(str(category_span))
        except OutputValidationError as exc:
            dropped.append(f"merchant_category_span: {exc}")

    prohibited: list[str] = []
    for index, raw_span in enumerate(payload.get("prohibited_spans") or []):
        try:
            assert_quoted_span(str(raw_span), instruction, context=f"prohibited_spans[{index}]")
            prohibited.append(str(raw_span).strip().lower())
        except OutputValidationError as exc:
            dropped.append(f"prohibited_spans[{index}]: {exc}")

    # ── Ambiguity: lexical rules unioned with the model's own flags ─────────
    llm_flags: list[AmbiguityFlag] = []
    for raw_flag in payload.get("ambiguity_flags") or []:
        if not isinstance(raw_flag, dict):
            continue
        code = str(raw_flag.get("code", "")).strip().upper()
        span = str(raw_flag.get("span", "") or "").strip()
        if not code:
            continue
        # Ambiguity spans are free-ish text; if the model quoted the user we
        # keep the quote, otherwise we keep the code but drop the unquoted span
        # so no authored text enters the record.
        if span and not is_quoted_span(span, instruction):
            dropped.append(f"ambiguity_flags: span {span!r} not in instruction; kept code only")
            span = ""
        llm_flags.append(AmbiguityFlag(code, span, "llm"))

    lexical_flags = detect_lexical(instruction)
    lexical_flags += detect_unstated_quantity(instruction, item_count=1)
    flags = merge_flags(lexical_flags, llm_flags)

    # Obligation.id defaults to a random uuid4. That would make the obligation
    # hash differ between two compilations of the SAME instruction, which would
    # quietly defeat the reproducibility the response cache exists to provide —
    # the LLM output would replay identically and the hash still wouldn't match.
    # Derive it instead, so identical inputs give an identical bound obligation.
    obligation_id = "obl-" + hashlib.sha256(
        f"{user_id}::{instruction}::{prompt_version_string}".encode("utf-8")
    ).hexdigest()[:24]

    obligation = Obligation(
        id=obligation_id,
        raw_instruction=instruction,
        user_id=user_id,
        acceptance_criteria=criteria,
        prohibited=prohibited,
        budget_ceiling=budget_ceiling,
        merchant_scope=MerchantScope(merchant_ids=merchant_ids, category=merchant_category),
        delivery_window=delivery_window,
        ambiguity_flags=[f.code for f in flags],
        created_at=frozen_created_at,
    )

    bound = bind(obligation)   # hard-fails on any unresolvable path that slipped through

    return CompilationResult(
        obligation=bound,
        ambiguity_flags=flags,
        clarify=bool(flags),
        clarifying_question=clarifying_question(flags),
        unresolvable_paths=unresolvable_paths,
        dropped_criteria=dropped,
        raw_response=response.text,
        llm_response=response,
    )

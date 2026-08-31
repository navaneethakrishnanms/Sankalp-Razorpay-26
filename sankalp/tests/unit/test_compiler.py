"""
Unit tests for core/obligation/{compiler,binder,ambiguity}.py.

No API key and no network — a RecordingProvider supplies canned model
responses, which is how the adversarial cases (hallucinated amounts, invented
field paths, unbacked `stated` labels) can be exercised deterministically.
Those cases are the point: a compiler that behaves on well-formed output but
trusts a hallucination is not safe.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.llm.client import LLMClient, LLMRequest, LLMResponse
from core.models.enums import CriterionOperator, CriterionSource
from core.models.obligation import AcceptanceCriterion, Obligation
from core.obligation.ambiguity import (
    AmbiguityFlag,
    clarifying_question,
    detect_lexical,
    detect_unstated_quantity,
    merge_flags,
)
from core.obligation.binder import BindError, bind, validate_fields
from core.obligation.compiler import (
    CompilerError,
    _extract_json,
    _parse_deadline,
    _parse_int,
    _parse_money,
    _resolve_merchant_category,
    _resolve_merchant_ids,
    compile_obligation,
)

REFERENCE = datetime(2026, 8, 28, tzinfo=timezone.utc)
INSTRUCTION = "Order dinner for 4 people from Biryani House. No beef. Keep it under ₹1500. It should arrive by 9pm."


class ScriptedProvider:
    name = "anthropic"

    def __init__(self, payload) -> None:
        self.payload = payload if isinstance(payload, str) else json.dumps(payload)

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.payload, input_tokens=900, output_tokens=180,
                            model=request.model, from_cache=False, latency_seconds=0.1)


def compile_with(payload, instruction: str = INSTRUCTION, tmp_path=None):
    client = LLMClient(ScriptedProvider(payload), cache_dir=tmp_path, read_cache=False, write_cache=False)
    return compile_obligation(instruction, client=client, reference_date=REFERENCE)


GOOD_PAYLOAD = {
    "criteria": [
        {"field": "quantity_sum", "operator": "gte", "value_span": "4 people",
         "source": "stated", "evidence_span": "dinner for 4 people"},
        {"field": "item.ingredients", "operator": "excludes", "value_span": "beef",
         "source": "stated", "evidence_span": "No beef"},
    ],
    "budget_ceiling_span": "under ₹1500",
    "delivery_deadline_span": "by 9pm",
    "merchant_span": "Biryani House",
    "merchant_category_span": None,
    "prohibited_spans": ["beef"],
    "ambiguity_flags": [],
}


# ── Value parsing ─────────────────────────────────────────────────────────

class TestValueParsing:
    def test_parse_int(self):
        assert _parse_int("4 people") == 4

    def test_parse_int_hinglish(self):
        assert _parse_int("6 idli") == 6

    def test_parse_int_raises_when_absent(self):
        with pytest.raises(ValueError, match="no integer"):
            _parse_int("some people")

    def test_parse_money_rupee_symbol(self):
        assert _parse_money("under ₹1500") == Decimal("1500")

    def test_parse_money_rs_prefix(self):
        assert _parse_money("under Rs 900") == Decimal("900")

    def test_parse_money_with_separator(self):
        assert _parse_money("₹1,500") == Decimal("1500")

    def test_parse_money_raises_when_absent(self):
        with pytest.raises(ValueError, match="no monetary amount"):
            _parse_money("cheap")

    def test_parse_deadline_pm(self):
        assert _parse_deadline("by 9pm", REFERENCE).hour == 21

    def test_parse_deadline_am(self):
        assert _parse_deadline("by 9am", REFERENCE).hour == 9

    def test_parse_deadline_with_minutes(self):
        result = _parse_deadline("by 8:30pm", REFERENCE)
        assert (result.hour, result.minute) == (20, 30)

    def test_parse_deadline_hinglish_raat(self):
        assert _parse_deadline("raat 9 baje tak", REFERENCE).hour == 21

    def test_parse_deadline_hinglish_sham(self):
        assert _parse_deadline("sham 6 baje tak", REFERENCE).hour == 18

    def test_parse_deadline_bare_hour_defaults_to_pm(self):
        assert _parse_deadline("by 7", REFERENCE).hour == 19

    def test_parse_deadline_is_anchored_to_reference_not_today(self):
        """A now()-dependent compiler cannot have a reproducible eval."""
        result = _parse_deadline("by 9pm", REFERENCE)
        assert (result.year, result.month, result.day) == (2026, 8, 28)


class TestMerchantResolution:
    def test_exact_name_resolves(self):
        assert _resolve_merchant_ids("Biryani House") == ["rest-biryani"]

    def test_case_insensitive(self):
        assert _resolve_merchant_ids("biryani house") == ["rest-biryani"]

    def test_unknown_merchant_resolves_to_nothing(self):
        assert _resolve_merchant_ids("Nonexistent Diner") == []

    def test_category_grocery(self):
        assert _resolve_merchant_category("any grocery store") == "grocery"

    def test_category_food_delivery(self):
        assert _resolve_merchant_category("any food delivery place") == "food_delivery"

    def test_unknown_category(self):
        assert _resolve_merchant_category("any hardware shop") is None


class TestExtractJson:
    def test_plain_object(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_object(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_with_surrounding_prose(self):
        assert _extract_json('Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_malformed_json_raises(self):
        with pytest.raises(CompilerError, match="Malformed JSON"):
            _extract_json('{"a": }')

    def test_no_object_raises(self):
        with pytest.raises(CompilerError, match="No JSON object"):
            _extract_json("I could not do that.")


# ── Happy path ────────────────────────────────────────────────────────────

class TestCompileHappyPath:
    def test_produces_a_bound_obligation(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.obligation.hash != ""
        assert len(result.obligation.hash) == 64

    def test_extracts_both_criteria(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.criteria_emitted == 2

    def test_quantity_value_is_parsed_not_generated(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        qty = next(c for c in result.obligation.acceptance_criteria if c.field == "quantity_sum")
        assert qty.value == 4
        assert isinstance(qty.value, int)

    def test_budget_parsed_from_span(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.obligation.budget_ceiling == Decimal("1500")

    def test_deadline_parsed_from_span(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.obligation.delivery_window is not None
        assert result.obligation.delivery_window.latest_by.hour == 21

    def test_merchant_scope_resolved_to_catalogue_id(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.obligation.merchant_scope.merchant_ids == ["rest-biryani"]

    def test_prohibited_captured(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.obligation.prohibited == ["beef"]

    def test_stated_source_preserved_when_evidence_is_real(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert all(c.source == CriterionSource.stated for c in result.obligation.acceptance_criteria)

    def test_no_clarify_on_a_fully_specified_instruction(self, tmp_path):
        result = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert result.clarify is False
        assert result.clarifying_question is None

    def test_compilation_is_reproducible(self, tmp_path):
        """Same instruction + same model output => byte-identical bound obligation.
        Without a derived obligation id this fails on uuid4 alone, and the whole
        point of the response cache would be lost."""
        a = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        b = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert a.obligation.hash == b.obligation.hash

    def test_obligation_id_is_derived_not_random(self, tmp_path):
        a = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        b = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        assert a.obligation.id == b.obligation.id
        assert a.obligation.id.startswith("obl-")

    def test_different_instructions_get_different_ids(self, tmp_path):
        a = compile_with(GOOD_PAYLOAD, tmp_path=tmp_path)
        empty = {**GOOD_PAYLOAD, "criteria": [], "budget_ceiling_span": None,
                  "delivery_deadline_span": None, "merchant_span": None,
                  "prohibited_spans": []}
        b = compile_with(empty, instruction="Order 2 idli", tmp_path=tmp_path)
        assert a.obligation.id != b.obligation.id


# ── Adversarial: the model misbehaving ────────────────────────────────────

class TestCompilerRejectsAuthoredValues:
    def test_hallucinated_budget_is_dropped_not_trusted(self, tmp_path):
        """The model inflates the ceiling. The span isn't in the instruction,
        so it must not survive."""
        payload = {**GOOD_PAYLOAD, "budget_ceiling_span": "under ₹9000"}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.obligation.budget_ceiling is None
        assert any("budget_ceiling_span" in d for d in result.dropped_criteria)

    def test_hallucinated_criterion_value_is_dropped(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "quantity_sum", "operator": "gte", "value_span": "40 people",
             "source": "stated", "evidence_span": "dinner for 4 people"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.criteria_emitted == 0

    def test_invented_field_path_is_counted_and_dropped(self, tmp_path):
        """Closed registry: an invented path is never silently used."""
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "cart.spiciness_level", "operator": "lte", "value_span": "4 people",
             "source": "stated", "evidence_span": "dinner for 4 people"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.unresolvable_paths == ["cart.spiciness_level"]
        assert result.criteria_emitted == 0

    def test_hallucinated_prohibited_item_is_dropped(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "prohibited_spans": ["peanuts"]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.obligation.prohibited == []

    def test_unbacked_stated_is_demoted_to_inferred(self, tmp_path):
        """A false `stated` causes a false block, so an unverifiable `stated`
        claim is demoted rather than trusted."""
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "quantity_sum", "operator": "gte", "value_span": "4 people",
             "source": "stated", "evidence_span": "the user definitely wanted four"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.obligation.acceptance_criteria[0].source == CriterionSource.inferred

    def test_unknown_operator_is_dropped(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "quantity_sum", "operator": "approximately", "value_span": "4 people",
             "source": "stated", "evidence_span": "dinner for 4 people"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.criteria_emitted == 0

    def test_unknown_merchant_leaves_scope_empty(self, tmp_path):
        result = compile_with(
            {**GOOD_PAYLOAD, "merchant_span": "dinner"}, tmp_path=tmp_path
        )
        assert result.obligation.merchant_scope.merchant_ids == []

    def test_malformed_response_raises_compiler_error(self, tmp_path):
        with pytest.raises(CompilerError):
            compile_with("the model said something conversational", tmp_path=tmp_path)

    def test_empty_criteria_still_produces_a_bound_obligation(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "criteria": []}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.obligation.hash != ""
        assert result.criteria_emitted == 0


# ── Ambiguity ─────────────────────────────────────────────────────────────

class TestAmbiguity:
    def test_lexical_detects_vague_quantifier(self):
        flags = detect_lexical("Get some raita and a couple more snacks")
        assert any(f.code == "VAGUE_QUANTIFIER" for f in flags)

    def test_lexical_detects_hinglish_vague_quantifier(self):
        assert any(f.code == "VAGUE_QUANTIFIER" for f in detect_lexical("kuch snacks bhej do"))

    def test_lexical_detects_subjective_constraint(self):
        flags = detect_lexical("nothing too spicy please")
        assert any(f.code == "SUBJECTIVE_CONSTRAINT" for f in flags)

    def test_lexical_clean_instruction_has_no_flags(self):
        assert detect_lexical("Order 2 Margherita from Pizza Point under ₹900") == []

    def test_unstated_quantity_fires_when_no_digit_present(self):
        flags = detect_unstated_quantity("order milk and bread from FreshMart", item_count=1)
        assert any(f.code == "UNSTATED_QUANTITY" for f in flags)

    def test_unstated_quantity_silent_when_a_digit_exists(self):
        assert detect_unstated_quantity("order 2 milk", item_count=1) == []

    def test_merge_deduplicates(self):
        a = [AmbiguityFlag("VAGUE_QUANTIFIER", "some", "lexical")]
        b = [AmbiguityFlag("VAGUE_QUANTIFIER", "Some", "llm")]
        assert len(merge_flags(a, b)) == 1

    def test_merge_keeps_distinct_codes(self):
        a = [AmbiguityFlag("VAGUE_QUANTIFIER", "some", "lexical")]
        b = [AmbiguityFlag("SUBJECTIVE_CONSTRAINT", "too spicy", "llm")]
        assert len(merge_flags(a, b)) == 2

    def test_clarifying_question_is_none_when_unambiguous(self):
        assert clarifying_question([]) is None

    def test_clarifying_question_prioritises_quantity(self):
        flags = [AmbiguityFlag("SUBJECTIVE_CONSTRAINT", "too spicy", "llm"),
                 AmbiguityFlag("UNSTATED_QUANTITY", "milk and bread", "lexical")]
        assert "How many" in (clarifying_question(flags) or "")

    def test_compiler_routes_ambiguous_instruction_to_clarify(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "criteria": [], "budget_ceiling_span": None,
                    "delivery_deadline_span": None, "merchant_span": None,
                    "prohibited_spans": [],
                    "ambiguity_flags": [{"code": "VAGUE_QUANTIFIER", "span": "some snacks"}]}
        result = compile_with(payload, instruction="Get some snacks", tmp_path=tmp_path)
        assert result.clarify is True
        assert result.clarifying_question is not None

    def test_unquoted_ambiguity_span_is_stripped_but_code_kept(self, tmp_path):
        """The finding is kept (the model may be right that it's ambiguous) but
        the unquoted text is dropped, so no authored prose enters the record."""
        payload = {**GOOD_PAYLOAD,
                    "ambiguity_flags": [{"code": "CONFLICTING_REQUIREMENT",
                                          "span": "a span the user never wrote"}]}
        result = compile_with(payload, tmp_path=tmp_path)
        conflicting = [f for f in result.ambiguity_flags if f.code == "CONFLICTING_REQUIREMENT"]
        assert conflicting, "the flag's code must survive"
        assert all(f.span == "" for f in conflicting), "the unquoted span must be dropped"


class TestPromptVersioning:
    """Prompt iteration against a measured set is a form of fitting. It is only
    honest if every version's numbers survive side by side, which requires the
    version to be selectable and to reach the cache key."""

    def test_both_versions_resolve(self):
        from core.obligation.compiler import PROMPT_VERSIONS, resolve_prompt

        assert set(PROMPT_VERSIONS) >= {"v1", "v2"}
        for version in ("v1", "v2"):
            filename, version_string = resolve_prompt(version)
            assert filename.endswith(".md")
            assert version_string.endswith(f"/{version}")

    def test_unknown_version_raises(self):
        from core.obligation.compiler import resolve_prompt

        with pytest.raises(CompilerError, match="Unknown prompt version"):
            resolve_prompt("v99")

    def test_default_is_v1_until_a_baseline_exists(self):
        from core.obligation.compiler import DEFAULT_PROMPT_VERSION

        assert DEFAULT_PROMPT_VERSION == "v1"

    def test_versions_produce_different_prompts(self):
        from core.obligation.compiler import build_prompt

        v1 = build_prompt("Order 2 Margherita", "obligation_compiler_v1.md")
        v2 = build_prompt("Order 2 Margherita", "obligation_compiler_v2.md")
        assert v1 != v2

    def test_v2_documents_the_floor_convention(self):
        """v1 had no way to know quantities are floors; that is the one axis v2
        changes, so any metric movement is attributable to it."""
        from core.obligation.compiler import build_prompt

        v2 = build_prompt("x", "obligation_compiler_v2.md").lower()
        assert "gte" in v2 and "floor" in v2
        assert "never `eq`" in v2 or "not `eq" in v2

    def test_version_reaches_the_cache_key(self, tmp_path):
        """Two prompt versions must never share a cached response."""
        from core.llm.client import cache_key

        client = LLMClient(ScriptedProvider(GOOD_PAYLOAD), cache_dir=tmp_path)
        a = compile_obligation(INSTRUCTION, client=client, reference_date=REFERENCE,
                                prompt_version="v1")
        b = compile_obligation(INSTRUCTION, client=client, reference_date=REFERENCE,
                                prompt_version="v2")
        assert a.llm_response is not None and b.llm_response is not None
        assert len(list(tmp_path.glob("*.json"))) == 2, "each version needs its own entry"
        _ = cache_key

    def test_version_reaches_the_obligation_id(self, tmp_path):
        client = LLMClient(ScriptedProvider(GOOD_PAYLOAD), cache_dir=tmp_path,
                            read_cache=False, write_cache=False)
        a = compile_obligation(INSTRUCTION, client=client, reference_date=REFERENCE,
                                prompt_version="v1")
        b = compile_obligation(INSTRUCTION, client=client, reference_date=REFERENCE,
                                prompt_version="v2")
        assert a.obligation.id != b.obligation.id


class TestMonetaryAndDatetimeCriteriaAreRefused:
    def test_total_criterion_is_dropped_not_coerced_through_float(self, tmp_path):
        """AcceptanceCriterion.value has no Decimal member; coercing money via
        float in a payments system is worse than declining the criterion.
        Budget limits ride on Obligation.budget_ceiling instead."""
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "total", "operator": "lte", "value_span": "under ₹1500",
             "source": "stated", "evidence_span": "under ₹1500"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.criteria_emitted == 0
        assert any("budget_ceiling" in d for d in result.dropped_criteria)

    def test_budget_still_captured_on_the_obligation_field(self, tmp_path):
        """Refusing the criterion must not lose the limit."""
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "total", "operator": "lte", "value_span": "under ₹1500",
             "source": "stated", "evidence_span": "under ₹1500"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.obligation.budget_ceiling == Decimal("1500")

    def test_fulfilment_eta_criterion_is_dropped(self, tmp_path):
        payload = {**GOOD_PAYLOAD, "criteria": [
            {"field": "fulfilment_eta", "operator": "lte", "value_span": "by 9pm",
             "source": "stated", "evidence_span": "arrive by 9pm"},
        ]}
        result = compile_with(payload, tmp_path=tmp_path)
        assert result.criteria_emitted == 0
        assert result.obligation.delivery_window is not None   # captured properly instead


# ── Binder ────────────────────────────────────────────────────────────────

def make_obligation(**overrides) -> Obligation:
    # id and created_at are pinned: both default to non-deterministic values
    # (uuid4, now()), which would make hash-equality tests meaningless.
    defaults = dict(
        id="obl-fixed-for-test", raw_instruction="test", user_id="u1",
        acceptance_criteria=[AcceptanceCriterion(
            id="c1", field="quantity_sum", operator=CriterionOperator.gte,
            value=4, source=CriterionSource.stated, confidence=1.0)],
        created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Obligation(**defaults)


class TestBinder:
    def test_bind_stamps_a_hash(self):
        assert len(bind(make_obligation()).hash) == 64

    def test_bind_is_deterministic(self):
        assert bind(make_obligation()).hash == bind(make_obligation()).hash

    def test_validate_accepts_registered_paths(self):
        validate_fields(make_obligation())   # must not raise

    def test_validate_rejects_unregistered_path(self):
        obligation = make_obligation(acceptance_criteria=[AcceptanceCriterion(
            id="c1", field="cart.made_up_field", operator=CriterionOperator.gte,
            value=4, source=CriterionSource.stated, confidence=1.0)])
        with pytest.raises(BindError, match="not in the SANKALP field registry"):
            validate_fields(obligation)

    def test_bind_rejects_unregistered_path(self):
        obligation = make_obligation(acceptance_criteria=[AcceptanceCriterion(
            id="c1", field="nope", operator=CriterionOperator.gte,
            value=4, source=CriterionSource.stated, confidence=1.0)])
        with pytest.raises(BindError):
            bind(obligation)

    def test_bind_error_names_every_offending_criterion(self):
        obligation = make_obligation(acceptance_criteria=[
            AcceptanceCriterion(id="a", field="bad.one", operator=CriterionOperator.gte,
                                 value=1, source=CriterionSource.stated, confidence=1.0),
            AcceptanceCriterion(id="b", field="bad.two", operator=CriterionOperator.gte,
                                 value=1, source=CriterionSource.stated, confidence=1.0),
        ])
        with pytest.raises(BindError) as exc:
            bind(obligation)
        assert "bad.one" in str(exc.value) and "bad.two" in str(exc.value)

    def test_double_bind_rejected(self):
        bound = bind(make_obligation())
        with pytest.raises(BindError, match="already bound"):
            bind(bound)

    def test_empty_criteria_binds_fine(self):
        assert bind(make_obligation(acceptance_criteria=[])).hash != ""

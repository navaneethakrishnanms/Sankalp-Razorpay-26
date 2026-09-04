"""
Unit tests for eval/compiler_harness.py.

No API key and no network: a scripted provider stands in for the model, so
these tests exercise the measurement plumbing (matching, scoring, the delta)
rather than model quality. Model quality is measured by an actual recorded
run — see `make eval-stage4`.
"""

from __future__ import annotations

import json

import pytest

from core.llm.client import LLMClient, LLMRequest, LLMResponse
from eval.compiler_harness import (
    _criterion_signature,
    blocked_by_composite,
    caught_by_composite,
    run_compiler_eval,
)
from eval.harness import load_records, load_split, record_to_models

EMPTY_PAYLOAD = {
    "criteria": [],
    "budget_ceiling_span": None,
    "delivery_deadline_span": None,
    "merchant_span": None,
    "merchant_category_span": None,
    "prohibited_spans": [],
    "ambiguity_flags": [],
}


class ScriptedProvider:
    name = "anthropic"

    def __init__(self, payload=None) -> None:
        self.payload = json.dumps(payload if payload is not None else EMPTY_PAYLOAD)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.payload, input_tokens=800, output_tokens=120,
                            model=request.model, from_cache=False, latency_seconds=0.05)


@pytest.fixture
def scripted_client(tmp_path):
    return LLMClient(ScriptedProvider(), cache_dir=tmp_path, read_cache=False, write_cache=False)


# ── Criterion matching ────────────────────────────────────────────────────

class TestCriterionSignature:
    def test_case_insensitive_on_value(self):
        assert _criterion_signature("item.ingredients", "excludes", "Beef") == \
               _criterion_signature("item.ingredients", "excludes", "beef")

    def test_int_and_str_value_match(self):
        """Gold stores 4; a compiled criterion also parses to 4 — these must match."""
        assert _criterion_signature("quantity_sum", "gte", 4) == \
               _criterion_signature("quantity_sum", "gte", "4")

    def test_different_field_does_not_match(self):
        assert _criterion_signature("quantity_sum", "gte", 4) != \
               _criterion_signature("item_count", "gte", 4)

    def test_different_operator_does_not_match(self):
        assert _criterion_signature("quantity_sum", "gte", 4) != \
               _criterion_signature("quantity_sum", "lte", 4)

    def test_source_is_deliberately_not_part_of_identity(self):
        """Source is scored separately on matched pairs; folding it into identity
        would turn every source mislabel into a spurious missed+invented pair."""
        sig = _criterion_signature("quantity_sum", "gte", 4)
        assert len(sig) == 3   # (field, operator, value) only

    def test_semantic_criteria_match_on_field_and_operator_only(self):
        """The corpus stores an authored label ('not_too_spicy'); the compiler can
        only quote the user ('nothing too spicy'). Comparing those would score
        every semantic criterion as both missed and invented — punishing the
        compiler for obeying the no-authored-values rule."""
        gold = _criterion_signature("item.categories", "semantic", "not_too_spicy")
        compiled = _criterion_signature("item.categories", "semantic", "nothing too spicy")
        assert gold == compiled

    def test_semantic_collapse_does_not_leak_into_other_operators(self):
        """Only `semantic` is value-insensitive. An `excludes` criterion must
        still distinguish 'beef' from 'chicken', or dietary extraction would be
        scored as correct when it is dangerously wrong."""
        assert _criterion_signature("item.ingredients", "excludes", "beef") != \
               _criterion_signature("item.ingredients", "excludes", "chicken")

    def test_semantic_still_distinguishes_fields(self):
        assert _criterion_signature("item.categories", "semantic", "x") != \
               _criterion_signature("item.names", "semantic", "x")


# ── ID-independent caught/blocked ─────────────────────────────────────────

class TestCaughtByComposite:
    @pytest.fixture(scope="class")
    def corpus(self):
        return load_records(), load_split()

    def _find(self, records, violation_class, **labels):
        for r in records:
            if r["labels"]["violation_class"] != violation_class:
                continue
            if all(r["labels"].get(k) == v for k, v in labels.items()):
                return r
        raise AssertionError(f"no record for {violation_class} {labels}")

    def test_gold_obligation_catches_a_budget_breach(self, corpus):
        records, _ = corpus
        record = self._find(records, "BUDGET_BREACH")
        obligation, cart = record_to_models(record)
        assert caught_by_composite(record, obligation, cart) is True

    def test_gold_obligation_catches_a_catchable_quantity_mismatch(self, corpus):
        records, _ = corpus
        record = self._find(records, "QUANTITY_MISMATCH", verifier_catchable=True)
        obligation, cart = record_to_models(record)
        assert caught_by_composite(record, obligation, cart) is True

    def test_gold_obligation_misses_an_uncatchable_quantity_mismatch(self, corpus):
        records, _ = corpus
        record = self._find(records, "QUANTITY_MISMATCH", verifier_catchable=False)
        obligation, cart = record_to_models(record)
        assert caught_by_composite(record, obligation, cart) is False

    def test_abstain_expected_record_is_caught_via_abstain_not_fail(self, corpus):
        records, _ = corpus
        record = self._find(records, "CONSTRAINT_VIOLATION", abstain_expected=True)
        obligation, cart = record_to_models(record)
        assert caught_by_composite(record, obligation, cart) is True

    def test_clean_record_is_not_blocked_under_gold(self, corpus):
        records, _ = corpus
        record = self._find(records, "CLEAN")
        obligation, cart = record_to_models(record)
        assert blocked_by_composite(obligation, cart) is False

    def test_empty_obligation_blocks_nothing(self, corpus):
        """Sanity anchor for the delta: an obligation with no criteria and no
        limits cannot block anything, so a compiler that extracts nothing
        scores 0% recall rather than crashing."""
        records, _ = corpus
        record = self._find(records, "BUDGET_BREACH")
        _, cart = record_to_models(record)
        from core.models.obligation import Obligation
        empty = Obligation(raw_instruction="x", user_id="u", acceptance_criteria=[])
        assert blocked_by_composite(empty, cart) is False


# ── End-to-end plumbing ───────────────────────────────────────────────────

class TestRunCompilerEval:
    @pytest.fixture(scope="class")
    def metrics(self, tmp_path_factory):
        cache = tmp_path_factory.mktemp("cache")
        client = LLMClient(ScriptedProvider(), cache_dir=cache, read_cache=False, write_cache=False)
        return run_compiler_eval(client=client, limit_seeds=4)

    def test_produces_every_expected_section(self, metrics):
        for key in ("provenance", "criterion_extraction", "source_labelling",
                     "unresolvable_paths", "ambiguity_detection", "per_language",
                     "cost", "latency_seconds", "delta_vs_gold_criteria", "per_seed"):
            assert key in metrics, key

    def test_provenance_pins_the_exact_configuration(self, metrics):
        """A number without its model identifier is not reproducible."""
        p = metrics["provenance"]
        for field in ("provider", "model", "temperature", "reasoning_effort",
                       "prompt_version", "prompt_file"):
            assert field in p and p[field] is not None, field

    def test_provenance_records_temperature_zero(self, metrics):
        """temperature>0 would make the same instruction compile differently each
        run, voiding both the cache and the metrics."""
        assert metrics["provenance"]["temperature"] == 0.0

    def test_provenance_flags_unverified_pricing(self, metrics):
        assert "pricing_verified" in metrics["provenance"]

    def test_provenance_carries_no_credential(self, metrics):
        blob = json.dumps(metrics["provenance"])
        assert "gsk" not in blob and "sk-ant" not in blob
        assert "api_key" not in blob.lower()

    def test_scope_is_train_only(self, metrics):
        assert "train" in metrics["scope"]

    def test_respects_the_seed_limit(self, metrics):
        assert metrics["seeds_compiled"] == 4

    def test_compiles_once_per_seed_not_once_per_record(self, metrics):
        """1,359 records expand from 45 seeds; compiling per record would cost
        ~30x and make the CIs meaningless (samples would not be independent)."""
        assert metrics["seeds_compiled"] < metrics["train_records_in_scope"]

    def test_empty_extraction_scores_zero_recall_not_an_error(self, metrics):
        """The scripted model extracts nothing. That must be reported as 0%
        recall, not crash and not silently look like success."""
        assert metrics["criterion_extraction"]["captured"] == 0
        assert metrics["criterion_extraction"]["recall"]["rate"] == 0.0

    def test_delta_reports_both_sides(self, metrics):
        d = metrics["delta_vs_gold_criteria"]
        assert d["gold_recall"]["n"] > 0
        assert "compiled_recall" in d and "recall_delta" in d

    def test_delta_is_negative_when_compiler_extracts_nothing(self, metrics):
        """A compiler that extracts nothing must show a real recall cost."""
        assert metrics["delta_vs_gold_criteria"]["recall_delta"] <= 0.0

    def test_gold_side_of_delta_catches_most_violations(self, metrics):
        """Anchors the comparison: the gold obligations are the Stage 3 result."""
        assert metrics["delta_vs_gold_criteria"]["gold_recall"]["rate"] > 0.5

    def test_every_rate_carries_a_wilson_interval(self, metrics):
        for section in ("criterion_extraction", "source_labelling"):
            for key, value in metrics[section].items():
                if isinstance(value, dict) and "rate" in value:
                    assert "ci_low" in value and "ci_high" in value, f"{section}.{key}"

    def test_ambiguity_note_discloses_label_noise(self, metrics):
        """The ambiguity label is the noisiest in the corpus; the report must
        say so rather than presenting agreement as ground truth."""
        assert "noisiest label" in metrics["ambiguity_detection"]["note"]

    def test_cost_is_reported_in_rupees(self, metrics):
        assert "total_inr" in metrics["cost"]
        assert float(metrics["cost"]["total_inr"]) > 0

    def test_per_seed_detail_is_present(self, metrics):
        assert len(metrics["per_seed"]) == 4
        assert all("seed_id" in row for row in metrics["per_seed"])

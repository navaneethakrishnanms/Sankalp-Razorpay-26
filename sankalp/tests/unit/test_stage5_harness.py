"""
Offline smoke tests for eval/stage5_harness.py — a scripted client, zero
network calls. Catches wiring bugs (bad field access, wrong record shape)
before spending real Groq quota on the ~148-call live subset run.
"""

from __future__ import annotations

from core.llm.client import LLMClient, LLMRequest, LLMResponse
from eval.harness import load_records, load_split
from eval.stage5_harness import (
    HoldoutSealedError,
    run_clean_semantic_subset,
    run_deceptive_subset,
    run_uncatchable_semantic_subset,
    select_subset,
)

import pytest


class AlwaysPassProvider:
    name = "groq"

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text='{"verdict": "PASS", "confidence": 0.95, "reasoning": "looks fine"}',
            input_tokens=200, output_tokens=30, model=request.model,
            from_cache=False, latency_seconds=0.01,
        )


def scripted_client(tmp_path) -> LLMClient:
    return LLMClient(AlwaysPassProvider(), cache_dir=tmp_path, read_cache=False, write_cache=False)


class TestSubsetSelection:
    def test_subset_is_train_only(self):
        records = load_records()
        split = load_split()
        subset = select_subset(records, split)
        for group in subset.values():
            for r in group:
                assert split[r["order_id"]] == "train"

    def test_deceptive_matches_labels(self):
        records = load_records()
        subset = select_subset(records, load_split())
        assert all(r["labels"]["self_report_deceptive"] for r in subset["deceptive"])
        assert len(subset["deceptive"]) > 0

    def test_uncatchable_semantic_matches_labels(self):
        records = load_records()
        subset = select_subset(records, load_split())
        for r in subset["uncatchable_semantic"]:
            assert r["labels"]["violation_class"] == "CONSTRAINT_VIOLATION"
            assert r["labels"]["verifier_catchable"] is False
            assert r["labels"]["abstain_expected"] is False
        assert len(subset["uncatchable_semantic"]) > 0

    def test_clean_semantic_has_a_semantic_criterion(self):
        records = load_records()
        subset = select_subset(records, load_split())
        for r in subset["clean_semantic"]:
            assert r["labels"]["violation_class"] == "CLEAN"
            assert any(c["operator"] == "semantic" for c in r["obligation"]["acceptance_criteria"])
        assert len(subset["clean_semantic"]) > 0

    def test_clean_semantic_sample_is_capped(self):
        from eval.stage5_harness import CLEAN_SEMANTIC_SAMPLE_SIZE
        subset = select_subset(load_records(), load_split())
        assert len(subset["clean_semantic"]) <= CLEAN_SEMANTIC_SAMPLE_SIZE


class TestDeceptiveSubsetOffline:
    def test_runs_without_error_on_a_small_slice(self, tmp_path):
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"][:3]
        results = run_deceptive_subset(subset, scripted_client(tmp_path))
        assert len(results) <= 3
        for r in results:
            assert r.semantic_verdict is not None

    def test_confident_pass_on_self_report_is_recorded(self, tmp_path):
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"][:2]
        results = run_deceptive_subset(subset, scripted_client(tmp_path))
        for r in results:
            assert r.semantic_confidence == pytest.approx(0.95)

    def test_with_floor_catches_more_or_equal_than_without(self, tmp_path):
        """Floor enforcement should never make the deceptive case WORSE — it
        excludes the fooled verdict, it does not add new failures."""
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"][:5]
        results = run_deceptive_subset(subset, scripted_client(tmp_path))
        caught_with = sum(1 for r in results if r.caught_with_floor)
        caught_without = sum(1 for r in results if r.caught_without_floor)
        assert caught_with >= caught_without

    def test_two_populations_exist(self, tmp_path):
        """Population A (deterministic backup present) and population B
        (semantic-only, the true fooled-judge test) must both appear in the
        full deceptive subset — conflating them hid the real result (see
        FAILURES.md's Stage 5 entry: it measured a 0.0% gap because pop A's
        absolute `stated` FAIL silently swallowed pop B, the only place the
        gap is real)."""
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"]
        results = run_deceptive_subset(subset, scripted_client(tmp_path))
        assert any(r.has_deterministic_backup for r in results)
        assert any(not r.has_deterministic_backup for r in results)

    def test_population_b_has_no_deterministic_backup_by_construction(self, tmp_path):
        """Population B records are QUANTITY_MISMATCH:uncatchable — no
        criterion in the registry can express what was violated, so no
        deterministic verifier output exists for them at all."""
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"]
        pop_b_records = [r for r in subset if not r["labels"]["violating_criterion_ids"]]
        assert pop_b_records
        for r in pop_b_records:
            assert r["labels"]["violation_class"] == "QUANTITY_MISMATCH"
            assert r["labels"]["verifier_catchable"] is False

    def test_with_always_pass_provider_population_b_would_wrongly_clear_without_floor(self, tmp_path):
        """The load-bearing counterfactual, made concrete: a confident SELF-only
        PASS with the floor off must let the payment clear (EXECUTE) — the
        exact failure floor enforcement exists to prevent."""
        from core.models.enums import SettlementAction
        records = load_records()
        subset = select_subset(records, load_split())["deceptive"]
        pop_b = [r for r in subset if not r["labels"]["violating_criterion_ids"]][:3]
        results = run_deceptive_subset(pop_b, scripted_client(tmp_path))
        assert results, "population B smoke slice produced no results"
        # With AlwaysPassProvider and no deterministic backup, the floor-off
        # counterfactual must fail to catch (the payment wrongly clears).
        assert any(not r.caught_without_floor for r in results)
        # With the floor on, the same confident PASS is excluded and the
        # engine must NOT wrongly clear.
        assert all(r.caught_with_floor for r in results)


class TestUncatchableSemanticOffline:
    def test_runs_without_error(self, tmp_path):
        records = load_records()
        subset = select_subset(records, load_split())["uncatchable_semantic"][:3]
        results = run_uncatchable_semantic_subset(subset, scripted_client(tmp_path))
        assert len(results) == 3


class TestCleanSemanticOffline:
    def test_runs_without_error(self, tmp_path):
        records = load_records()
        subset = select_subset(records, load_split())["clean_semantic"][:3]
        results = run_clean_semantic_subset(subset, scripted_client(tmp_path))
        assert len(results) == 3
        for r in results:
            assert r.false_blocked is False   # AlwaysPassProvider never fails

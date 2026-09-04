"""
Unit tests for eval/harness.py and eval/stats.py.

TestWilsonCI            interval properties (monotonic, contains point estimate, bounds)
TestRecordToModels       corpus record -> Obligation/Cart round-trip
TestEvaluateRecord       per-violation-class "caught" logic on hand-built records
TestComputeMetrics       aggregate correctness on a small synthetic result set
TestFullCorpusRun        end-to-end sanity on the real committed corpus — this is
                         where the Stage 3 "what a correct result looks like"
                         invariants (100% catchable, 0% uncatchable, 0 unexpected
                         misses, 0 mislabels, 0 false-block) are actually asserted
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models.enums import Verdict
from eval.baselines import evaluate_baselines
from eval.generator import build_corpus, build_split, records_jsonl
from eval.harness import (
    compute_metrics,
    evaluate_record,
    load_records,
    load_split,
    record_to_models,
    run_harness,
)
from eval.stats import rate, wilson_ci


# ── Wilson CI ────────────────────────────────────────────────────────────

class TestWilsonCI:
    def test_bounds_within_0_1(self):
        for successes, n in [(0, 10), (10, 10), (5, 10), (1, 1000), (999, 1000)]:
            lo, hi = wilson_ci(successes, n)
            assert 0.0 <= lo <= hi <= 1.0

    def test_zero_n_returns_zero(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)

    def test_interval_contains_point_estimate(self):
        lo, hi = wilson_ci(7, 10)
        assert lo <= 0.7 <= hi

    def test_interval_narrows_with_more_data(self):
        lo_small, hi_small = wilson_ci(7, 10)
        lo_large, hi_large = wilson_ci(700, 1000)
        assert (hi_large - lo_large) < (hi_small - lo_small)

    def test_100_percent_interval_does_not_include_0(self):
        """Near 100%, Wilson (unlike the normal approximation) stays sane."""
        lo, hi = wilson_ci(50, 50)
        assert hi == 1.0
        assert lo > 0.9

    def test_0_percent_interval_does_not_include_1(self):
        lo, hi = wilson_ci(0, 50)
        assert lo == 0.0
        assert hi < 0.1

    def test_rate_helper_matches_wilson_ci(self):
        r = rate(26, 26)
        lo, hi = wilson_ci(26, 26)
        assert r.ci_low == lo and r.ci_high == hi
        assert r.rate == 1.0


# ── record_to_models ────────────────────────────────────────────────────

class TestRecordToModels:
    def test_roundtrip_preserves_cart_total(self):
        record = load_records()[0]
        obligation, cart = record_to_models(record)
        assert str(cart.total) == record["cart"]["total"]

    def test_roundtrip_preserves_criteria_count(self):
        record = load_records()[0]
        obligation, cart = record_to_models(record)
        assert len(obligation.acceptance_criteria) == len(record["obligation"]["acceptance_criteria"])

    def test_budget_ceiling_none_preserved(self):
        records = load_records()
        record = next(r for r in records if r["obligation"]["budget_ceiling"] is None)
        obligation, _ = record_to_models(record)
        assert obligation.budget_ceiling is None

    def test_delivery_window_none_preserved(self):
        records = load_records()
        record = next(r for r in records if r["obligation"]["delivery_window"] is None)
        obligation, _ = record_to_models(record)
        assert obligation.delivery_window is None


# ── evaluate_record: per-class "caught" logic ──────────────────────────────

class TestEvaluateRecord:
    @pytest.fixture(scope="class")
    def records_by_id(self):
        return {r["order_id"]: r for r in load_records()}

    @pytest.fixture(scope="class")
    def split(self):
        return load_split()

    def _first(self, records_by_id, split, violation_class, **label_filters):
        for oid, r in records_by_id.items():
            if r["labels"]["violation_class"] != violation_class:
                continue
            if all(r["labels"].get(k) == v for k, v in label_filters.items()):
                return evaluate_record(r, split[oid])
        raise AssertionError(f"no record found for {violation_class} {label_filters}")

    def test_clean_record_has_no_caught_value(self, records_by_id, split):
        result = self._first(records_by_id, split, "CLEAN")
        assert result.caught is None

    def test_quantity_catchable_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "QUANTITY_MISMATCH", verifier_catchable=True)
        assert result.caught is True

    def test_quantity_uncatchable_is_not_caught_and_not_mislabelled(self, records_by_id, split):
        result = self._first(records_by_id, split, "QUANTITY_MISMATCH", verifier_catchable=False)
        assert result.caught is False
        assert result.mislabel is False

    def test_constraint_catchable_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "CONSTRAINT_VIOLATION",
                              abstain_expected=False, verifier_catchable=True)
        assert result.caught is True

    def test_constraint_abstain_expected_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "CONSTRAINT_VIOLATION", abstain_expected=True)
        assert result.caught is True
        assert result.detail.criterion_verdicts
        assert all(
            v == Verdict.ABSTAIN for cid, v in result.detail.criterion_verdicts.items()
        ) or any(v == Verdict.ABSTAIN for v in result.detail.criterion_verdicts.values())

    def test_constraint_uncatchable_semantic_is_not_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "CONSTRAINT_VIOLATION",
                              abstain_expected=False, verifier_catchable=False)
        assert result.caught is False
        assert result.mislabel is False   # semantic criteria are structurally never evaluated

    def test_budget_breach_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "BUDGET_BREACH")
        assert result.caught is True
        assert result.detail.budget_verdict == Verdict.FAIL

    def test_wrong_merchant_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "WRONG_MERCHANT")
        assert result.caught is True
        assert result.detail.merchant_scope_verdict == Verdict.FAIL

    def test_timing_miss_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "TIMING_MISS")
        assert result.caught is True
        assert result.detail.delivery_verdict == Verdict.FAIL

    def test_total_misdeclared_is_caught(self, records_by_id, split):
        result = self._first(records_by_id, split, "TOTAL_MISDECLARED")
        assert result.caught is True
        assert result.detail.total_arithmetic_verdict == Verdict.FAIL

    def test_subpopulation_key_matches_label(self, records_by_id, split):
        result = self._first(records_by_id, split, "BUDGET_BREACH")
        assert result.subpopulation == "BUDGET_BREACH"

    def test_latency_is_recorded_and_nonnegative(self, records_by_id, split):
        result = self._first(records_by_id, split, "CLEAN")
        assert result.latency_seconds >= 0.0


# ── compute_metrics on a tiny synthetic set ─────────────────────────────────

class TestComputeMetrics:
    def test_empty_results_do_not_crash(self):
        m = compute_metrics([])
        assert m["record_count"] == 0
        assert m["headline"]["recall_excl_total_misdeclared"]["n"] == 0

    def test_full_run_on_real_corpus_produces_all_expected_top_level_keys(self):
        m = run_harness()
        for key in ("headline", "secondary_diagnostic_within_expressive_power",
                     "per_subpopulation_recall", "abstain_accuracy",
                     "verifier_catchable_audit", "false_block", "latency_seconds",
                     "split", "language", "misses", "misses_summary", "not_yet_exercised"):
            assert key in m, key


# ── Full corpus run — the Stage 3 correctness invariants ───────────────────

class TestFullCorpusRun:
    @pytest.fixture(scope="class")
    def metrics(self):
        return run_harness()

    def test_catchable_subpopulations_are_fully_caught(self, metrics):
        for key in ("QUANTITY_MISMATCH:catchable", "CONSTRAINT_VIOLATION:catchable",
                     "CONSTRAINT_VIOLATION:abstain", "BUDGET_BREACH", "WRONG_MERCHANT",
                     "TIMING_MISS", "TOTAL_MISDECLARED"):
            v = metrics["per_subpopulation_recall"][key]
            assert v["rate"] == 1.0, f"{key}: {v}"

    def test_uncatchable_subpopulations_are_never_caught(self, metrics):
        """The instrument check: if these are ever caught, investigate the
        corpus, not the verifier (they are uncatchable BY CONSTRUCTION)."""
        for key in ("QUANTITY_MISMATCH:uncatchable", "CONSTRAINT_VIOLATION:uncatchable"):
            v = metrics["per_subpopulation_recall"][key]
            assert v["rate"] == 0.0, f"{key}: {v} — verifier caught a structurally-uncatchable record"

    def test_no_unexpected_misses(self, metrics):
        assert metrics["misses_summary"]["unexpected"] == 0, metrics["misses"]

    def test_no_mislabelled_catchable_false_records(self, metrics):
        assert metrics["verifier_catchable_audit"]["mislabelled_count"] == 0

    def test_zero_false_block_on_clean(self, metrics):
        """A deterministic, zero-noise verifier over a correctly-labelled CLEAN
        population should never false-block. Nonzero means either the verifier
        or the corpus is wrong — see FAILURES.md's Stage 3 entry."""
        assert metrics["false_block"]["count"] == 0

    def test_zero_over_abstention_on_clean(self, metrics):
        assert metrics["abstain_accuracy"]["over_abstention_on_clean"]["successes"] == 0

    def test_headline_recall_excludes_total_misdeclared_from_denominator_change(self, metrics):
        excl = metrics["headline"]["recall_excl_total_misdeclared"]["n"]
        incl = metrics["headline"]["recall_incl_total_misdeclared"]["n"]
        assert incl > excl   # TOTAL_MISDECLARED records add to the denominator when included

    def test_headline_denominator_includes_every_violation(self, metrics):
        """The correction that matters: structurally-uncatchable violations are
        IN the headline denominator and counted as misses. If this regresses,
        'recall' silently becomes 'recall over what we knew we could catch'."""
        assert metrics["headline"]["recall_incl_total_misdeclared"]["n"] == metrics["violation_count"]

    def test_headline_denominator_is_strictly_larger_than_diagnostic(self, metrics):
        headline_n = metrics["headline"]["recall_excl_total_misdeclared"]["n"]
        diag_n = metrics["secondary_diagnostic_within_expressive_power"][
            "recall_excl_total_misdeclared"]["n"]
        assert headline_n > diag_n, (
            "headline denominator must include the uncatchable records the diagnostic drops"
        )

    def test_headline_recall_is_below_100_percent(self, metrics):
        """With known misses correctly in the denominator, headline recall cannot
        be 100% while structurally-uncatchable records exist in the corpus."""
        assert metrics["headline"]["recall_excl_total_misdeclared"]["rate"] < 1.0

    def test_diagnostic_is_100_percent_within_expressive_power(self, metrics):
        d = metrics["secondary_diagnostic_within_expressive_power"]
        assert d["recall_excl_total_misdeclared"]["rate"] == 1.0

    def test_headline_misses_equal_uncatchable_count(self, metrics):
        """Every headline miss should be an expected (by-design) miss at Stage 3."""
        incl = metrics["headline"]["recall_incl_total_misdeclared"]
        misses = incl["n"] - incl["successes"]
        assert misses == metrics["misses_summary"]["expected"]
        assert metrics["misses_summary"]["unexpected"] == 0

    def test_train_and_holdout_both_reported(self, metrics):
        assert metrics["split"]["train"]["record_count"] > 0
        assert metrics["split"]["holdout"]["record_count"] > 0
        total = metrics["split"]["train"]["record_count"] + metrics["split"]["holdout"]["record_count"]
        assert total == metrics["record_count"]

    def test_not_yet_exercised_metrics_are_listed_not_silently_zero(self, metrics):
        assert len(metrics["not_yet_exercised"]) > 0
        for entry in metrics["not_yet_exercised"]:
            assert isinstance(entry, str) and "Stage" in entry

    def test_recall_ci_present_on_every_subpopulation(self, metrics):
        for key, v in metrics["per_subpopulation_recall"].items():
            assert "ci_low" in v and "ci_high" in v, key
            assert v["ci_low"] <= v["rate"] <= v["ci_high"], key


# ── Baselines ────────────────────────────────────────────────────────────

class TestBaselines:
    def test_block_nothing_is_zero_zero(self):
        b = evaluate_baselines(load_records())
        assert b["block_nothing"]["recall"] == 0.0
        assert b["block_nothing"]["false_block_rate"] == 0.0

    def test_block_everything_is_full_full(self):
        b = evaluate_baselines(load_records())
        assert b["block_everything"]["recall"] == 1.0
        assert b["block_everything"]["false_block_rate"] == 1.0

    def test_counts_match_corpus(self):
        records = load_records()
        b = evaluate_baselines(records)
        assert b["block_nothing"]["n_violations"] + b["block_nothing"]["n_clean"] == len(records)

    def test_sankalp_beats_both_baselines_on_recall(self):
        """SANKALP's excl-TOTAL_MISDECLARED recall must clearly beat block-nothing (0%)
        — and match block-everything (100%) on the catchable subset, which is the
        strongest possible deterministic result. Per PRE_REGISTERED.md: any result
        that doesn't clearly beat both is not a result."""
        metrics = run_harness()
        recall = metrics["headline"]["recall_excl_total_misdeclared"]["rate"]
        assert recall > 0.0   # beats block-nothing
        false_block = metrics["headline"]["false_block_rate_proxy"]["rate"]
        assert false_block < 1.0   # beats block-everything's 100% false-block cost

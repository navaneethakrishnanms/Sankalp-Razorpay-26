"""
Tests for scripts/check_results_no_drift.py.

This script exists because the naive `git diff --exit-code eval/results/`
CI step failed on every single run: stage3_results.json embeds a wall-clock
latency measurement that is never bit-identical between two runs on
different hardware. The first version of this script's latency-strip only
popped the key at the top level and missed that it's actually nested under
"constraint_and_receipt_verifiers" — these tests pin the correct (recursive)
behaviour down so that regression can't silently reappear.
"""

from __future__ import annotations

import json

from scripts.check_results_no_drift import _deep_strip_latency, _strip_latency_md


class TestDeepStripLatency:
    def test_removes_a_top_level_latency_key(self):
        assert _deep_strip_latency({"latency_seconds": {"p50": 1}, "other": 2}) == {"other": 2}

    def test_removes_a_nested_latency_key(self):
        payload = {"constraint_and_receipt_verifiers": {"latency_seconds": {"p50": 1}, "recall": 0.9}}
        assert _deep_strip_latency(payload) == {"constraint_and_receipt_verifiers": {"recall": 0.9}}

    def test_removes_latency_keys_inside_lists(self):
        payload = {"misses": [{"latency_seconds": 0.1, "order_id": "a"}]}
        assert _deep_strip_latency(payload) == {"misses": [{"order_id": "a"}]}

    def test_leaves_everything_else_untouched(self):
        payload = {"a": 1, "b": [1, 2, {"c": 3}], "d": {"e": "f"}}
        assert _deep_strip_latency(payload) == payload

    def test_two_payloads_differing_only_in_latency_compare_equal_after_strip(self):
        a = json.loads(json.dumps({
            "constraint_and_receipt_verifiers": {"latency_seconds": {"p50": 5.3e-05}, "recall": 0.84},
        }))
        b = json.loads(json.dumps({
            "constraint_and_receipt_verifiers": {"latency_seconds": {"p50": 6.8e-05}, "recall": 0.84},
        }))
        assert _deep_strip_latency(a) == _deep_strip_latency(b)

    def test_two_payloads_differing_in_a_real_field_stay_different(self):
        a = {"constraint_and_receipt_verifiers": {"latency_seconds": {"p50": 1}, "recall": 0.84}}
        b = {"constraint_and_receipt_verifiers": {"latency_seconds": {"p50": 1}, "recall": 0.79}}
        assert _deep_strip_latency(a) != _deep_strip_latency(b)


class TestStripLatencyMd:
    def test_normalises_p50_and_p95_lines(self):
        text = "## Latency\n\n- p50: 0.053 ms\n- p95: 0.128 ms\n"
        assert "0.053" not in _strip_latency_md(text)
        assert "0.128" not in _strip_latency_md(text)

    def test_two_reports_differing_only_in_latency_compare_equal_after_strip(self):
        a = "- p50: 0.053 ms\n- p95: 0.128 ms\n"
        b = "- p50: 0.057 ms\n- p95: 0.122 ms\n"
        assert _strip_latency_md(a) == _strip_latency_md(b)

    def test_a_real_content_change_still_differs(self):
        a = "Recall: 84.0%\n- p50: 0.053 ms\n- p95: 0.128 ms\n"
        b = "Recall: 79.0%\n- p50: 0.053 ms\n- p95: 0.128 ms\n"
        assert _strip_latency_md(a) != _strip_latency_md(b)

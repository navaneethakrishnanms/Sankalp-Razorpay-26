"""
The two baselines every SANKALP result must clearly beat, at the fixed 2%
operating point, per eval/PRE_REGISTERED.md. Published from Stage 3 (needs
only a verdict, not settlement).

  block-nothing:     0% recall, 0% false-block.
  block-everything: 100% recall, 100% false-block.

Deliberately trivial — a baseline that requires the field registry or the
verifiers to compute would defeat the point of a baseline.
"""

from __future__ import annotations

from typing import Any


def evaluate_baselines(records: list[dict[str, Any]]) -> dict[str, Any]:
    violation_count = sum(1 for r in records if r["labels"]["violation_class"] != "CLEAN")
    clean_count = sum(1 for r in records if r["labels"]["violation_class"] == "CLEAN")

    return {
        "block_nothing": {
            "recall": 0.0, "false_block_rate": 0.0,
            "n_violations": violation_count, "n_clean": clean_count,
        },
        "block_everything": {
            "recall": 1.0, "false_block_rate": 1.0,
            "n_violations": violation_count, "n_clean": clean_count,
        },
    }

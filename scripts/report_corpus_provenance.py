"""
Which seeds were edited in each corpus regeneration, and on which side of the split.

This exists because "the corpus was regenerated" is not a disclosure — the
question that matters is whether HOLDOUT content was touched. A holdout
corrected after inspection is weaker evidence than a holdout never touched, and
a reader is entitled to know which one they are looking at.

The edit lists below are a historical record. Append a new entry per
regeneration; never revise an existing one.

    python scripts/report_corpus_provenance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eval.generator import build_corpus, build_split, DEFAULT_GLOBAL_SEED   # noqa: E402

# ── Historical record of corpus regenerations ─────────────────────────────

REGENERATIONS: list[dict] = [
    {
        "id": 1,
        "stage": "2.5 (Part A)",
        "trigger": "Scale-up to the agreed floors: >=40 seeds, >=600 records, "
                    ">=25 per subpopulation, seed-level stratified split.",
        "found_by": "Planned rework, not a defect.",
        "seeds_edited": [],   # wholesale regeneration; no targeted seed edits
        "note": "Corpus rebuilt from 10 seeds to 45. Not a defect fix.",
    },
    {
        "id": 2,
        "stage": "3",
        "trigger": "Six CLEAN records violated a distinct_item_count criterion: "
                    "_clean_extra_uncovered_item added a distinct item to seeds whose "
                    "criteria counted distinct items.",
        "found_by": "The Stage 3 constraint verifier — the component under test. The "
                     "false-block proxy returned 0.63% where a deterministic verifier "
                     "over a correctly-labelled CLEAN set must return exactly 0%.",
        "seeds_edited": ["S02-biryani-office-lunch", "S18-southindian-rich"],
        "note": "Fixed structurally: mutate_clean_dispatch now validates every candidate "
                 "CLEAN cart with the real evaluate_constraint_checks, not a hand-picked "
                 "subset. No individual record was edited.",
    },
    {
        "id": 3,
        "stage": "4",
        "trigger": "14 seeds carried a budget_ceiling or delivery_window that contradicted "
                    "their own instruction text — introduced during the Stage 2.5 "
                    "budget-conflict fix, which changed field values without updating the "
                    "instructions.",
        "found_by": "The Stage 4 obligation compiler — the component under test. It read "
                     "the instructions correctly and was scored WRONG for it.",
        "seeds_edited": [
            "S02-biryani-office-lunch", "S04-biryani-not-oily-hinglish",
            "S05-biryani-big-cart", "S08-biryani-chicken65-hinglish",
            "S16-southindian-no-chicken", "S18-southindian-rich",
            "S23-punjabi-scope-budget", "S28-punjabi-no-colour-ambiguous",
            "S30-freshmart-no-eggs", "S33-freshmart-big-cart",
            "S38-dailybasket-no-mutton", "S39-dailybasket-no-eggs-hinglish",
            "S43-dailybasket-apples-no-mutton", "S45-dailybasket-eggs-no-mutton",
        ],
        "note": "Fixed structurally: _assert_obligation_fields_traceable now extends the "
                 "traceability guard to budget_ceiling and delivery_window. Instruction "
                 "TEXT was corrected, not the field values — the values encode mutation "
                 "constraints that took a boundary-trial pass to establish.",
    },
]


def main() -> int:
    records = build_corpus(DEFAULT_GLOBAL_SEED)
    split = build_split(records, DEFAULT_GLOBAL_SEED)

    seed_side: dict[str, str] = {}
    for record in records:
        seed_side[record["generation"]["seed_id"]] = split[record["order_id"]]

    total_seeds = len(seed_side)
    n_train = sum(1 for s in seed_side.values() if s == "train")
    n_holdout = total_seeds - n_train

    print(f"Corpus: {len(records)} records from {total_seeds} seeds "
          f"({n_train} train / {n_holdout} holdout)\n")

    for entry in REGENERATIONS:
        edited = entry["seeds_edited"]
        print(f"── Regeneration {entry['id']} — Stage {entry['stage']} " + "─" * 30)
        print(f"   Trigger:  {entry['trigger']}")
        print(f"   Found by: {entry['found_by']}")

        if not edited:
            print("   Seeds edited: (wholesale rebuild — no targeted seed edits)\n")
            continue

        train_hit = sorted(s for s in edited if seed_side.get(s) == "train")
        holdout_hit = sorted(s for s in edited if seed_side.get(s) == "holdout")
        unknown = sorted(s for s in edited if s not in seed_side)

        print(f"   Seeds edited: {len(edited)} of {total_seeds}")
        print(f"     train:   {len(train_hit)}/{n_train}  {train_hit}")
        print(f"     HOLDOUT: {len(holdout_hit)}/{n_holdout}  {holdout_hit}")
        if unknown:
            print(f"     UNKNOWN SEED IDS (stale record): {unknown}")
        if holdout_hit:
            print("     ^^ HOLDOUT CONTENT WAS TOUCHED — this must be stated in the README. "
                   "A holdout corrected after inspection is weaker evidence than one never "
                   "touched.")
        print(f"   Note: {entry['note']}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

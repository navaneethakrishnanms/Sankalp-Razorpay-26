# SANKALP — Stage 5 Results (semantic verifier + floor enforcement, live)

Scope: train split only — holdout sealed until Stage 8. Subset: 42 deceptive-self-report, 16 uncatchable-semantic, 60 CLEAN-with-semantic-criteria.

## Headline — the measured value of the architecture

"Caught" = the order was NOT wrongly cleared (action != EXECUTE); ABORT, CLARIFY, and HOLD (no admissible basis) all count. Two populations are reported separately because conflating them hid the real result (see FAILURES.md): population A always has a deterministic `stated` FAIL, which this aggregator makes absolute regardless of floor — so its gap is correctly ~0%, that IS the source-enforcement rule working, not the floor doing nothing. Population B has NO deterministic verifier at all — the self-report is the only evidence — and is where floor enforcement's actual value is measured.

### Population B — semantic-only, the true fooled-judge test (THE headline)

THE headline. No deterministic verifier exists for these — the self-report is the sole evidence. This is where the architecture's measured value actually lives.

- Catch rate WITH floor enforcement: **100.0% [80.6%, 100.0%] (n=16)**
- Catch rate WITHOUT floor (counterfactual): 100.0% [80.6%, 100.0%] (n=16)
- **Architecture value gap: +0.0%**

### Population A — deterministic backup present (expected ~0% gap — the enforcement rule, not an inert floor)

A `stated` FAIL always exists here and is absolute either way — expect ~0% gap; that is the enforcement rule, not an inert floor.

- Catch rate WITH floor: 100.0% [87.1%, 100.0%] (n=26)
- Catch rate WITHOUT floor: 100.0% [87.1%, 100.0%] (n=26)

### All deceptive records combined (for reference only — do not headline this)

- Catch rate WITH floor: 100.0% [91.6%, 100.0%] (n=42)
- Catch rate WITHOUT floor: 100.0% [91.6%, 100.0%] (n=42)

## Semantic verifier accuracy (26-record uncatchable-semantic subpopulation)

Recall on the 26-record subpopulation deterministic verification provably cannot reach.

- catch rate: 0.0% [0.0%, 19.4%] (n=16)
- abstention rate: 100.0% [80.6%, 100.0%] (n=16)

## Aggregate recall projection vs Stage 3 baseline (84.0%)

Stage 3's deterministic verdicts are kept as-is for every record NOT in the live-run uncatchable-semantic subset; only that subset's verdicts are replaced with the live semantic verifier's actual output. This is the subset-honest recall projection, not a full 414-record LLM re-run.

- recall excl. TOTAL_MISDECLARED: 84.0% [79.6%, 87.5%] (n=324)
- recall incl. TOTAL_MISDECLARED: 87.4% [83.9%, 90.3%] (n=414)
- uncatchable-semantic run in this subset: 16 / 26

## False-block from semantic verification

Semantic verification can introduce false FAILs where deterministic verification introduced none (Stage 3 false-block was exactly 0%). A recall gain paid for in false blocks is not a gain.

- false-block rate: 0.0% [0.0%, 6.0%] (n=60)
- abstention rate on CLEAN: 100.0% [94.0%, 100.0%] (n=60)

## CLARIFY

- rate: 100.0% [91.6%, 100.0%] (n=42)

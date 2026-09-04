# SANKALP — Stage 3 Results (deterministic verifiers, zero LLM calls)

Corpus: 1359 records (945 CLEAN, 414 violations, 69.5% base rate).

## Headline — recall over ALL violations

Recall over ALL violations — structurally-uncatchable records are IN the denominator and counted as misses. Single achieved deterministic operating point; see eval/harness.py module docstring for why the 1/2/5% false-block sweep is not yet meaningful.

- **Recall (excl. TOTAL_MISDECLARED): 84.0% [79.6%, 87.5%] (n=324)** ← headline
- Recall (incl. TOTAL_MISDECLARED): 87.4% [83.9%, 90.3%] (n=414)
- False-block proxy: 0.0% [0.0%, 0.4%] (n=945)

## Secondary diagnostic — within deterministic expressive power

Recall over violations within deterministic expressive power — denominator EXCLUDES the structurally-uncatchable records. This is a diagnostic answering 'is the deterministic layer correct within its reach?', NOT a headline: its denominator is chosen by the component being measured. Always read it next to the headline figure above.

- Recall (excl. TOTAL_MISDECLARED): 100.0% [98.6%, 100.0%] (n=272)
- Recall (incl. TOTAL_MISDECLARED): 100.0% [99.0%, 100.0%] (n=362)

## Baselines

- block-nothing: 0.0% recall, 0.0% false-block
- block-everything: 100.0% recall, 100.0% false-block
- SANKALP constraint+receipt: 84.0% recall, 0.0% false-block

## Per-subpopulation recall

| Subpopulation | Recall | 95% CI | n |
|---|---|---|---|
| BUDGET_BREACH | 100.0% | [93.1%, 100.0%] | 52 |
| CONSTRAINT_VIOLATION:abstain | 100.0% | [87.1%, 100.0%] | 26 |
| CONSTRAINT_VIOLATION:catchable | 100.0% | [90.4%, 100.0%] | 36 |
| CONSTRAINT_VIOLATION:uncatchable | 0.0% | [0.0%, 12.9%] | 26 |
| QUANTITY_MISMATCH:catchable | 100.0% | [92.3%, 100.0%] | 46 |
| QUANTITY_MISMATCH:uncatchable | 0.0% | [0.0%, 12.9%] | 26 |
| TIMING_MISS | 100.0% | [92.9%, 100.0%] | 50 |
| TOTAL_MISDECLARED | 100.0% | [95.9%, 100.0%] | 90 |
| WRONG_MERCHANT | 100.0% | [94.2%, 100.0%] | 62 |

## ABSTAIN accuracy

- Correct on abstain_expected records: 100.0% [87.1%, 100.0%] (n=26)
- Over-abstention on CLEAN records: 0.0% [0.0%, 0.4%] (n=945)

## verifier_catchable audit

- catchable=False records checked: 52
- mislabelled (actually caught): 0

## Misses

- expected (uncatchable by design): 52
- unexpected (catchable but missed): 0

## Split (all denominators are all-violations, per the headline convention)

- train: 943 records, recall (excl. TOTAL_MISDECLARED) 86.1% [81.0%, 90.0%] (n=230)
- holdout: 416 records, recall (excl. TOTAL_MISDECLARED) 78.7% [69.4%, 85.8%] (n=94) — low resolution, aggregate only, draw no per-class conclusions here

A train/holdout gap here is NOT overfitting: at Stage 3 nothing fits — the verifiers are deterministic code written before the split existed and they never see a label. Because catchable-only recall is exactly 100%, headline recall reduces to the ratio of catchable to uncatchable violations in each split, so any gap is corpus composition. It becomes a meaningful signal at Stage 4, when the compiler prompt is something that can overfit.

## Language

- en: recall (excl. TOTAL_MISDECLARED) 82.9% [77.6%, 87.2%] (n=234)
- hinglish: recall (excl. TOTAL_MISDECLARED) 86.7% [78.1%, 92.2%] (n=90)

## Latency

- p50: 0.053 ms
- p95: 0.128 ms

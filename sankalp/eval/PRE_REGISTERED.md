# SANKALP — Pre-Registered Evaluation Metrics

**Written once, at Stage 2. Do not edit after first commit — CI checks this
file's hash.** If a metric definition turns out to be wrong, that is a
finding for `FAILURES.md`, not a reason to edit this file: add a dated
addendum section at the bottom instead.

Corpus: `eval/corpus/records.jsonl`, hash-locked by `eval/corpus/CORPUS_LOCK.json`
(see `eval/generator.py`, `tests/integration/test_corpus_lock.py`).
Split: `eval/corpus/split.json`, ~70% train / ~30% holdout, assigned at the
**seed** level (not the record level) and stratified so every violation
subpopulation has at least one contributing seed on each side — see
`eval/generator.build_split`. Splitting at the record level would put
near-duplicate records from the same seed on both sides, which is leakage,
not generalisation (Stage 2.5, Part A3). The record-level ratio does not
land exactly on 70/30 once stratification moves seeds around; that is
expected, not a bug.

## Statistical reporting

**Every recall, precision, and rate figure in every table below is reported
with a Wilson score confidence interval, not a bare point estimate.**
Per-subpopulation counts in this corpus run from ~25 (the enforced floor —
see `MIN_PER_SUBPOPULATION` in `eval/generator.py`) to several hundred; a
recall figure on 25-30 records has a wide interval, and reporting the point
estimate alone is how a measurement gets mistaken for a claim. Use the
Wilson interval (not the normal approximation) because it stays sane near
0% and 100%, where several of this corpus's expected results sit
(uncatchable-by-construction subpopulations are expected near 0%; the
block-everything baseline is 100% by definition).

## Stage gating

Every metric below is tagged with the stage at which it first becomes
computable (see the Stage order, README §7). A metric is either a real
number or `NOT_YET_EXERCISED` — **never a silent 0 or null that could be
misread as a measured zero.** The harness must render the two states
differently.

| Stage | Unlocks |
|---|---|
| 3 | Constraint + receipt verifiers (deterministic). Per-criterion verdicts exist. |
| 4 | Obligation compiler (LLM). Compiler metrics become measurable. |
| 5 | Semantic verifier + aggregator + floor enforcement, wired end to end. |
| 6 | Exposure banding + settlement instruction. Settlement-level metrics (false-block, clarify) become measurable. |

## Headline metric

**Violation catch rate at a fixed 2% false-block rate.**

Operating point fixed now, in writing, before any results exist — chosen
before the numbers so it can't be picked to flatter them. 2%, not 5%: at
5%, one in twenty food orders would be interrupted, which is not a
shippable product regardless of what the recall number says.

Reported alongside, for the curve, not as alternate headlines: recall at
1%, 2%, and 5% false-block, each with its Wilson confidence interval.

*Stage: 6* (false-block rate is a settlement-level concept — requires the
aggregator and settlement instruction to exist. Stage 3 may report a
verifier-level proxy — FAIL-rate on CLEAN records — explicitly labelled as
a proxy, not the headline.)

## Per-class recall

Recall computed separately per `violation_class`, and *within*
`QUANTITY_MISMATCH` and `CONSTRAINT_VIOLATION*, split by the
`verifier_catchable` / `abstain_expected` subpopulation tags the generator
assigns at construction time (never inferred after the fact — see
`eval/generator.py` module docstring, rule 3):

- `QUANTITY_MISMATCH` — catchable / uncatchable-by-construction
- `CONSTRAINT_VIOLATION` — catchable / abstain-expected / uncatchable-by-construction (semantic-only)
- `BUDGET_BREACH`, `WRONG_MERCHANT`, `TIMING_MISS` — single population
- `TOTAL_MISDECLARED` — single population. Reported separately from the
  other five with a note: **this is agent misreporting (`Cart.validate_total()`
  failure), not an intent violation — a secondary catch, not the thesis.**
  It must never be averaged into the primary per-class recall table.

Recall on the `uncatchable-by-construction` subpopulations is expected to
be ~0% by design — this is not a verifier failure, it is the corpus doing
its job (§6.1's tautology guard). Reporting it un-annotated next to the
catchable recall numbers would misread as a regression; it must be
presented as its own labelled row.

*Stage: 3* for `QUANTITY_MISMATCH` / `CONSTRAINT_VIOLATION` (constraint
verifier only). *Stage: 6* for `BUDGET_BREACH` / `WRONG_MERCHANT` /
`TIMING_MISS` / `TOTAL_MISDECLARED` (these are Obligation/Cart-level
checks, not AcceptanceCriterion checks — see `eval/generator.py`'s
`violating_obligation_fields` vs `violating_criterion_ids` distinction).

### verifier_catchable mislabel check

`verifier_catchable=False` is a hand-labelled belief about the constraint
verifier's expressive power at the time the corpus was authored, and
beliefs are sometimes wrong. At Stage 3, the harness must assert that every
record tagged `verifier_catchable=False` was, in fact, missed by the
constraint verifier. If one is caught, report it under a distinct counter
— **mislabelled-record count** — and do not silently relabel it to
`verifier_catchable=True`; a mislabel is itself a corpus-quality finding
worth surfacing, not a metric to quietly fix.

## False-block count and rupee cost

Count and ₹ total of CLEAN records the pipeline blocks (FAIL/HOLD/ABORT on
a record with `violation_class == CLEAN`).

*Stage: 6* (requires settlement action, not just verifier verdict).

## Clarification precision

Of records routed to CLARIFY, the fraction that were genuinely ambiguous
(`instruction_ambiguous == true`) or had a real, non-`stated` violation
that should not hard-block. Precision, not recall — a CLARIFY on a
perfectly CLEAN, unambiguous record counts against this number.

*Stage: 6* (CLARIFY is a `SettlementAction`, produced by the aggregator +
exposure band, per the `CriterionSource` enforcement table in
`core/models/enums.py`).

## Deceptive self-report catch rate

Recall, restricted to records with `self_report_deceptive == true` (always
a subset of violation records — the generator never tags a CLEAN record
deceptive, since there is nothing to lie about). This is the §5.2
"fooled-judge" scenario at corpus scale: a `SELF`-class claim of compliance
sitting next to a real, `REC`-class violation. Ground truth for this
metric is set at corpus construction, per rule 1 in `eval/generator.py` —
never derived by running the semantic verifier over the record.

*Stage: 5* (requires the semantic verifier to read `self_report` and the
floor/aggregator to structurally exclude it — this is the exact mechanism
proven at the unit level in `tests/unit/test_admissibility.py::TestFloorEnforcementFooledJudge`).
Before Stage 5, report `NOT_YET_EXERCISED`, not 0% — a 0% would read as
"the system is currently failing this," which is false; the mechanism this
metric measures does not exist yet.

## Latency p50 / p95, by exposure band

*Stage: 6* (exposure bands — LOW / MODERATE / ELEVATED — don't exist
before the exposure scorer is built).

## Cost per clearing (₹)

LLM API cost, amortised per cleared order. Zero by construction for every
record routed through deterministic verifiers only (LOW band).

*Stage: 4* for compiler-only cost (`POST /api/obligations/compile` cost in
isolation). *Stage: 6* for full end-to-end cost per clearing.

## Band distribution

Fraction of corpus records assigned to each of LOW / MODERATE / ELEVATED.

*Stage: 6.*

## Compiler metrics

All *Stage: 4.* Computed by running the compiler on `instruction_text` and
comparing its output to the corpus's hand-authored `obligation.acceptance_criteria`
— the ground truth `eval/generator.py` produces, never criteria derived
from the compiler itself (rule 1).

- **Criterion extraction accuracy**: captured / invented / missed, against
  the ground-truth criteria list per record.
- **Source-labelling accuracy**: `stated`-labelled-as-`inferred` and
  `inferred`-labelled-as-`stated` rates. The first is the dangerous
  direction — a compiler that mislabels a hard `stated` constraint as
  advisory `inferred` can inflate the headline catch rate while making the
  underlying violation unenforceable (see `core/models/enums.py`,
  `CriterionSource` docstring). Must be reported even when the headline
  number looks clean; a clean headline with a rising
  stated-labelled-as-inferred rate is the compiler gaming the metric, not
  a genuine improvement.
- **Unresolvable-path rate**: fraction of compiler-emitted `field` paths
  not present in `core/models/fields.py`'s registry. Per the registry's
  own doc comment, an unresolvable path is a hard bind failure, never a
  silent ABSTAIN — this metric tracks how often the compiler forces that
  failure.
- **Instruction-traceability check**: for the corpus's own ground truth
  (not the compiler's output) — every `stated` criterion's `phrases` must
  be literal substrings of its record's `instruction_text`. Enforced at
  corpus-generation time (`eval/generator._assert_stated_criteria_traceable`),
  re-asserted here so a hand-edit to `records.jsonl` that breaks
  traceability is caught by the eval run too, not only by the generator.

## Base rate

~70% CLEAN by construction (`eval/generator.py`'s `TestFloors`/`TestBaseRate`
suites assert the achieved figure never drifts far from this). Reported
alongside every results table so a reader can sanity-check any recall
number against it. The corpus floors that make this rate resolvable at the
2% headline operating point (≥600 records, ≥300 CLEAN, ≥45 seeds, ≥25 per
violation subpopulation) are enforced by `tests/unit/test_generator.py::TestFloors`
— see `eval/generator.py`'s `MIN_*` constants. A corpus that shrinks back
below those floors makes the 2% point unresolvable again (§A1 of the
Stage 2.5 corrections): with N clean records, the finest achievable
false-block granularity is `1/N`, so N must be large enough that 2% falls
between two achievable points, not on top of a rounding artifact.

## Baselines

Published from day one, *Stage: 3* (need only a verdict, not settlement):

- **block-nothing**: 0% recall, 0% false-block.
- **block-everything**: 100% recall, 100% false-block.

Any SANKALP result that does not clearly beat both, at the fixed 2%
operating point, is not a result.

## Language split

Every metric above additionally reported split by `language`
(`en` / `hinglish`), so a language-specific blind spot surfaces in the
results table rather than at demo time. Hinglish is capped at generation
time (see `TestBaseRate::test_hinglish_capped_and_present`, ≤35% of the
corpus; achieved figure in `CORPUS_LOCK.json`).

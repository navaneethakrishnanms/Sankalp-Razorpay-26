# SANKALP

**Intent-Fulfilment Clearing for Agentic UPI Commerce.**

When an AI agent buys something on your behalf, two different questions have to
be answered before money moves:

1. *Was the agent allowed to act, and is the record authentic?*
2. *Did the agent actually do what you asked?*

The emerging protocol layer answers the first question well. SANKALP is an
implementation of the second.

---

## The gap this addresses

> **Verification note (read before publishing or presenting this section).**
> The AP2 description below is written from a secondary summary and has **not
> been verified against Google's primary sources**. Before this README is
> shown to anyone external, confirm at source: the protocol's launch date and
> partner count, that the three mandate types are named Intent / Cart /
> Payment, and that mandates are issued as W3C Verifiable Credentials. The
> *architectural* claim SANKALP rests on — that AP2 establishes authorization
> and authenticity but leaves fulfilment adjudication out of scope — should be
> confirmed against the specification's own scope statement rather than
> inferred from a summary. If any specific detail turns out to be wrong,
> correct it here; the positioning does not depend on the details, only on the
> scope boundary.

**Google's Agent Payments Protocol (AP2)** establishes a cryptographic chain of
*Mandates* — signed, tamper-evident credentials recording what the user
authorised (Intent), what the agent assembled (Cart), and what is being charged
(Payment). The result is a non-repudiable audit trail: you can prove the agent
was permitted to act, and that the record has not been altered.

**RAILS** (arXiv 2606.08790) names the same boundary from the research side:
authorization establishes *permitted agency*, not *fulfilled obligation*.

Here is the gap both leave open. An agent can produce a perfectly valid
Intent → Cart → Payment mandate chain for an order that gets the quantity
wrong. Every signature verifies. The audit trail is complete and admissible.
The user is still out the money, and the dispute still has to be adjudicated by
someone.

SANKALP is that adjudication, mechanised. It consumes authority evidence of the
shape AP2 produces and adds the determination those protocols explicitly leave
to an adjudicator: **did the delivered cart satisfy the obligation the user
actually expressed?**

We claim the implementation, not the diagnosis. The gap is named by others; what
is here is a working, measured system that closes it.

---

## The core mechanism: an admissibility lattice

The question every LLM-verification system eventually faces is *"what stops your
model from hallucinating a PASS?"* Most answers are some combination of better
prompts, higher thresholds, and more judges voting. All of those are statistical
and all of them degrade under adversarial pressure.

SANKALP's answer is structural. Evidence is typed by how trustworthy its source
is, in a total order:

```
SELF (0)  <  SIGN (1)  <  WIT (2)  <  REC (3)  <  ATT (4)
```

Each obligation declares an **admissibility floor**. Every verifier must declare
the evidence it consulted. A verifier whose evidence falls below the floor is
assigned weight zero and **excluded before aggregation runs** — not outvoted,
not down-weighted. Removed from the set.

The consequence, proven as a unit test
(`tests/unit/test_admissibility.py::TestFloorEnforcementFooledJudge`):

> A semantic verifier returning **PASS at 0.99 confidence**, on a `SELF`-class
> basis, against a `REC` floor, is **structurally absent from the survivor set,
> not outvoted.** The surviving constraint verifier's `REC`-based FAIL carries
> the decision.

Confidence is irrelevant to exclusion. A more persuasive hallucination is not a
more dangerous one, because persuasiveness was never the thing being checked.

The non-promotion invariant makes this hold under composition: `join([SELF,
SIGN]) = SIGN`, which still does not clear a `REC` floor. Two fooled LLM
verifiers agreeing with each other contribute exactly zero.

---

## Results

Stage 3 (deterministic verifiers, zero LLM calls) over a 1,359-record corpus,
69.5% CLEAN base rate. Full output: [`eval/results/`](eval/results/).

Regenerate with `make eval` (see [Running it](#running-it)).

**Headline — recall over ALL violations,** including the ones this layer
provably cannot catch:

| Metric | Value |
|---|---|
| Recall, excl. `TOTAL_MISDECLARED` | **272/324 = 84.0%** |
| Recall, incl. `TOTAL_MISDECLARED` | 362/414 = 87.4% |
| False-block rate | 0.0% |

**Secondary diagnostic — within deterministic expressive power:** 100.0%
(272/272). This says the deterministic layer is perfect *within its reach*, and
the headline says exactly how far that reach goes. The 52-record gap is the
semantic verifier's job at Stage 5; measuring from 84% leaves somewhere to go,
measuring from 100% would not.

**Baselines** (both published from day one, per pre-registration):

| System | Recall | False-block |
|---|---|---|
| block-nothing | 0% | 0% |
| block-everything | 100% | 100% |
| **SANKALP (Stage 3)** | **84.0%** | **0.0%** |

### Two reporting decisions worth stating explicitly

**We do not report the requested 1% / 2% / 5% false-block sweep.** The
pre-registration asked for recall at three false-block operating points. A
deterministic predicate evaluator has no confidence knob to slide: on a
correctly-labelled CLEAN record it is either right or wrong, and no threshold
changes that. Sweeping a fake threshold over a binary signal would produce three
identical numbers plotted as a curve — theatre, not measurement. We report the
single achieved operating point with its Wilson interval, and the sweep becomes
meaningful at Stage 4/5, where the compiler and semantic verifier introduce
genuine continuous-confidence error. The reasoning is in
[`eval/harness.py`](eval/harness.py)'s module docstring.

**The 0.0% false-block rate is expected, not impressive.** Over a
correctly-labelled CLEAN population, a deterministic verifier *should* score
exactly zero. It is reported as a regression check — a nonzero value means
either the verifier or the corpus is wrong — rather than as an achievement. It
earned its keep in exactly that way once (below).

### Every number carries a Wilson interval

Per-subpopulation counts run from 26 to 945. A recall figure on 26 records has a
wide interval, and a point estimate alone is a claim rather than a measurement.
Wilson rather than the normal approximation, because several expected results
sit at exactly 0% or 100% where the normal approximation misbehaves.

---

## Disclosure: a corpus label bug found by the component under test

This project's corpus rule is that **labels never derive from the thing being
measured**. The following bends that rule, so it is disclosed rather than
buried.

**What it was.** Six CLEAN records were mislabelled. A distractor mutation added
to diversify the clean population, `_clean_extra_uncovered_item`, adds an item
to the cart "that no criterion covers." Two seeds (`S02`, `S18`) declare a
`distinct_item_count == N` criterion — and adding a new distinct item is
precisely what that criterion detects. Those six carts genuinely failed a
`stated` criterion while being labelled CLEAN.

**How it was found.** By running the Stage 3 constraint verifier — the component
under test — over the corpus. The false-block proxy came back at 0.63% when a
deterministic verifier over a correctly-labelled CLEAN set should return exactly
0%. That gap was the signal. It was invisible for all of Stage 2.5, because
nothing before Stage 3 evaluated criteria against carts.

**Why this corrects a label error rather than tunes toward a number.** The six
carts violated a criterion the user's own instruction stated. They were
mislabelled the moment they were generated, independently of who noticed or
when. The verifier did not decide what the correct label was — the seed's
hand-authored criteria did, and those were written before any verifier existed.
What the verifier supplied was *detection*, not *ground truth*.

**Why the fix is structural, not six hand-patched records.** Two changes:
`_clean_extra_uncovered_item` now declines to fire on any seed carrying a
`distinct_item_count` or `item_count` criterion; and — the durable half —
`mutate_clean_dispatch` now validates every candidate CLEAN cart by running the
real `evaluate_constraint_checks`, instead of the hand-picked subset of checks
it used before. No individual record was edited. The corpus was regenerated from
the fixed generator and re-locked.

**The residual risk, stated plainly.** The generator's self-check now imports
the verifier, so a bug *inside* `evaluate_constraint_checks` would produce a
corpus consistent with that bug. That is a real coupling. It is bounded by the
fact that CLEAN labels still originate from hand-authored criteria written
before the verifier existed — the verifier can only detect a contradiction
between cart and criteria, never author either. Full write-up in
[`FAILURES.md`](FAILURES.md).

---

## Architecture

```
POST /api/clear
   │
   ├─ 1. Obligation compiler   (LLM)  NL instruction → AcceptanceCriterion[]
   ├─ 2. Binder                       freeze + hash; unresolvable path = hard fail
   ├─ 3. Evidence envelope            assemble items, assign classes via provenance meet
   ├─ 4. Exposure scorer       (det)  → band LOW | MODERATE | ELEVATED
   ├─ 5. Verifier mesh                band selects which verifiers run
   │      ├─ constraint  (det)  predicate evaluation over the closed registry → REC
   │      ├─ receipt     (det)  merchant catalogue cross-check                → REC
   │      └─ semantic    (LLM)  declares its own basis
   ├─ 6. Floor enforcement     (det)  sub-floor verifiers → weight 0, excluded pre-join
   ├─ 7. Aggregator            (det)  weighted verdict, join over survivors only
   ├─ 8. Clearing decision            performance + policy verdict, fault, confidence
   └─ 9. Settlement instruction       EXECUTE | HOLD | CLARIFY | ABORT
```

Only steps 1 and 5-semantic involve a model. Everything else is deterministic
Python. Deliberate deviations and design decisions are recorded in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

### The LLM never authors a value

The obligation compiler does not emit numbers. It emits **spans** — verbatim
substrings of the user's instruction — and deterministic Python parses values
out of them:

```
instruction : "Order dinner for 4 people ... under ₹1500"
model emits : value_span="4 people",  budget_ceiling_span="under ₹1500"
code parses : 4,                      Decimal("1500")
```

A span that does not appear literally in the instruction is rejected
([`core/guards/output_validator.py`](core/guards/output_validator.py)), so a
hallucinated "under ₹2000" cannot survive: it is not in the user's text. This is
strictly stronger than rejecting digits, because it also catches authored values
containing no digits — an invented merchant, a prohibited ingredient the user
never mentioned.

### Reproducibility without an API key

Determinism is **not** `temperature=0` — sampling parameters were removed on
current Claude models and a request carrying them is rejected. Instead, every
LLM response is cached on disk, keyed by a hash over
(provider, model, system, prompt, max_tokens, effort, prompt_version), and
`eval/llm_cache/` is **committed**. Once recorded, every later run — CI, a
reviewer's laptop, the demo machine — reproduces the exact bytes the metrics
were computed from. `CacheOnlyProvider` makes that enforceable: it raises on a
cache miss rather than silently generating fresh output.

---

## Evaluation design

- **Corpus built forward from the world.** Every record starts from a
  hand-authored seed (a real instruction, hand-derived criteria, a compliant
  cart); violations are produced by mutating the **cart**. No label is ever
  derived by running the compiler or a verifier over generated data.
- **Instruction/criteria traceability is mechanically enforced.** Every `stated`
  criterion declares phrases that must appear literally in the instruction, or
  generation fails. A criterion not recoverable from the user's words is not
  `stated`.
- **Some violations are uncatchable on purpose.** 26 quantity-swap records
  (aggregate-preserving) and 26 semantic-only records are invisible to
  deterministic verification by construction. They are the tautology guard: if
  the deterministic layer ever catches one, the corpus is wrong, not the
  verifier. Asserted every run; 0 mislabels to date.
- **Held-out split is seed-level and stratified.** Records from one seed share a
  merchant, vocabulary and criteria shape — splitting at the record level would
  put near-duplicates on both sides, which is leakage. 45 seeds, 31 train / 14
  holdout, every violation subpopulation present on both sides.
- **Pre-registered before any verifier existed.**
  [`eval/PRE_REGISTERED.md`](eval/PRE_REGISTERED.md) is hash-locked in CI.
- **Metrics that don't exist yet say so.** Anything gated behind a later stage
  reports `NOT_YET_EXERCISED`, never `0%` — a zero would read as a measured
  failure of a mechanism that has not been built.

---

## Running it

```bash
pip install -e ".[dev]"
pytest -q                       # full suite
```

Regenerate the corpus (only needed after changing the generator):

```bash
python -c "from pathlib import Path; from eval.generator import write_corpus; print(write_corpus(Path('eval/corpus')))"
```

Stage 3 evaluation (no API key, no network):

```bash
python -c "from eval.harness import write_results; write_results()"
```

Stage 4 compiler evaluation — replay the committed cache, no key needed:

```bash
python -m eval.compiler_harness --cache-only
```

To re-record the cache (requires `ANTHROPIC_API_KEY` or `ant auth login`; costs
real money):

```bash
python -m eval.compiler_harness --limit-seeds 3   # cheap smoke run first
python -m eval.compiler_harness                   # full train split
```

---

## Project status

| Stage | State |
|---|---|
| 1. Models, admissibility lattice, floor enforcement | complete |
| 2 / 2.5. Corpus, generator, hash lock, seed-level split | complete |
| 3. Constraint + receipt verifiers, harness, baselines | complete |
| 4. Obligation compiler (LLM) | built; awaiting first recorded run |
| 5. Semantic verifier + aggregator wired end to end | not started |
| 6. Exposure banding + settlement + Razorpay test rail | not started |
| 7. API + React console | not started |
| 8. Adversarial run, held-out eval, docs | not started |

Razorpay integration is **test mode only**. No production rails have been
exercised, and the README makes no claim that they have.

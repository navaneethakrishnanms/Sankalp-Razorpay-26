# SANKALP

**Intent-Fulfilment Clearing for Agentic UPI Commerce.**

An AI agent orders dinner for you. It gets the quantity wrong. Every signature
on the transaction verifies, the audit trail is complete and admissible — and
you are still out the money.

SANKALP is the layer that catches that before the payment clears.

---

## By the numbers

Stage 3, over a 1,359-record hash-locked corpus. Every figure below is
reproducible with `make eval` — no API key, no network.

| | Result |
|---|---|
| **Violation recall** (all violations in denominator) | **84.0%** `[79.6, 87.5]` |
| **False-block rate** (correct orders wrongly stopped) | **0.0%** `[0.0, 0.4]` |
| Recall within deterministic expressive power | 100.0% `[98.6, 100.0]` |
| Added latency, p50 / p95 | 0.062 ms / 0.123 ms |
| Unexpected misses | 0 |
| Corpus mislabels found by audit | 0 |
| Test suite | 408 passing |

**What this means in practice.** SANKALP catches five of every six agent errors
while never once interrupting a correct order. The one-in-six it misses is not
an unknown blind spot — it is a *characterised* one: 52 records the corpus
deliberately builds to be invisible to deterministic verification, which is
precisely the work the semantic verifier does at Stage 5. At 0.062 ms p50 it is
free at the point of decision.

Against the only two baselines that matter:

| System | Recall | False-block | Verdict |
|---|---|---|---|
| block-nothing | 0% | 0% | no protection |
| block-everything | 100% | 100% | unusable product |
| **SANKALP** | **84.0%** | **0.0%** | both, at once |

---

## The gap this closes

Two different questions have to be answered before an agent's payment settles:

1. *Was the agent allowed to act, and is the record authentic?*
2. *Did the agent actually do what you asked?*

**Google's Agent Payments Protocol (AP2)** answers the first. It establishes a
cryptographic chain of *Mandates* — signed, tamper-evident credentials recording
what the user authorised (Intent), what the agent assembled (Cart), and what is
charged (Payment). The result is a non-repudiable audit trail.

**RAILS** (arXiv 2606.08790) names the same boundary from the research side:
authorization establishes *permitted agency*, not *fulfilled obligation*.

Neither answers the second question. An agent can produce a perfectly valid
Intent → Cart → Payment chain for an order that gets the quantity wrong. Every
signature verifies. The dispute still has to be adjudicated by someone.

**SANKALP is that adjudication, mechanised.** It consumes authority evidence of
the shape AP2 produces and adds the determination those protocols explicitly
leave to an adjudicator: *did the delivered cart satisfy the obligation the user
actually expressed?*

We claim the implementation, not the diagnosis. The gap is named by others; what
is here is a working, measured system that closes it.

> **⚠ Verify before presenting.** The AP2 details above come from a secondary
> summary and are **not yet confirmed against Google's primary sources**. Check:
> launch date, partner count, the Intent/Cart/Payment mandate names, W3C
> Verifiable Credential issuance, and — most importantly — that AP2's own scope
> statement leaves fulfilment adjudication out. The positioning survives if a
> detail is wrong; a wrong specific in front of a payments panel does not.

---

## How it works: an admissibility lattice

Every LLM-verification system eventually faces the question *"what stops your
model from hallucinating a PASS?"* The usual answers — better prompts, higher
thresholds, more judges voting — are statistical, and all of them degrade under
adversarial pressure.

SANKALP's answer is structural. Evidence is typed by the trustworthiness of its
source, in a total order:

```
SELF (0)  <  SIGN (1)  <  WIT (2)  <  REC (3)  <  ATT (4)
```

Each obligation declares an **admissibility floor**. Every verifier must declare
the evidence it consulted. A verifier whose evidence falls below the floor is
assigned weight zero and **excluded before aggregation runs** — not outvoted,
not down-weighted. Removed from the set.

The consequence, proven as a unit test
([`test_admissibility.py::TestFloorEnforcementFooledJudge`](tests/unit/test_admissibility.py)):

> A semantic verifier returning **PASS at 0.99 confidence** on a `SELF`-class
> basis, against a `REC` floor, is **structurally absent from the survivor set,
> not outvoted.** The surviving constraint verifier's `REC`-based FAIL carries
> the decision.

Confidence is irrelevant to exclusion. A more persuasive hallucination is not a
more dangerous one, because persuasiveness was never what got checked.

The **non-promotion invariant** makes this hold under composition:
`join([SELF, SIGN]) = SIGN`, which still does not clear a `REC` floor. Two fooled
LLM verifiers agreeing with each other contribute exactly zero.

### The LLM never authors a value

The obligation compiler does not emit numbers. It emits **spans** — verbatim
substrings of the user's instruction — and deterministic Python parses values
out of them:

```
instruction : "Order dinner for 4 people ... under ₹1500"
model emits : value_span="4 people"      budget_ceiling_span="under ₹1500"
code parses : 4                          Decimal("1500")
```

A span absent from the instruction is rejected
([`output_validator.py`](core/guards/output_validator.py)), so a hallucinated
"under ₹2000" cannot survive — it is not in the user's text. This is strictly
stronger than rejecting digits: it also catches authored values containing no
digits, such as an invented merchant or an ingredient the user never mentioned.

---

## Architecture

```
POST /api/clear
   │
   ├─ 1. Obligation compiler   (LLM)  NL instruction → AcceptanceCriterion[]
   ├─ 2. Binder                       freeze + hash; unresolvable path = hard fail
   ├─ 3. Evidence envelope            assign classes via provenance meet
   ├─ 4. Exposure scorer       (det)  → band LOW | MODERATE | ELEVATED
   ├─ 5. Verifier mesh                band selects which verifiers run
   │      ├─ constraint  (det)  predicate evaluation over a closed registry → REC
   │      ├─ receipt     (det)  merchant catalogue cross-check              → REC
   │      └─ semantic    (LLM)  declares its own basis
   ├─ 6. Floor enforcement     (det)  sub-floor verifiers → weight 0, excluded pre-join
   ├─ 7. Aggregator            (det)  weighted verdict, join over survivors only
   ├─ 8. Clearing decision            performance + policy verdict, fault, confidence
   └─ 9. Settlement instruction       EXECUTE | HOLD | CLARIFY | ABORT
```

Only steps 1 and 5-semantic involve a model. Everything else is deterministic
Python. Design decisions and deliberate deviations are recorded in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

### Open weights, as a design claim

The obligation compiler runs on **`openai/gpt-oss-120b` via Groq** — open
weights, not a frontier model. That is a claim about the architecture, not a
budget decision.

SANKALP's safety property does not come from the model being good. It comes
from floor enforcement: a verifier's verdict counts only if the evidence it
declared clears the obligation's admissibility floor, and that check is
deterministic Python that never asks how capable the model was. If the guarantee
were really "we used a strong model", the lattice would be decoration.

Running the one hard AI component on open weights is the test of that claim. We
expect worse extraction and worse Hinglish handling than a frontier model would
give, and those numbers are reported as they come. What must **not** degrade is
the structural property — a sub-floor verifier contributes zero regardless of
which model produced it.

The Anthropic path is kept working and selected by configuration
(`SANKALP_LLM_PROVIDER=anthropic`), so the swap is reversible and the claim is
testable in both directions.

**Reproducibility without an API key.** Every LLM response is cached on disk,
keyed by a hash over (provider, model, system, prompt, max_tokens, **temperature**,
effort, prompt_version), and `eval/llm_cache/` is **committed**. Model and
temperature are in the key deliberately: without them, switching provider would
replay the old model's responses under the new model's name and corrupt every
metric. `CacheOnlyProvider` raises on a miss rather than silently generating
fresh output, so CI either reproduces the recorded bytes exactly or fails.

Determinism comes from `temperature=0` plus that cache — note that Anthropic
*removed* sampling parameters on current models (a request carrying `temperature`
is rejected with a 400), which is why temperature is applied per-provider rather
than globally.

### Credentials

Keys live in `.env`, which is gitignored; `.env.example` is the committed
template and holds placeholders only. Keys are read from the environment at
client construction only — never inside business logic, never logged, never in a
cache key, never in an error message. `scripts/check_no_secrets.py` fails CI if a
live-looking key appears anywhere in the tree, and runs before the test suite so
a leak stops the build immediately.

```bash
cp .env.example .env      # then edit .env and add your key
```

---

## Why the numbers can be trusted

The measurement design is the part most likely to be wrong in a project like
this, so it was built first and locked.

| Control | What it prevents |
|---|---|
| **Corpus built forward from the world** — violations made by mutating the *cart*, never by inventing criteria | Labels deriving from the component under test |
| **45 hand-authored seeds**, not 45 templates of one | Correlated samples inflating the effective sample size |
| **Seed-level stratified split** (31 train / 14 holdout) | Near-duplicate records straddling the split — leakage |
| **Traceability assertion** — every `stated` criterion's phrases must appear literally in the instruction | A "stated" requirement the user never actually stated |
| **52 deliberately uncatchable violations** | Measuring a tautology; audited every run, 0 mislabels |
| **Hash-locked corpus + pre-registration**, checked in CI | Silent corpus drift; metrics chosen after seeing results |
| **Wilson intervals on every rate** | A point estimate passing as a measurement |

**Two reporting decisions worth stating plainly:**

*We do not report the requested 1% / 2% / 5% false-block sweep.* A deterministic
predicate evaluator has no confidence knob: on a correctly-labelled CLEAN record
it is either right or wrong, and no threshold changes that. Sweeping a fake
threshold over a binary signal would produce three identical numbers plotted as
a curve — theatre, not measurement. The sweep becomes meaningful at Stage 4/5,
where the compiler and semantic verifier introduce genuine continuous-confidence
error. Reasoning in [`eval/harness.py`](eval/harness.py).

*The 0.0% false-block rate is expected, not impressive.* Over a
correctly-labelled CLEAN population a deterministic verifier **should** score
exactly zero. It is reported as a regression check — nonzero means either the
verifier or the corpus is wrong — and it earned its keep in exactly that way
once, below.

**On the train/holdout gap.** Train reads 86.1%, holdout 78.7%. This is *not*
overfitting: at Stage 3 nothing fits — the verifiers are deterministic code
written before the split existed and they never see a label. Because
catchable-only recall is exactly 100%, headline recall reduces to the ratio of
catchable to uncatchable violations in each split (198/230 vs 74/94; 198 + 74 =
272 ✓). The holdout simply carries proportionally more semantic violations. The
intervals overlap heavily. The gap becomes meaningful at Stage 4, when the
prompt *is* something that can overfit.

---

## Disclosure: a corpus label bug found by the component under test

This project's rule is that **labels never derive from the thing being
measured**. The following bends that rule, so it is disclosed rather than buried.

**What it was.** Six CLEAN records were mislabelled. A distractor mutation,
`_clean_extra_uncovered_item`, adds an item to the cart "that no criterion
covers." Two seeds declare a `distinct_item_count == N` criterion — and adding a
distinct item is exactly what that criterion detects. Those carts genuinely
failed a `stated` criterion while labelled CLEAN.

**How it was found.** By running the Stage 3 constraint verifier — the component
under test — over the corpus. The false-block proxy returned 0.63% where a
deterministic verifier over a correctly-labelled CLEAN set must return exactly
0%. That gap was the signal. It was invisible for all of Stage 2.5, because
nothing before Stage 3 evaluated criteria against carts.

**Why this corrects a mislabelling rather than tuning toward a number.** The six
carts violated a criterion the user's own instruction stated. They were
mislabelled the moment they were generated, independently of who noticed. The
verifier supplied *detection*; ground truth remained the hand-authored criteria,
written before any verifier existed.

**Why the fix is structural.** `_clean_extra_uncovered_item` now declines to fire
on seeds carrying a `distinct_item_count` criterion; and — the durable half —
`mutate_clean_dispatch` now validates every candidate CLEAN cart with the real
`evaluate_constraint_checks` instead of a hand-picked subset of checks. No
individual record was edited. The corpus was regenerated and re-locked.

**Residual risk, stated.** The generator's self-check now imports the verifier,
so a bug *inside* `evaluate_constraint_checks` would produce a corpus consistent
with that bug. The coupling is real. It is bounded by the fact that CLEAN labels
still originate from hand-authored criteria written before the verifier existed —
the verifier can only detect a contradiction between cart and criteria, never
author either. Full write-up in [`FAILURES.md`](FAILURES.md).

---

## Running it

```bash
pip install -e ".[dev]"
make test          # 408 tests, no API key required
make eval          # Stage 3 evaluation → eval/results/
```

Regenerate the corpus (only after changing the generator — rewrites the hash lock):

```bash
make corpus
```

Stage 4 compiler evaluation, replaying the committed cache — no key, no spend:

```bash
make eval-stage4
```

Re-recording the cache requires `ANTHROPIC_API_KEY` and **costs real money**.
Smoke-test first:

```bash
python -m eval.compiler_harness --limit-seeds 3   # cheap
make eval-stage4-record                            # full train split
```

> On Windows, override the interpreter if `python` is not on PATH:
> `make test PY=C:\Users\nk\anaconda3\python.exe`

---

## Status

| Stage | State |
|---|---|
| 1. Models, admissibility lattice, floor enforcement | ✅ complete |
| 2 / 2.5. Corpus, generator, hash lock, seed-level split | ✅ complete |
| 3. Constraint + receipt verifiers, harness, baselines | ✅ complete |
| 4. Obligation compiler (LLM) | 🔨 built & tested; **no measured numbers yet** |
| 5. Semantic verifier + aggregator end to end | ⬜ not started |
| 6. Exposure banding + settlement + Razorpay test rail | ⬜ not started |
| 7. API + React console | ⬜ not started |
| 8. Adversarial run, held-out eval, docs | ⬜ not started |

Stage 4 is implemented and unit-tested, but **no extraction accuracy,
source-labelling, or recall-delta figures are reported** until a recorded run
produces them. Razorpay integration is **test mode only** — no production rail
has been exercised, and nothing here claims otherwise.

---

## Repository map

```
core/
  models/          7 frozen Pydantic models, hash-chained
  models/fields.py closed registry — 14 resolvable paths, unknown = hard fail
  admissibility/   meet · join · propagated_class · apply_floor
  verifiers/       constraint · receipt (deterministic, independent)
  obligation/      compiler · ambiguity · binder
  llm/             provider-agnostic client + committed response cache
  guards/          output validator — the no-authored-values rule
agent/catalogue.py synthetic merchant catalogue (the "world")
eval/
  generator.py     deterministic corpus generator
  harness.py       Stage 3 metrics
  compiler_harness.py  Stage 4 metrics + recall delta
  PRE_REGISTERED.md    hash-locked before any verifier existed
  corpus/ results/ llm_cache/
```

# SANKALP

**Intent-Fulfilment Clearing for Agentic UPI Commerce.**

An AI agent orders dinner for you. It gets the quantity wrong, or swaps an
ingredient, or reports back that everything went fine when it didn't. Every
authorization signature on the transaction verifies. The audit trail is
complete and admissible. You are still out the money.

SANKALP is the layer that catches that before the payment clears.

**Live demo of the core mechanism:** [FloorGate — a confident verdict, structurally excluded before it can vote](https://claude.ai/code/artifact/962246ec-47fd-47a2-8974-d9f79c434ac4)

---

## By the numbers

Every figure below is reproducible: `make test` (521 tests, no API key),
`make eval` (Stage 3, deterministic), `make eval-stage4` / a live Stage 5 run
(replays the committed cache — no key, no spend once recorded).

| | Result | Stage |
|---|---|---|
| Deterministic violation recall (all violations, incl. structurally uncatchable ones) | **84.0%** `[79.6, 87.5]` | 3 |
| Deterministic false-block rate | **0.0%** `[0.0, 0.4]` | 3 |
| Compiler false-block, prompt v1 → v2 (same 3-seed sample) | 14.3% → **0.0%** | 4 |
| Compiler extraction recall, v1 → v2 | 50.0% → **75.0%** | 4 |
| Deceptive self-report: order NOT wrongly cleared, with floor | **100%** `[80.6, 100]` (n=16) | 5 |
| — same, floor disabled (counterfactual) | 100% `[80.6, 100]` (n=16) — see note below | 5 |
| Test suite | **521 passing**, 0 failing | — |

**What this means in practice.** The deterministic layer alone catches five of
every six agent errors while never once interrupting a correct order — and the
sixth is a *characterised* gap (52 records the corpus builds to be invisible to
deterministic verification on purpose), not an unknown blind spot. The
compiler's single documented prompt revision cut its false-block rate to zero
on the sample measured. And the deceptive-self-report result — read the note
below before quoting it; it is more interesting than "100%" suggests.

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
leave to an adjudicator: *did the delivered cart satisfy the obligation the
user actually expressed?*

We claim the implementation, not the diagnosis. The gap is named by others; what
is here is a working, measured system that closes it.

> **⚠ Verify before presenting.** The AP2 description above has not been
> independently re-verified against Google's primary sources this session.
> Before this section is shown externally, confirm: launch date, partner
> count, the Intent/Cart/Payment mandate names, W3C Verifiable Credential
> issuance, and — most importantly — that AP2's own scope statement leaves
> fulfilment adjudication out. The positioning survives if a detail is wrong;
> a wrong specific in front of a payments panel does not.

---

## How it works: an admissibility lattice

The usual answer to *"what stops your model from hallucinating a PASS?"* is
statistical — better prompts, higher thresholds, more judges voting — and
degrades under adversarial pressure. SANKALP's answer is structural. Evidence
is typed by the trustworthiness of its source, in a total order:

```
SELF (0)  <  SIGN (1)  <  WIT (2)  <  REC (3)  <  ATT (4)
```

Each obligation declares an **admissibility floor**. Every verifier must declare
the evidence it consulted — and it cannot lie about that declaration, because
the declaration is not the model's to make (see
[ARCHITECTURE.md](ARCHITECTURE.md)). A verifier whose evidence falls below the
floor is assigned weight zero and **excluded before aggregation runs** — not
outvoted, not down-weighted. Removed from the set, before `join` ever sees it.

Proven at the unit level
([`test_admissibility.py::TestFloorEnforcementFooledJudge`](tests/unit/test_admissibility.py))
and now live, through the real engine, against real corpus data
([`test_stage5.py::TestLiveFooledJudge`](tests/unit/test_stage5.py)):

> A semantic verifier returning **PASS at 0.99 confidence** on a `SELF`-class
> basis, against a `REC` floor, is **structurally absent from the survivor set,
> not outvoted.** The surviving constraint verifier's `REC`-based FAIL carries
> the decision.

The **non-promotion invariant** makes this hold under composition:
`join([SELF, SIGN]) = SIGN`, which still does not clear a `REC` floor. Two
fooled LLM verifiers agreeing with each other contribute exactly zero.

### The Stage 5 headline, read correctly

Running this live against 42 deceptive-self-report records split the population
in a way that matters:

- **26 records** already have a deterministic `stated`-criterion FAIL as backup
  — this project's own enforcement rule makes a `stated` FAIL absolute, floor
  or no floor, so this half shows ~0% gap *by design*, not because the floor
  is inert.
- **16 records** have *no* deterministic verifier at all — the self-report is
  the only evidence, and this is where the mechanism's value would show up as
  a measured gap.

On those 16, the live open-weights model returned **ABSTAIN**, not a confident
PASS, on every single one — it correctly treated the bare self-report as an
unverifiable claim rather than trusting it. Because ABSTAIN routes to CLARIFY
either way, the floor was never actually needed to prevent a wrongful clear in
*this specific run* — there was no confident wrong verdict for it to exclude.

That is a real, reportable result, not a disappointing one: the model's own
caution and the floor's structural guarantee are two independent safety
properties, and this run exercised the first without needing the second. The
second is proven separately, deterministically, by a scripted test that forces
the failure mode
([`TestLiveFooledJudge`](tests/unit/test_stage5.py)) — full account in
[FAILURES.md](FAILURES.md).

### The LLM never authors a value

The obligation compiler emits **spans** — verbatim substrings of the user's
instruction — never numbers. Deterministic Python parses values out of them:

```
instruction : "Order dinner for 4 people ... under ₹1500"
model emits : value_span="4 people"      budget_ceiling_span="under ₹1500"
code parses : 4                          Decimal("1500")
```

A span absent from the instruction is rejected
([`output_validator.py`](core/guards/output_validator.py)), so a hallucinated
"under ₹2000" cannot survive — it is not in the user's text.

---

## Architecture

```
POST /api/clear
   │
   ├─ 1. Obligation compiler   (LLM)  NL instruction → AcceptanceCriterion[]
   ├─ 2. Binder                       freeze + hash; unresolvable path = hard fail
   ├─ 3. Evidence envelope            catalogue → REC, agent self-report → SELF
   ├─ 4. Exposure scorer       (thin) band LOW | MODERATE | ELEVATED — not built this pass
   ├─ 5. Verifier mesh
   │      ├─ constraint  (det)  predicate evaluation over a closed registry → REC
   │      ├─ receipt     (det)  merchant catalogue cross-check              → REC
   │      └─ semantic    (LLM)  declares its basis from evidence it was given, never from the model
   ├─ 6. Floor enforcement     (det)  sub-floor verifiers → weight 0, excluded pre-join
   ├─ 7. Aggregator            (det)  weighted verdict, join over survivors only
   ├─ 8. Clearing decision            performance + policy verdict, fault, confidence
   └─ 9. Settlement instruction       EXECUTE | HOLD | CLARIFY | ABORT
```

Only steps 1 and 5-semantic involve a model. Everything else is deterministic
Python. Design decisions and deliberate deviations — including the two Stage 5
integration findings — are recorded in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Open weights, as a design claim

The obligation compiler and semantic verifier run on **`openai/gpt-oss-120b`
via Groq** — open weights, not a frontier model. That is a claim about the
architecture, not a budget decision.

SANKALP's safety property does not come from the model being good. It comes
from floor enforcement: a verifier's verdict counts only if the evidence it
declared clears the obligation's admissibility floor, and that check is
deterministic Python that never asks how capable the model was. Running the
one hard AI component on open weights is the test of that claim, and the
Stage 5 result above is exactly the evidence for it: even where the live model
behaved cautiously rather than confidently, the structural guarantee — proven
separately — did not depend on that caution to hold.

The Anthropic provider path is kept working and selected by configuration
(`SANKALP_LLM_PROVIDER=anthropic`), so the swap is reversible and the claim is
testable in both directions.

### Reproducibility without an API key

Every LLM response is cached on disk, keyed by a hash over (provider, model,
system, prompt, max_tokens, **temperature**, effort, prompt_version), and
`eval/llm_cache/` is **committed**. Model and temperature are in the key
deliberately: without them, switching provider would replay the old model's
responses under the new model's name and corrupt every metric.
`CacheOnlyProvider` raises on a miss rather than silently generating fresh
output, so CI either reproduces the recorded bytes exactly or fails.

### Credentials

Keys live in `.env` (gitignored); `.env.example` is the committed template
with placeholders only. `scripts/check_no_secrets.py` fails CI if a
live-looking key appears anywhere in the tracked tree, and runs before the
test suite so a leak stops the build immediately.

```bash
cp .env.example .env      # then edit .env and add GROQ_API_KEY
```

---

## Why the numbers can be trusted

| Control | What it prevents |
|---|---|
| **Corpus built forward from the world** — violations made by mutating the *cart*, never by inventing criteria | Labels deriving from the component under test |
| **45 hand-authored seeds**, seed-level stratified split (31 train / 14 holdout) | Correlated samples, near-duplicate records straddling the split |
| **Traceability assertion** — every `stated` criterion's phrases, and every obligation-level field, must appear literally in the instruction | A "stated" requirement the user never actually stated — extended to obligation fields after it bit once (see below) |
| **52 deliberately uncatchable violations** | Measuring a tautology; audited every run |
| **Hash-locked corpus + pre-registration**, checked in CI | Silent corpus drift; metrics chosen after seeing results |
| **Wilson intervals on every rate** | A point estimate passing as a measurement |
| **`declared_basis` unsettable by the model** | Floor enforcement filtering on a verifier's own unverifiable claim |
| **One prompt iteration per stage, both runs reported** | Prompt tuning against the measured set passing as a fixed result |

*We do not report a false-block sweep at multiple operating points.* A
deterministic predicate evaluator has no confidence knob: on a
correctly-labelled CLEAN record it is either right or wrong, and no threshold
changes that. The single achieved point is reported with its Wilson interval
instead of a fabricated curve.

---

## Disclosure: three corpus regenerations, and what each one cost

This project's rule is that **labels never derive from the thing being
measured**. All three regenerations below were triggered by a component under
test surfacing a defect the previous stage could not see, and all three were
fixed structurally — the generator's own logic was corrected, not individual
records. Documented together because leaving three regenerations unstated
would look like the corpus was shaped until the numbers cooperated; disclosed,
each one is process evidence.

1. **Stage 2.5 — scale-up.** 10 seeds → 45, to meet the pre-agreed statistical
   floors. Planned rework, not a defect.
2. **Stage 3 — found by the constraint verifier.** Six CLEAN records
   violated a `distinct_item_count` criterion; a distractor mutation added a
   distinct item to carts whose obligation counted distinct items. The
   false-block proxy came back 0.63% where a correct corpus must read exactly
   0% — that gap was the signal. Fixed by having the generator's own
   CLEAN-record safety check run the *real* verifier logic instead of a
   hand-picked subset of it. **2 of 45 seeds touched** (1 train, 1 holdout).
3. **Stage 4 — found by the obligation compiler.** 14 seeds carried a
   `budget_ceiling` or `delivery_window` that contradicted their own
   instruction text — introduced when Stage 2.5's budget-conflict fix changed
   field values without updating the instructions. The compiler read the
   instructions correctly and was scored wrong for it. Fixed by extending the
   traceability guard from criteria to obligation-level fields.
   **14 of 45 seeds touched** (9 train, 5 holdout).

**The holdout answer, stated plainly.** Across regenerations 2 and 3, **5 of
14 holdout seeds were edited** (`S05`, `S18`, `S23`, `S30`, `S39` — `S18` in
both). Holdout was not sealed through Stages 2–4; it was corrected after
inspection, twice, for the same reason train was: a downstream component
proved the ground truth wrong. A holdout corrected after inspection is weaker
evidence than one never touched, and that is stated here rather than left for
a reviewer to discover. From Stage 5 onward, holdout is sealed — this project's
own harness raises `HoldoutSealedError` if a holdout record reaches it before
Stage 8.

Full write-ups, including the two Stage 5 findings, in
[`FAILURES.md`](FAILURES.md). Reproducible: `python scripts/report_corpus_provenance.py`.

---

## Running it

```bash
pip install -e ".[dev]"
make test          # 521 tests, no API key required
make eval          # Stage 3 evaluation → eval/results/
```

Regenerate the corpus (only after changing the generator — rewrites the hash lock):

```bash
make corpus
python scripts/audit_seed_traceability.py   # must report clean before regenerating
```

Stage 4 compiler evaluation, replaying the committed cache — no key, no spend:

```bash
make eval-stage4                                          # v1
python -m eval.compiler_harness --cache-only --prompt-version v2   # v2
```

Stage 5, live (or `--cache-only` to replay):

```bash
python -c "from eval.stage5_harness import write_results; write_results()"
```

Re-recording any of the above requires `GROQ_API_KEY` in `.env` and **costs
real money** — smoke-test with `--limit-seeds 3` first.

> On Windows, override the interpreter if `python` is not on PATH:
> `make test PY=C:\path\to\python.exe`

---

## Status

| Stage | State |
|---|---|
| 1. Models, admissibility lattice, floor enforcement | ✅ complete |
| 2 / 2.5. Corpus, generator, hash lock, seed-level split | ✅ complete |
| 3. Constraint + receipt verifiers, harness, baselines | ✅ complete |
| 4. Obligation compiler, prompt v1 → v2, both reported | ✅ complete |
| 5. Semantic verifier + aggregator + floor enforcement, live | ✅ complete (subset, train only) |
| 6. Exposure banding + settlement instruction | 🔸 thin — settlement emitter with documented HOLD mapping; no rail call, no banding logic |
| 7. API + React console | 🔸 FloorGate built as a standalone visual demo (linked above); no FastAPI/React wiring |
| 8. Adversarial run, held-out eval, final docs | ⬜ not started — holdout stays sealed until here |

Razorpay integration is **test mode only, and not yet wired at all** —
`core/settlement/instruction.py` produces the hash-chained record and
documents the HOLD mapping but makes no rail call. Nothing here claims a
production rail has been touched.

---

## Repository map

```
core/
  models/          7 frozen Pydantic models, hash-chained
  models/fields.py closed registry — 14 resolvable paths, unknown = hard fail
  admissibility/   meet · join · propagated_class · apply_floor
  verifiers/       constraint · receipt · semantic (all three, wired live)
  clearing/        aggregator (order is the mechanism) · engine (evidence assembly)
  settlement/      instruction.py — thin, documented, no rail call
  obligation/      compiler · ambiguity · binder
  llm/             provider-agnostic client + committed response cache
  guards/          output validator — the no-authored-values rule
agent/catalogue.py synthetic merchant catalogue (the "world")
eval/
  generator.py         deterministic corpus generator
  harness.py           Stage 3 metrics
  compiler_harness.py  Stage 4 metrics + v1/v2 comparison
  stage5_harness.py    Stage 5 metrics — subset, train only
  PRE_REGISTERED.md    hash-locked before any verifier existed
  corpus/ results/ llm_cache/
scripts/
  check_no_secrets.py            CI secret scan
  audit_seed_traceability.py     corpus self-consistency check
  report_corpus_provenance.py    which seeds were edited, which side of the split
```

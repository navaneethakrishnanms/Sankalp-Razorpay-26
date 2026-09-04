# SANKALP — Stage 4 Results (obligation compiler)

Scope: train split only — holdout untouched at Stage 4. 3 seeds compiled, 91 train records in scope.

## Provenance

A metric without its model identifier is not reproducible. Every number in this file was produced by exactly this configuration.

| | |
|---|---|
| provider | `groq` |
| model | `openai/gpt-oss-120b` |
| temperature | 0.0 |
| reasoning effort | medium |
| prompt version | `obligation_compiler/v2` (obligation_compiler_v2.md) |
| cache hits / misses | 0 / 3 |
| pricing verified | yes |

## Criterion extraction

- gold criteria: 4
- captured: 3 | invented: 1 | missed: 1
- recall: 75.0% [30.1%, 95.4%] (n=4)
- precision: 75.0% [30.1%, 95.4%] (n=4)

## Obligation-field extraction

budget_ceiling, merchant_scope and delivery_window are not AcceptanceCriteria but drive four of the six violation classes. 'spurious' means the compiler invented a limit the user never set — the most expensive error here, because it blocks correct orders.

| field | correct | missed | spurious | wrong value | accuracy |
|---|---|---|---|---|---|
| `budget_ceiling` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |
| `merchant_scope` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |
| `delivery_window` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |

## Source labelling (the metric that hides failure)

Scored only on criteria that matched gold by (field, operator, value). stated_labelled_as_inferred is the dangerous direction: the criterion survives but can no longer block, so violations clear while extraction accuracy still looks clean.

- matched criteria scored: 3
- accuracy: 100.0% [43.9%, 100.0%] (n=3)
- **stated-labelled-as-inferred: 0.0% [0.0%, 56.1%] (n=3)** (dangerous direction)
- inferred-labelled-as-stated: 0.0% [0.0%, 56.1%] (n=3)

## Unresolvable paths

- count: 0 | rate: 0.0% [0.0%, 49.0%] (n=4)
- paths: []

## Ambiguity detection

instruction_ambiguous was assigned per-seed by hand during corpus authoring from an intuitive reading, not a written-down rule — it is the noisiest label in the corpus. Read these as agreement with the author's intuition, not as ground truth.

- precision: 0.0% [0.0%, 0.0%] (n=0) | recall: 0.0% [0.0%, 0.0%] (n=0)

## Delta vs hand-authored criteria — the compiler's true cost

Stage 3 verifiers run over the same train records twice — once with hand-authored obligations, once with compiled ones. This is the compiler's true cost at the point of decision.

| | gold criteria | compiled criteria | delta |
|---|---|---|---|
| recall | 85.7% [68.5%, 94.3%] (n=28) | 85.7% [68.5%, 94.3%] (n=28) | +0.0% |
| false-block | 0.0% [0.0%, 5.8%] (n=63) | 0.0% [0.0%, 5.8%] (n=63) | +0.0% |

### What caused the false blocks

A false-block rate says the compiler is wrong; the cause says where.

_No clean record was blocked by the compiled criteria._

Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.

## Cost and latency

- cost (**estimate**, not spend — ESTIMATE, not spend. Computed from cached token counts at published rates; no live API calls occurred during THIS run if cache_misses is 0 below. This is what the recorded compilations would have cost, not a charge incurred by running the harness again.): Rs 0.225350400 across 3 compilations (mean Rs 0.075116800 each, at 88.0 INR/USD)
- cache this run: 0 hits / 3 misses
- latency p50 2.724s / p95 2.724s — Measured live over 3 uncached call(s) in this run. Cached hits are excluded from these percentiles.

## Per-language

| language | seeds | extraction recall | extraction precision |
|---|---|---|---|
| en | 3 | 75.0% [30.1%, 95.4%] (n=4) | 75.0% [30.1%, 95.4%] (n=4) |

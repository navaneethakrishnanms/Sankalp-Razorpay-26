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
| prompt version | `obligation_compiler/v1` (obligation_compiler_v1.md) |
| cache hits / misses | 3 / 0 |
| pricing verified | **NO** — PLACEHOLDER — verify at https://groq.com/pricing before publishing |

## Criterion extraction

- gold criteria: 4
- captured: 2 | invented: 3 | missed: 2
- recall: 50.0% [15.0%, 85.0%] (n=4)
- precision: 40.0% [11.8%, 76.9%] (n=5)

## Obligation-field extraction

budget_ceiling, merchant_scope and delivery_window are not AcceptanceCriteria but drive four of the six violation classes. 'spurious' means the compiler invented a limit the user never set — the most expensive error here, because it blocks correct orders.

| field | correct | missed | spurious | wrong value | accuracy |
|---|---|---|---|---|---|
| `budget_ceiling` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |
| `merchant_scope` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |
| `delivery_window` | 3 | 0 | 0 | 0 | 100.0% [43.9%, 100.0%] (n=3) |

## Source labelling (the metric that hides failure)

Scored only on criteria that matched gold by (field, operator, value). stated_labelled_as_inferred is the dangerous direction: the criterion survives but can no longer block, so violations clear while extraction accuracy still looks clean.

- matched criteria scored: 2
- accuracy: 100.0% [34.2%, 100.0%] (n=2)
- **stated-labelled-as-inferred: 0.0% [0.0%, 65.8%] (n=2)** (dangerous direction)
- inferred-labelled-as-stated: 0.0% [0.0%, 65.8%] (n=2)

## Unresolvable paths

- count: 0 | rate: 0.0% [0.0%, 43.5%] (n=5)
- paths: []

## Ambiguity detection

instruction_ambiguous was assigned per-seed by hand during corpus authoring from an intuitive reading, not a written-down rule — it is the noisiest label in the corpus. Read these as agreement with the author's intuition, not as ground truth.

- precision: 0.0% [0.0%, 79.3%] (n=1) | recall: 0.0% [0.0%, 0.0%] (n=0)

## Delta vs hand-authored criteria — the compiler's true cost

Stage 3 verifiers run over the same train records twice — once with hand-authored obligations, once with compiled ones. This is the compiler's true cost at the point of decision.

| | gold criteria | compiled criteria | delta |
|---|---|---|---|
| recall | 85.7% [68.5%, 94.3%] (n=28) | 85.7% [68.5%, 94.3%] (n=28) | +0.0% |
| false-block | 0.0% [0.0%, 5.8%] (n=63) | 14.3% [7.7%, 25.0%] (n=63) | +14.3% |

### What caused the false blocks

A false-block rate says the compiler is wrong; the cause says where.

| cause | clean records blocked |
|---|---|
| `criterion:quantity_sum eq 2` | 6 |
| `criterion:item.names contains 'chicken biryani'` | 3 |

Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.

## Cost and latency

- total: Rs 0.254786400 across 3 compilations (mean Rs 0.084928800 each, at 88.0 INR/USD)
- latency p50 0.002s / p95 0.002s — Cached responses report near-zero latency; re-record to measure live latency.

## Per-language

| language | seeds | extraction recall | extraction precision |
|---|---|---|---|
| en | 3 | 50.0% [15.0%, 85.0%] (n=4) | 40.0% [11.8%, 76.9%] (n=5) |

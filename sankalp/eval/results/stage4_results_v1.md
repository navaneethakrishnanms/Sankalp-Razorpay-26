# SANKALP — Stage 4 Results (obligation compiler)

Scope: train split only — holdout untouched at Stage 4. 31 seeds compiled, 943 train records in scope.

## Provenance

A metric without its model identifier is not reproducible. Every number in this file was produced by exactly this configuration.

| | |
|---|---|
| provider | `groq` |
| model | `openai/gpt-oss-120b` |
| temperature | 0.0 |
| reasoning effort | medium |
| prompt version | `obligation_compiler/v1` (obligation_compiler_v1.md) |
| cache hits / misses | 3 / 28 |
| pricing verified | **NO** — PLACEHOLDER — verify at https://groq.com/pricing before publishing |

## Criterion extraction

- gold criteria: 37
- captured: 19 | invented: 22 | missed: 18
- recall: 51.3% [35.9%, 66.5%] (n=37)
- precision: 46.3% [32.1%, 61.3%] (n=41)

## Source labelling (the metric that hides failure)

Scored only on criteria that matched gold by (field, operator, value). stated_labelled_as_inferred is the dangerous direction: the criterion survives but can no longer block, so violations clear while extraction accuracy still looks clean.

- matched criteria scored: 19
- accuracy: 100.0% [83.2%, 100.0%] (n=19)
- **stated-labelled-as-inferred: 0.0% [0.0%, 16.8%] (n=19)** (dangerous direction)
- inferred-labelled-as-stated: 0.0% [0.0%, 16.8%] (n=19)

## Unresolvable paths

- count: 0 | rate: 0.0% [0.0%, 8.6%] (n=41)
- paths: []

## Ambiguity detection

instruction_ambiguous was assigned per-seed by hand during corpus authoring from an intuitive reading, not a written-down rule — it is the noisiest label in the corpus. Read these as agreement with the author's intuition, not as ground truth.

- precision: 40.0% [16.8%, 68.7%] (n=10) | recall: 100.0% [51.0%, 100.0%] (n=4)

## Delta vs hand-authored criteria — the compiler's true cost

Stage 3 verifiers run over the same train records twice — once with hand-authored obligations, once with compiled ones. This is the compiler's true cost at the point of decision.

| | gold criteria | compiled criteria | delta |
|---|---|---|---|
| recall | 89.7% [85.7%, 92.7%] (n=292) | 89.4% [85.3%, 92.4%] (n=292) | -0.3% |
| false-block | 0.0% [0.0%, 0.6%] (n=651) | 15.5% [12.9%, 18.5%] (n=651) | +15.5% |

### What caused the false blocks

A false-block rate says the compiler is wrong; the cause says where.

| cause | clean records blocked |
|---|---|
| `criterion:quantity_sum eq 2` | 35 |
| `budget_ceiling` | 25 |
| `criterion:quantity_sum eq 3` | 18 |
| `criterion:quantity_sum eq 5` | 9 |
| `criterion:item.names contains 'masala dosa'` | 6 |
| `criterion:quantity_sum eq 10` | 6 |
| `criterion:item.names contains 'dal makhani'` | 4 |
| `criterion:quantity_sum eq 6` | 3 |
| `criterion:min_item_quantity eq 2` | 2 |

Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.

## Cost and latency

- total: Rs 2.711596800 across 31 compilations (mean Rs 0.08747086451612903225806451613 each, at 88.0 INR/USD)
- latency p50 22.714s / p95 27.284s — Cached responses report near-zero latency; re-record to measure live latency.

## Per-language

| language | seeds | extraction recall | extraction precision |
|---|---|---|---|
| en | 22 | 57.7% [39.0%, 74.5%] (n=26) | 55.6% [37.3%, 72.4%] (n=27) |
| hinglish | 9 | 36.4% [15.2%, 64.6%] (n=11) | 28.6% [11.7%, 54.6%] (n=14) |

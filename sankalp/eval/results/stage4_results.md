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
- captured: 2 | invented: 2 | missed: 2
- recall: 50.0% [15.0%, 85.0%] (n=4)
- precision: 50.0% [15.0%, 85.0%] (n=4)

## Source labelling (the metric that hides failure)

Scored only on criteria that matched gold by (field, operator, value). stated_labelled_as_inferred is the dangerous direction: the criterion survives but can no longer block, so violations clear while extraction accuracy still looks clean.

- matched criteria scored: 2
- accuracy: 100.0% [34.2%, 100.0%] (n=2)
- **stated-labelled-as-inferred: 0.0% [0.0%, 65.8%] (n=2)** (dangerous direction)
- inferred-labelled-as-stated: 0.0% [0.0%, 65.8%] (n=2)

## Unresolvable paths

- count: 0 | rate: 0.0% [0.0%, 49.0%] (n=4)
- paths: []

## Ambiguity detection

instruction_ambiguous was assigned per-seed by hand during corpus authoring from an intuitive reading, not a written-down rule — it is the noisiest label in the corpus. Read these as agreement with the author's intuition, not as ground truth.

- precision: 0.0% [0.0%, 79.3%] (n=1) | recall: 0.0% [0.0%, 0.0%] (n=0)

## Delta vs hand-authored criteria — the compiler's true cost

Stage 3 verifiers run over the same train records twice — once with hand-authored obligations, once with compiled ones. This is the compiler's true cost at the point of decision.

| | gold criteria | compiled criteria | delta |
|---|---|---|---|
| recall | 85.7% [68.5%, 94.3%] (n=28) | 100.0% [87.9%, 100.0%] (n=28) | +14.3% |
| false-block | 0.0% [0.0%, 5.8%] (n=63) | 42.9% [31.4%, 55.1%] (n=63) | +42.9% |

### What caused the false blocks

A false-block rate says the compiler is wrong; the cause says where.

| cause | clean records blocked |
|---|---|
| `criterion:quantity_sum eq 2` | 27 |

Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.

## Cost and latency

- total: Rs 0.229574400 across 3 compilations (mean Rs 0.076524800 each, at 88.0 INR/USD)
- latency p50 0.001s / p95 0.001s — Cached responses report near-zero latency; re-record to measure live latency.

## Per-language

| language | seeds | extraction recall | extraction precision |
|---|---|---|---|
| en | 3 | 50.0% [15.0%, 85.0%] (n=4) | 50.0% [15.0%, 85.0%] (n=4) |

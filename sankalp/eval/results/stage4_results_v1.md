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
| cache hits / misses | 22 / 9 |
| pricing verified | **NO** — PLACEHOLDER — verify at https://groq.com/pricing before publishing |

## Criterion extraction

- gold criteria: 37
- captured: 20 | invented: 22 | missed: 17
- recall: 54.0% [38.4%, 69.0%] (n=37)
- precision: 47.6% [33.4%, 62.3%] (n=42)

## Obligation-field extraction

budget_ceiling, merchant_scope and delivery_window are not AcceptanceCriteria but drive four of the six violation classes. 'spurious' means the compiler invented a limit the user never set — the most expensive error here, because it blocks correct orders.

| field | correct | missed | spurious | wrong value | accuracy |
|---|---|---|---|---|---|
| `budget_ceiling` | 30 | 1 | 0 | 0 | 96.8% [83.8%, 99.4%] (n=31) |
| `merchant_scope` | 23 | 1 | 7 | 0 | 74.2% [56.8%, 86.3%] (n=31) |
| `delivery_window` | 31 | 0 | 0 | 0 | 100.0% [89.0%, 100.0%] (n=31) |

Mismatches:

- `S04-biryani-not-oily-hinglish` **merchant_scope** gold=`None` compiled=`['rest-biryani']`
- `S17-southindian-idli-coffee` **merchant_scope** gold=`None` compiled=`['rest-southindian']`
- `S19-southindian-served-hot-category` **merchant_scope** gold=`food_delivery` compiled=`None`
- `S20-southindian-chettinad` **merchant_scope** gold=`None` compiled=`['rest-southindian']`
- `S22-southindian-coffee-hinglish` **merchant_scope** gold=`None` compiled=`['rest-southindian']`
- `S24-punjabi-hinglish-budget` **budget_ceiling** gold=`700.00` compiled=`None`
- `S27-punjabi-naan-hinglish` **merchant_scope** gold=`None` compiled=`['rest-punjabi']`
- `S28-punjabi-no-colour-ambiguous` **merchant_scope** gold=`None` compiled=`['rest-punjabi']`
- `S45-dailybasket-eggs-no-mutton` **merchant_scope** gold=`None` compiled=`['grocery-dailybasket']`

## Source labelling (the metric that hides failure)

Scored only on criteria that matched gold by (field, operator, value). stated_labelled_as_inferred is the dangerous direction: the criterion survives but can no longer block, so violations clear while extraction accuracy still looks clean.

- matched criteria scored: 20
- accuracy: 100.0% [83.9%, 100.0%] (n=20)
- **stated-labelled-as-inferred: 0.0% [0.0%, 16.1%] (n=20)** (dangerous direction)
- inferred-labelled-as-stated: 0.0% [0.0%, 16.1%] (n=20)

## Unresolvable paths

- count: 0 | rate: 0.0% [0.0%, 8.4%] (n=42)
- paths: []

## Ambiguity detection

instruction_ambiguous was assigned per-seed by hand during corpus authoring from an intuitive reading, not a written-down rule — it is the noisiest label in the corpus. Read these as agreement with the author's intuition, not as ground truth.

- precision: 36.4% [15.2%, 64.6%] (n=11) | recall: 100.0% [51.0%, 100.0%] (n=4)

## Delta vs hand-authored criteria — the compiler's true cost

Stage 3 verifiers run over the same train records twice — once with hand-authored obligations, once with compiled ones. This is the compiler's true cost at the point of decision.

| | gold criteria | compiled criteria | delta |
|---|---|---|---|
| recall | 89.7% [85.7%, 92.7%] (n=292) | 89.0% [84.9%, 92.1%] (n=292) | -0.7% |
| false-block | 0.0% [0.0%, 0.6%] (n=651) | 10.0% [7.9%, 12.5%] (n=651) | +10.0% |

### What caused the false blocks

A false-block rate says the compiler is wrong; the cause says where.

| cause | clean records blocked |
|---|---|
| `criterion:quantity_sum eq 3` | 18 |
| `criterion:quantity_sum eq 2` | 14 |
| `criterion:quantity_sum eq 4` | 9 |
| `criterion:item.names contains 'masala dosa'` | 6 |
| `criterion:quantity_sum eq 10` | 6 |
| `criterion:item.names contains 'dal makhani'` | 4 |
| `criterion:item.names contains 'chicken biryani'` | 3 |
| `criterion:quantity_sum eq 6` | 3 |
| `criterion:min_item_quantity eq 2` | 2 |

Per-seed gold-vs-compiled criteria are in `stage4_results.json` under `per_seed`.

## Cost and latency

- total: Rs 2.749731600 across 31 compilations (mean Rs 0.08870101935483870967741935484 each, at 88.0 INR/USD)
- latency p50 0.003s / p95 44.064s — Cached responses report near-zero latency; re-record to measure live latency.

## Per-language

| language | seeds | extraction recall | extraction precision |
|---|---|---|---|
| en | 22 | 61.5% [42.5%, 77.6%] (n=26) | 57.1% [39.1%, 73.5%] (n=28) |
| hinglish | 9 | 36.4% [15.2%, 64.6%] (n=11) | 28.6% [11.7%, 54.6%] (n=14) |

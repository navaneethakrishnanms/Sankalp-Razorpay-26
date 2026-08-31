# Model provenance — which model produced which stage's numbers

**Append-only. Never edit or delete a row.** A metric without its model
identifier is not reproducible, and a metric whose model identifier was
retroactively changed is worse than one with none.

## Why this is a separate file from `PRE_REGISTERED.md`

`eval/PRE_REGISTERED.md` is hash-locked and must not change after its first
commit — that lock is what stops metric definitions being edited after results
are seen. Model provenance is *not* a metric definition; it is a fact about a
run, and there will be one new fact per run. Appending run facts to the locked
file would either break the lock on every run or force the lock to be
permanently disabled, and a lock that is routinely broken protects nothing.

So provenance lives here, and this file is deliberately **not** hash-locked.
The machine-readable copy of the same facts is written into each run's
`eval/results/stage*_results.json` under `provenance`, so the numbers and their
model identifier can never be separated.

## Runs

| Stage | Date | Provider | Model | Temp | Effort | Prompt version | Notes |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08 | — | — | — | — | — | Models and lattice. No LLM involved. |
| 2 / 2.5 | 2026-08 | — | — | — | — | — | Corpus generation is fully deterministic. No LLM involved. |
| 3 | 2026-08-29 | — | — | — | — | — | **Zero LLM calls by design.** Deterministic verifiers only, so the Stage 3 numbers are model-independent and stay valid across any later provider change. |
| 4 | _pending first recorded run_ | `groq` | `openai/gpt-oss-120b` | 0.0 | medium | `obligation_compiler/v1` | Fill in from `stage4_results.json.provenance` after the run. |

## Provider change log

| Date | Change | Reason |
|---|---|---|
| 2026-08-29 | Default provider switched Anthropic → Groq (`openai/gpt-oss-120b`) | Open weights as a design claim, not an economy: the clearing layer's safety comes from floor enforcement, not model quality. Running the compiler on an open-weights model is evidence for that architecture. The Anthropic path is kept working and selectable via `SANKALP_LLM_PROVIDER=anthropic`, so the swap is reversible and the claim is testable in both directions. |

**Cache implication of any provider or model change.** The LLM response cache
keys on `(provider, model, system, prompt, max_tokens, temperature, effort,
prompt_version)`. Changing any of those produces new cache entries rather than
replaying old ones, so a provider swap can never silently serve the previous
model's responses under the new model's name. Verified by
`tests/unit/test_llm_client.py::TestCacheKey`.

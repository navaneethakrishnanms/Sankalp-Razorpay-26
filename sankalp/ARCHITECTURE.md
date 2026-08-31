# SANKALP — Architecture Notes

This file records deliberate deviations from the original spec and other
architectural decisions that aren't obvious from reading the code alone.
It is not the full system architecture document (that is a Stage 8
deliverable, per the build order) — it exists so decisions made along the
way are traceable to a reason instead of being rediscovered by accident
later.

## Deviation: `VerifierOutput.declared_basis` defaults to `SELF`, not a hard error

**What the original spec said:** a verifier must explicitly declare its
evidence basis; there was no defined behaviour for a verifier that omits it.

**What the code does** (`core/models/verifier.py`, `core/admissibility/floor.py::verifier_basis_class`):
a `VerifierOutput` with an empty `declared_basis` is treated as `SELF`
class — the weakest possible class — rather than raising a validation
error at construction time.

**Why this is the safer default, not a shortcut:** the alternative (hard
error on missing basis) would let a buggy or malicious verifier crash the
pipeline simply by omitting the field, which is a worse failure mode than
silently under-trusting it. Defaulting to `SELF` means a verifier that
forgets to declare its basis is **structurally excluded** at any floor
above `SELF` (i.e. essentially every real obligation, since the default
floor is `REC`) — the same mechanism that excludes a dishonest verifier
excludes a careless one. This is deliberate, not an oversight: see
`core/models/verifier.py`'s `VerifierOutput` docstring and
`core/admissibility/floor.py::verifier_basis_class`'s "Edge cases" note.

This is a named, documented deviation, not a silent one. A future spec
revision that wants a hard error instead should treat this as the thing
being changed, not assume the current behaviour was unintentional.

## Corpus generation is programmatic-but-hand-authored, not templated-from-criteria

`eval/generator.py` builds every record from a hand-authored `Seed` (real
instruction text, real hand-derived `AcceptanceCriteria`, a cart that
already satisfies them) and mutates only the cart to produce a violation.
Criteria are never derived from a mutation, a verifier, or the compiler —
see the generator's module docstring, rule 1. This is why the corpus can
be trusted as ground truth for Stage 3+ metrics: nothing that measures the
system was used to construct the thing being measured.

## Deviation: determinism comes from the response cache, not `temperature=0`

**What the Stage 4 brief said:** the LLM client should use a "deterministic
seed".

**Why that is not implementable:** `temperature`, `top_p` and `top_k` were
**removed** on Claude Opus 5 / Sonnet 5 / Opus 4.6+ — a request carrying any
of them is rejected with a 400. There is no seed parameter on the Messages
API either. Writing `temperature=0` would not be a conservative choice; it
would be a runtime error.

**What the code does instead** (`core/llm/client.py`): every request is keyed
by a SHA-256 over (provider, model, system, prompt, max_tokens, effort,
prompt_version), responses are cached on disk, and `eval/llm_cache/` is
committed to the repository. `CacheOnlyProvider` raises on a cache miss
rather than falling through to a live call, so a CI run either reproduces
recorded results exactly or fails.

This is a *stronger* guarantee than a seed: a seed reproduces a sample from
one model version, whereas the cache reproduces the exact bytes the published
metrics were computed from, and survives model deprecation.

## The "LLM never authors a value" rule is enforced by spans, not digit-rejection

A literal reading of the project rule ("the LLM never authors a number") is
unimplementable for a compiler whose entire output is criteria containing
numbers. The rule is really *the model never originates a value*, and
`core/guards/output_validator.py` implements it in two parts:

- Value-bearing fields must be **verbatim substrings of the user's
  instruction** (`assert_quoted_span`). Deterministic Python then parses the
  number out of the quoted span. A hallucinated amount fails because the span
  is not in the user's text.
- Genuinely free-text fields must contain no digit spans and no URLs at all
  (`assert_no_authored_values`), since there is no source text to anchor them.

The span mechanism is strictly stronger than digit-rejection: it also catches
authored values containing no digits, such as an invented merchant name or a
prohibited ingredient the user never mentioned.

## Compiler drops-and-counts rather than failing the whole compilation

When the model emits an unresolvable field path or an unparseable value, the
offending criterion is dropped and recorded in
`CompilationResult.unresolvable_paths` / `.dropped_criteria`, which are
reported as first-class metrics. This is *not* the silent skip that
`core/models/fields.py` warns against: the count is published, and `bind()`
would reject the obligation outright if a bad path reached it. What must never
happen — a bad path quietly becoming an `ABSTAIN` inside the verifier — cannot
happen, because the path never reaches the verifier.

An unbacked `stated` label is **demoted to `inferred`** rather than trusted,
because a false `stated` causes a false block on a correct order.

## Train/holdout split is seed-level, not record-level

Records sharing a seed share a merchant, item vocabulary, and criteria
shape — they are correlated, not independent samples. Splitting at the
record level would put near-duplicate records on both sides of the split,
which is leakage. `eval/generator.build_split` assigns whole seeds to
train/holdout and stratifies so every violation subpopulation has at least
one contributing seed on each side. See Stage 2.5 corrections, Part A3.

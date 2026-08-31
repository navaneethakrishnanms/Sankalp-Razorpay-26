# SANKALP — Failure Log

Every bug that cost more than ~30 minutes to find or fix, in the order
encountered. Cause and fix only — the code is the source of truth for
current behaviour; this file is for the reasoning that isn't visible in a
diff.

---

## Stage 2.5 — budget ceiling conflicts between constraint mutations and BUDGET_BREACH on the same seed

**Cause.** When scaling the corpus from 10 to 45 seeds (Part A2 of the
Stage 2.5 corrections), several seeds were authored with both a
`CONSTRAINT_VIOLATION` mutation (swap item 0 for a catalogue item
containing a prohibited ingredient, or one with undeclared ingredients)
*and* a `BUDGET_BREACH` mutation on the same seed. The two mutations have
opposite budget requirements on the same `budget_ceiling` value:

- `mutate_constraint_catchable` / `mutate_constraint_abstain` need the
  ceiling to be **at or above** `swap_item_price × item0_quantity`, or the
  mutation's own safety check raises (correctly — it doesn't want to
  introduce an unintended second violation).
- `mutate_budget_breach` needs the ceiling to be **below**
  `clean_total + 2 × item0_price` (the minimum extra it can add), or the
  mutation can silently fail to actually exceed the ceiling depending on
  which random draw fires.

For seeds where the swap item's price was much higher than item 0's price
at a non-trivial quantity (e.g. swapping a ₹80 idli for a ₹290 chicken
chettinad at quantity 8: ₹640 → ₹2,320), these two constraints became
mutually unsatisfiable at any single ceiling value. The first sign was a
single `GeneratorError` from `pytest`; running the corpus and fixing one
error at a time (as in Stage 2) would have meant ~15-20 separate
edit/rerun cycles for a systemic pattern, not 15-20 unrelated bugs.

**Fix.** Wrote a standalone boundary-trial script that ran every `_PLAN`
row's mutation function against its seed under several deterministic
`random.Random` seeds and collected *all* `GeneratorError`s in one pass,
rather than discovering them one `pytest` run at a time. This surfaced 21
problems (budget conflicts plus two `quantity_uncatchable_swap` seeds that
didn't have an item with quantity ≥2 to shift from) in a single run,
which were then fixed as a batch: for seeds where the conflict was
resolvable (the two required ranges overlapped), the ceiling was set
inside the overlap; for seeds where it wasn't (the swap price was too far
above item 0's price), the `BUDGET_BREACH` mutation was dropped from that
seed's plan row and the ceiling was raised to comfortably cover the
constraint mutation instead — relying on other seeds to cover the
`BUDGET_BREACH` subpopulation count.

**Lesson.** When a generator seed supports multiple mutation buckets, the
buckets are not independent — they share the seed's numeric fields
(`budget_ceiling`, quantities, prices), and mutations that push those
fields in opposite directions can be jointly unsatisfiable. Before
authoring a rich, multi-bucket seed, check whether its mutations agree on
direction for every shared field, or verify it with a boundary-trial pass
before running the full test suite — not after.

---

## Stage 3 — CLEAN records that weren't: `_clean_extra_uncovered_item` breached `distinct_item_count`

**Cause.** The Stage 2/2.5 `mutate_clean_dispatch` safety check (added
when `_clean_*` distractor mutations were introduced) asserted budget,
prohibited ingredients, the delivery window, and cart arithmetic — but
never re-checked the seed's `AcceptanceCriterion` list itself. It didn't
need to, reasoning went, because none of the CLEAN mutations touch a
criterion's field... except `_clean_extra_uncovered_item`, whose entire
job is to add a new distinct item to the cart "that no criterion covers."
Two seeds (`S02-biryani-office-lunch`, `S18-southindian-rich`) declare a
`distinct_item_count == N` criterion — and adding a new distinct item is
exactly the one thing that criterion is built to detect. Six CLEAN
records silently shipped with a cart that actually failed a `stated`
criterion.

This was invisible for the entirety of Stage 2.5, because nothing in that
stage evaluated criteria against carts — the bug only became observable
once Stage 3's constraint verifier existed and the harness's false-block
proxy metric came back non-zero (6 records, 0.63%) instead of the
expected 0%.

**Fix.** Two changes, not one:

1. `mutate_clean_dispatch` now builds a real `Obligation` from the seed
   (`Seed.to_obligation()`) and runs the actual
   `core.verifiers.constraint.evaluate_constraint_checks` against every
   candidate CLEAN cart, asserting every criterion verdict is `PASS` (not
   just the four hand-picked axes it checked before). This is a
   correctness assertion on the generator, not the tautology the
   "forward from the world" rule warns against — every record here is
   already labelled CLEAN by construction; running the real verifier here
   only catches the generator contradicting its own label, the same way a
   compiler catching a type error isn't "cheating" by knowing the type
   system.
2. `_clean_extra_uncovered_item` itself now skips (falls back to
   identity) on any seed declaring a `distinct_item_count` or
   `item_count` criterion, so the conflict is avoided at the source, not
   just detected after the fact.

**Disclosure.** This bug was found by running the component under test over
the corpus, and the corpus was then regenerated — which bends the rule that
labels never derive from the thing being measured. It is disclosed in
`README.md` rather than buried, with the argument for why it corrects a
mislabelling rather than tuning toward a number: the six carts violated a
criterion the user's own instruction stated, so they were mislabelled from the
moment they were generated regardless of who noticed. The verifier supplied
*detection*, not ground truth — ground truth remained the hand-authored
criteria, written before any verifier existed.

**Residual risk, stated rather than hidden.** The generator's self-check now
imports the verifier, so a bug *inside* `evaluate_constraint_checks` would
produce a corpus consistent with that bug. The coupling is real. It is bounded
by the fact that the verifier can only detect a contradiction between a cart
and its criteria — it can never author either one.

**Lesson.** A generator's own safety checks are only as good as the list
of things they think to check — and that list will always lag behind the
real system's semantics unless the safety check *is* the real system's
logic, not a hand-picked summary of it. Once the actual verifier exists,
route the generator's self-checks through it directly rather than
re-deriving a subset by hand; the subset is exactly where the next bug
like this one will hide. This also means the false-block proxy metric
(expected to be exactly 0% for a deterministic, zero-noise verifier over
a correctly-labelled CLEAN population) is a genuinely useful regression
check going forward, not just a reported number — a nonzero value means
either the verifier or the corpus is wrong, and it's worth checking both.

---

## Stage 3 — recall denominator excluded the known misses

**Cause.** The first Stage 3 harness computed recall over
`structurally_catchable` violations only — the 52 records the corpus
deliberately builds to be invisible to deterministic verification were
dropped from the denominator rather than counted as misses. That produced a
headline of "100% recall (n=272)". The number was arithmetically correct and
methodologically wrong: it measured *recall over the violations we already
knew we could catch*, which is not recall.

The error was easy to make because each step looked locally reasonable — the
uncatchable records genuinely cannot be caught at this stage, so excluding
them "to avoid unfairly penalising the verifier" feels like a fairness
correction. It is not. The user does not care that a miss was predictable;
they are still out the money.

**Fix.** The headline denominator is now all violations (272/324 = 84.0%
excluding `TOTAL_MISDECLARED`; 362/414 = 87.4% including it). The
catchable-only figure survives as a clearly-labelled secondary diagnostic,
"recall over violations within deterministic expressive power", with its
denominator stated in the same breath. Four regression tests were added,
including `test_headline_denominator_includes_every_violation` and
`test_headline_recall_is_below_100_percent`, so the denominator cannot
quietly narrow again.

**Lesson.** When a metric's denominator is chosen by the component being
measured, it is a diagnostic, not a headline. The tell is that improving the
component can *shrink* the denominator: as the semantic verifier lands at
Stage 5, the uncatchable set shrinks and the catchable-only figure would stay
pinned near 100% while real recall moved from 84% upward — a metric that
cannot move is a metric that is not measuring anything.

---

## Stage 4 — `temperature=0` is a 400 on current Claude models

**Cause.** The Stage 4 brief specified a "deterministic seed" for the LLM
client, and the obvious implementation is `temperature=0`. That is now wrong
in a way that fails loudly at runtime rather than degrading quietly:
`temperature`, `top_p` and `top_k` were **removed** on Claude Opus 5 /
Sonnet 5 / Opus 4.6+, and a request carrying any of them is rejected with a
400. There is also no seed parameter on the Messages API. The same class of
staleness applies to `thinking: {budget_tokens: N}`, also removed on Opus 5
in favour of adaptive thinking plus `output_config.effort`.

Writing the "obviously correct" client from memory would have produced code
that 400s on its first real call — and, worse, would have been caught only at
the moment the user spent money running it.

**Fix.** Verified the current API surface against the bundled reference
before writing `core/llm/client.py`, and made the **on-disk response cache**
the determinism mechanism instead — which is what the brief actually needed
("on-disk response cache keyed by prompt hash for reproducible evals").
`CacheOnlyProvider` makes the guarantee enforceable by raising on a cache miss
rather than falling through to a live call. Documented as a named deviation in
`ARCHITECTURE.md`.

**Lesson.** For a fast-moving API, "what I remember the parameter being" is
not a design input. Check the current surface before writing the client, not
after the first 400 — especially when the failure costs the user an API call
to discover. The cache-as-determinism answer also turned out to be strictly
better than the seed would have been: it reproduces the exact bytes the
published metrics were computed from, and survives model deprecation.

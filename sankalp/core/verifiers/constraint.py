"""
Constraint verifier — deterministic predicate evaluation over the closed
field registry. Zero LLM calls.

THREE OUTCOMES PER CRITERION
-----------------------------
PASS, FAIL, ABSTAIN — never a guess. ABSTAIN fires specifically when a
dietary `excludes` criterion on `item.ingredients` is evaluated against a
cart where at least one item has no declared ingredients
(`item.missing_ingredient_count > 0`) — see core/models/fields.py's
ITEM_INGREDIENTS docstring. A verifier that returns FAIL on missing data
would catch some violations by luck; the corpus's `abstain_expected`
records exist specifically to catch that failure mode
(eval/generator.py's `mutate_constraint_abstain`).

`operator=semantic` criteria are skipped entirely, not failed — they are
out of scope for a deterministic verifier by construction (Stage 5's
semantic verifier owns them).

SOURCE-BASED ENFORCEMENT
-------------------------
stated FAIL      -> hard block, composite FAIL.
inferred FAIL    -> confidence reduction only; a single inferred failure
                    (or any number of them) cannot flip the composite to
                    FAIL by itself. Accumulation toward CLARIFY is the
                    aggregator's job (Stage 5), not this verifier's — this
                    verifier reports per-criterion detail and an honest
                    composite; it does not decide CLARIFY.
defaulted FAIL   -> logged only (present in per-criterion detail), never
                    affects the composite verdict.

OBLIGATION-LEVEL CHECKS
------------------------
budget_ceiling, merchant_scope, delivery_window, and Cart.validate_total()
are not AcceptanceCriterion checks — they are direct Obligation/Cart field
checks (see eval/generator.py's `violating_obligation_fields` vs
`violating_criterion_ids` distinction). They have no `source`, so a FAIL
on any of them is always a hard block: "wrong merchant" or "over budget"
isn't advisory in the way an inferred criterion is.

TOTAL_MISDECLARED (Cart.validate_total() failing) is kept as its own field
on ConstraintCheckDetail, evaluated by a separate code path from predicate
evaluation, so per-class recall stays interpretable — see
eval/PRE_REGISTERED.md's note that it's agent misreporting, not an intent
violation.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from core.models.cart import Cart
from core.models.enums import CriterionOperator, CriterionSource, Verdict
from core.models.obligation import AcceptanceCriterion, Obligation
from core.models.verifier import VerifierOutput
from core.verifiers.base import Verifier
import core.models.fields as field_registry

# Stage 3 evidence stand-in — see core/verifiers/base.py's module docstring.
CATALOGUE_EVIDENCE_ID = "ev-catalogue"


@dataclasses.dataclass(frozen=True)
class ConstraintCheckDetail:
    criterion_verdicts:        dict[str, Verdict]   # AcceptanceCriterion.id -> Verdict; semantic criteria absent
    budget_verdict:             Verdict | None        # None iff no budget_ceiling was stated
    merchant_scope_verdict:      Verdict | None        # None iff no merchant_scope was stated
    delivery_verdict:             Verdict | None        # None iff no delivery_window was stated
    total_arithmetic_verdict:      Verdict               # always evaluated
    loss_estimate:                  Decimal | None
    declared_basis:                  list[str]


def _eval_criterion(criterion: AcceptanceCriterion, cart: Cart) -> Verdict:
    if criterion.operator == CriterionOperator.semantic:
        raise ValueError("semantic criteria must be filtered out before _eval_criterion is called")

    value = field_registry.resolve(criterion.field, cart)
    target = criterion.value

    if criterion.field == "item.ingredients" and criterion.operator == CriterionOperator.excludes:
        # A definite violation found among the DECLARED data is conclusive —
        # missing ingredients elsewhere don't un-find a prohibited ingredient
        # we already found. Only fall back to ABSTAIN when the declared data
        # alone can't rule the prohibited ingredient out.
        if target in value:
            return Verdict.FAIL
        missing = field_registry.resolve("item.missing_ingredient_count", cart)
        if missing > 0:
            return Verdict.ABSTAIN
        return Verdict.PASS

    op = criterion.operator

    if op == CriterionOperator.eq:
        ok = value == target
    elif op == CriterionOperator.neq:
        ok = value != target
    elif op == CriterionOperator.lt:
        ok = value < target
    elif op == CriterionOperator.lte:
        ok = value <= target
    elif op == CriterionOperator.gt:
        ok = value > target
    elif op == CriterionOperator.gte:
        ok = value >= target
    elif op == CriterionOperator.contains:
        ok = target in value
    elif op == CriterionOperator.excludes:
        ok = target not in value
    elif op == CriterionOperator.in_set:
        ok = value in target
    else:
        raise ValueError(f"Unhandled operator: {op!r}")

    return Verdict.PASS if ok else Verdict.FAIL


def evaluate_constraint_checks(obligation: Obligation, cart: Cart) -> ConstraintCheckDetail:
    """Pure function: every deterministic check this verifier can run, with full
    per-criterion detail. Used both by the composite verify() below and directly
    by eval/harness.py, which needs per-subpopulation detail a single aggregate
    VerifierOutput cannot express."""
    criterion_verdicts: dict[str, Verdict] = {}
    loss = Decimal("0")
    has_loss = False

    for c in obligation.acceptance_criteria:
        if c.operator == CriterionOperator.semantic:
            continue
        v = _eval_criterion(c, cart)
        criterion_verdicts[c.id] = v
        if v == Verdict.FAIL and c.field == "quantity_sum" and c.operator == CriterionOperator.gte and cart.items:
            shortfall = int(c.value) - field_registry.resolve("quantity_sum", cart)
            if shortfall > 0:
                loss += shortfall * cart.items[0].unit_price
                has_loss = True

    budget_verdict = None
    if obligation.budget_ceiling is not None:
        over = cart.total > obligation.budget_ceiling
        budget_verdict = Verdict.FAIL if over else Verdict.PASS
        if over:
            loss += cart.total - obligation.budget_ceiling
            has_loss = True

    merchant_scope_verdict = None
    scope = obligation.merchant_scope
    if scope.merchant_ids:
        merchant_scope_verdict = Verdict.PASS if cart.merchant.id in scope.merchant_ids else Verdict.FAIL
    elif scope.category:
        merchant_scope_verdict = Verdict.PASS if cart.merchant.category == scope.category else Verdict.FAIL

    delivery_verdict = None
    if obligation.delivery_window is not None:
        on_time = cart.fulfilment_eta <= obligation.delivery_window.latest_by
        delivery_verdict = Verdict.PASS if on_time else Verdict.FAIL

    total_arithmetic_verdict = Verdict.PASS if cart.validate_total() else Verdict.FAIL

    return ConstraintCheckDetail(
        criterion_verdicts=criterion_verdicts,
        budget_verdict=budget_verdict,
        merchant_scope_verdict=merchant_scope_verdict,
        delivery_verdict=delivery_verdict,
        total_arithmetic_verdict=total_arithmetic_verdict,
        loss_estimate=loss if has_loss else None,
        declared_basis=[CATALOGUE_EVIDENCE_ID],
    )


def _composite_verdict(obligation: Obligation, detail: ConstraintCheckDetail) -> tuple[Verdict, float]:
    stated_fail = any(
        detail.criterion_verdicts.get(c.id) == Verdict.FAIL
        for c in obligation.acceptance_criteria
        if c.source == CriterionSource.stated and c.id in detail.criterion_verdicts
    )
    inferred_fail = any(
        detail.criterion_verdicts.get(c.id) == Verdict.FAIL
        for c in obligation.acceptance_criteria
        if c.source == CriterionSource.inferred and c.id in detail.criterion_verdicts
    )
    any_abstain = any(v == Verdict.ABSTAIN for v in detail.criterion_verdicts.values())
    obligation_level_fail = any(
        v == Verdict.FAIL
        for v in (detail.budget_verdict, detail.merchant_scope_verdict,
                   detail.delivery_verdict, detail.total_arithmetic_verdict)
        if v is not None
    )

    if stated_fail or obligation_level_fail:
        return Verdict.FAIL, 1.0
    if any_abstain:
        return Verdict.ABSTAIN, 0.0
    if inferred_fail:
        # Cannot block alone — accumulation toward CLARIFY is the aggregator's job (Stage 5).
        return Verdict.PASS, 0.6
    return Verdict.PASS, 1.0


def _reasoning(detail: ConstraintCheckDetail) -> str:
    fails = [cid for cid, v in detail.criterion_verdicts.items() if v == Verdict.FAIL]
    abstains = [cid for cid, v in detail.criterion_verdicts.items() if v == Verdict.ABSTAIN]
    parts = []
    if fails:
        parts.append(f"criteria FAIL: {fails}")
    if abstains:
        parts.append(f"criteria ABSTAIN (missing ingredient data): {abstains}")
    if detail.budget_verdict == Verdict.FAIL:
        parts.append("budget_ceiling exceeded")
    if detail.merchant_scope_verdict == Verdict.FAIL:
        parts.append("merchant outside declared scope")
    if detail.delivery_verdict == Verdict.FAIL:
        parts.append("fulfilment_eta past delivery_window")
    if detail.total_arithmetic_verdict == Verdict.FAIL:
        parts.append("cart.total does not match line-item arithmetic")
    return "; ".join(parts) if parts else "all deterministic checks passed"


def constraint_verify(obligation: Obligation, cart: Cart) -> VerifierOutput:
    detail = evaluate_constraint_checks(obligation, cart)
    verdict, confidence = _composite_verdict(obligation, detail)
    return VerifierOutput(
        role="constraint",
        verdict=verdict,
        confidence=confidence,
        declared_basis=detail.declared_basis,
        loss_estimate=detail.loss_estimate,
        reasoning=_reasoning(detail),
    )


class ConstraintVerifier(Verifier):
    role = "constraint"

    def verify(self, obligation: Obligation, cart: Cart) -> VerifierOutput:
        return constraint_verify(obligation, cart)

"""
SANKALP MCP server — one tool, `clear_intent`, so an agent can gate a
purchase through the real clearing pipeline before it ever calls a payment
API. This is what makes the project integrable rather than a demo: any
MCP-speaking agent framework can call this tool with the same three inputs
the pipeline has always taken (instruction, cart, evidence) and get back a
typed decision — no HTTP client, no bespoke integration code.

SCOPE, HONESTLY STATED
-----------------------
This tool runs the REAL obligation compiler (an LLM call — needs a working
GROQ_API_KEY or ANTHROPIC_API_KEY, see .env.example), the REAL constraint
verifier, and the REAL semantic verifier for any semantic criteria. It does
NOT run core/verifiers/receipt.py: that verifier cross-checks a cart against
agent/catalogue.py, this project's own synthetic demo merchant database —
meaningless for a third-party agent's real cart, which has no reason to
appear in our demo catalogue. A production deployment integrating a real
merchant catalogue would add its own receipt-equivalent verifier alongside
this tool's constraint check; this server does not fabricate one.

Every verifier's floor exclusion is preserved in the response — the whole
point of returning `excluded` alongside `survivors` is that a caller (or a
human auditing the caller) can see a confident PASS that was structurally
ignored, not just the final verdict.

Run standalone for local testing:
    python -m mcp_server.server
Or wire into an MCP-speaking client via stdio using the same command.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from core.clearing.engine import build_evidence, clear
from core.llm.client import LLMClient, default_client
from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import CriterionOperator, EvidenceClass
from core.obligation.compiler import CompilerError, compile_obligation
from core.settlement.instruction import emit, explain
from core.verifiers.constraint import ConstraintVerifier
from core.verifiers.semantic import SemanticVerifierError, verify_all_semantic

mcp = FastMCP("sankalp")


# ── Typed inputs (this is the schema documented in README.md's MCP section) ─

class CartItemIn(BaseModel):
    name: str = Field(description="Line item name, e.g. 'Chicken Biryani'.")
    quantity: int = Field(ge=1, description="Units of this item.")
    unit_price: str = Field(description="Decimal amount as a string, e.g. '280.00'. Never a float.")
    ingredients: list[str] = Field(default_factory=list, description="Lower-cased ingredient list, if known.")
    category: str | None = Field(default=None, description="Free-form tag, e.g. 'veg', 'non-veg', 'dessert'.")


class CartIn(BaseModel):
    merchant_id: str = Field(description="The agent's own identifier for the merchant.")
    merchant_name: str
    merchant_category: str = Field(default="food_delivery", description="e.g. 'food_delivery', 'grocery'.")
    items: list[CartItemIn] = Field(min_length=1)
    total: str = Field(description="Declared order total, decimal-as-string. Checked against the line-item sum.")
    fulfilment_eta: str = Field(description="ISO-8601 datetime the agent expects to fulfil by, e.g. "
                                              "'2026-09-05T20:30:00+05:30'.")


class EvidenceIn(BaseModel):
    self_report: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional agent self-report — a free-form claim about the agent's own work "
            "(e.g. {'note': 'all items match, no substitutions'}). This is SELF-class "
            "evidence, the weakest tier. If supplied, the semantic verifier is shown BOTH "
            "this and the catalogue evidence together, so its declared basis becomes the "
            "meet of the two (SELF) — a caller cannot get a REC-tier semantic verdict by "
            "attaching a self-report the model might have leaned on. Omit this field "
            "entirely for a semantic verdict that can clear a REC floor on its own."
        ),
    )


# ── Cart parsing (typed input -> real core.models.cart.Cart) ───────────────

def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{field}: {raw!r} is not a valid decimal amount") from exc


def _parse_cart(cart_in: CartIn) -> Cart:
    items = [
        CartItem(
            name=i.name, quantity=i.quantity,
            unit_price=_parse_decimal(i.unit_price, f"items[{idx}].unit_price"),
            ingredients=[ing.lower() for ing in i.ingredients], category=i.category,
        )
        for idx, i in enumerate(cart_in.items)
    ]
    try:
        eta = datetime.fromisoformat(cart_in.fulfilment_eta)
    except ValueError as exc:
        raise ValueError(f"fulfilment_eta: {cart_in.fulfilment_eta!r} is not ISO-8601") from exc
    return Cart(
        items=items,
        merchant=Merchant(id=cart_in.merchant_id, name=cart_in.merchant_name, category=cart_in.merchant_category),
        total=_parse_decimal(cart_in.total, "total"),
        fulfilment_eta=eta,
    )


# ── Response serialisation ──────────────────────────────────────────────────

def _verifier_row(v, evidence_index: dict[str, EvidenceClass], survived: bool) -> dict[str, Any]:
    basis = None
    for eid in v.declared_basis:
        if eid in evidence_index:
            basis = evidence_index[eid].name
            break
    return {
        "role": v.role, "verdict": v.verdict.value, "confidence": v.confidence,
        "declared_basis": v.declared_basis, "basis_class": basis or "SELF",
        "reasoning": v.reasoning, "loss_estimate": str(v.loss_estimate) if v.loss_estimate else None,
        "survived_floor": survived,
    }


# ── The tool ─────────────────────────────────────────────────────────────

@mcp.tool()
def clear_intent(instruction: str, cart: CartIn, evidence: EvidenceIn | None = None,
                  user_id: str = "mcp-caller") -> dict[str, Any]:
    """
    Decide whether `cart` fulfils `instruction`, and produce a settlement
    instruction — the ClearingDecision AND every verifier that was excluded
    by floor enforcement, not just the final verdict.

    Call this BEFORE calling a payment API. A non-EXECUTE action
    (HOLD / CLARIFY / ABORT) means: do not capture payment.

    Requires a working LLM provider (GROQ_API_KEY or ANTHROPIC_API_KEY in
    the environment) — both the obligation compiler and, when the
    instruction contains a subjective requirement, the semantic verifier
    make a live call.
    """
    client: LLMClient = default_client()

    try:
        compilation = compile_obligation(instruction, client=client, user_id=user_id)
    except CompilerError as exc:
        return {"error": "compilation_failed", "detail": str(exc)}

    obligation = compilation.obligation

    try:
        real_cart = _parse_cart(cart)
    except ValueError as exc:
        return {"error": "invalid_cart", "detail": str(exc)}

    envelope = build_evidence(real_cart, obligation.hash,
                               self_report=(evidence.self_report if evidence else None))

    verifiers = [ConstraintVerifier().verify(obligation, real_cart)]
    semantic_criteria = [c for c in obligation.acceptance_criteria if c.operator == CriterionOperator.semantic]
    if semantic_criteria:
        try:
            verifiers += verify_all_semantic(obligation, real_cart, envelope.items, client=client)
        except SemanticVerifierError as exc:
            return {"error": "semantic_verification_failed", "detail": str(exc)}

    outcome = clear(obligation, real_cart, verifiers, envelope, enforce_floor=True)
    instruction_out = emit(outcome, "0" * 64)

    evidence_index = envelope.index
    survivor_ids = {id(v) for v in outcome.aggregate.survivors}
    verifier_rows = [_verifier_row(v, evidence_index, id(v) in survivor_ids) for v in verifiers]

    return {
        "obligation": {
            "id": obligation.id,
            "hash": obligation.hash,
            "raw_instruction": obligation.raw_instruction,
            "acceptance_criteria": [
                {"id": c.id, "field": c.field, "operator": c.operator.value, "value": c.value,
                 "source": c.source.value}
                for c in obligation.acceptance_criteria
            ],
            "budget_ceiling": str(obligation.budget_ceiling) if obligation.budget_ceiling else None,
            "merchant_scope": {"merchant_ids": obligation.merchant_scope.merchant_ids,
                                "category": obligation.merchant_scope.category},
            "admissibility_floor": obligation.admissibility_floor.name,
        },
        "compiler_notes": {
            "ambiguity_flags": [f.code for f in compilation.ambiguity_flags],
            "clarify": compilation.clarify,
            "clarifying_question": compilation.clarifying_question,
            "unresolvable_paths": compilation.unresolvable_paths,
            "dropped_criteria": compilation.dropped_criteria,
        },
        "clearing_decision": {
            "verdict": outcome.aggregate.verdict.value,
            "confidence": outcome.aggregate.confidence,
            "reason_code": outcome.reason_code,
            "basis_class": outcome.aggregate.basis_class.name if outcome.aggregate.basis_class else None,
            "verifiers": verifier_rows,
        },
        "settlement_instruction": {
            "action": instruction_out.action.value,
            "action_explained": explain(instruction_out.action),
            "rail": instruction_out.rail.value,
            "reason_code": instruction_out.reason_code,
            "clearing_decision_hash": instruction_out.clearing_decision_hash,
            "hash": instruction_out.hash,
        },
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

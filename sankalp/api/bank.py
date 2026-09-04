"""
The wallet/order demo — a small, static-money "bank" wired to the REAL
SANKALP clearing pipeline.

WHY THIS EXISTS, SEPARATE FROM api/main.py
-------------------------------------------
api/main.py's scenarios are architecture PROOF: fixed corpus records, chosen
to walk a reviewer through the mechanism. This module is the PRODUCT demo: a
user logs in, builds a real order from the real catalogue, and watches
SANKALP decide whether it clears — with a wallet that actually debits (in
memory, never a real payment rail; see the module docstring in
core/settlement/instruction.py for why no rail is called).

TWO SEPARATE SAFETY LAYERS, NEVER CONFLATED
--------------------------------------------
1. SANKALP's clearing decision (EXECUTE / HOLD / CLARIFY / ABORT) — does
   this order fulfil what the user actually asked for? This is the real,
   unmodified core/clearing pipeline.
2. The wallet's funds check (sufficient balance / within daily limit) — can
   this user actually afford it? This is a plain ledger check, unrelated to
   fulfilment-clearing. A production system keeps these layers distinct
   (an authorized, well-fulfilled order can still bounce for insufficient
   funds), and so does this demo: the API response always reports them
   separately, and a debit only happens when BOTH pass.

WHY THIS FILE BUILDS PER-CRITERION VerifierOutputs INSTEAD OF CALLING
ConstraintVerifier().verify() DIRECTLY
------------------------------------------------------------------------
ConstraintVerifier's own composite (core/verifiers/constraint.py) folds every
criterion into ONE VerifierOutput, which means an `inferred`-source
criterion's FAIL never survives as a FAIL to the aggregator — it is already
resolved to a PASS at reduced confidence before core/clearing/aggregator.py
ever sees it (that verifier's job is to be a safe default for the common
case, not to expose every knob). Reaching the aggregator's inferred-weight
accumulation path (core/clearing/aggregator.py: a `stated`-source FAIL hard
blocks, `inferred` FAILs merely accumulate and only route to CLARIFY once
their combined weight passes INFERRED_CLARIFY_THRESHOLD) requires handing it
one VerifierOutput per criterion — exactly the pattern
tests/unit/test_stage5.py::TestSourceEnforcement uses to exercise this path.
`_criterion_outputs` below does that: it calls the REAL, unmodified
`evaluate_constraint_checks` (the same pure function ConstraintVerifier uses
internally) and reshapes its per-criterion detail into one VerifierOutput per
criterion, each declaring BOTH the criterion's own id and the catalogue
evidence id in `declared_basis` — the same dual-id pattern that test uses, so
`aggregate()` can resolve both the criterion's source (stated vs inferred)
and a valid REC basis class for it. No core/ file is modified to make this
work; this is exercising the aggregator exactly as it is designed to be
called by an integration that wants per-criterion granularity.

This is also why the order form has two constraint tiers:
  - "Hard requirements" (budget, excluded ingredients, delivery deadline) —
    each becomes a `stated` criterion. Any single violation blocks (ABORT).
  - "Soft preferences" (vegetarian only, no dessert) — each becomes an
    `inferred` criterion. One violation is noted but does not block; two
    violated at once pushes the accumulated weight past the threshold and
    the order genuinely routes to CLARIFY — a real decision from real code,
    not a scripted one.
"""

from __future__ import annotations

import dataclasses
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from agent import catalogue
from core.clearing.engine import EvidenceEnvelope, build_evidence, clear
from core.models.cart import Cart, CartItem, Merchant
from core.models.enums import CriterionOperator, CriterionSource, EvidenceClass, Verdict
from core.models.evidence import EvidenceItem, bind_evidence
from core.models.obligation import AcceptanceCriterion, DeliveryWindow, MerchantScope, Obligation
from core.models.verifier import VerifierOutput
from core.obligation.binder import bind
from core.settlement.instruction import emit, explain
from core.verifiers.constraint import CATALOGUE_EVIDENCE_ID, evaluate_constraint_checks
from core.verifiers.receipt import ReceiptVerifier

router = APIRouter(prefix="/api/bank", tags=["bank"])


# ── Demo users, in-memory only. Never persisted, never real money. ─────────

@dataclasses.dataclass
class DemoUser:
    id:            str
    name:          str
    password:      str
    avatar:        str
    balance:       Decimal
    daily_limit:   Decimal
    spent_today:   Decimal = Decimal("0")


USERS: dict[str, DemoUser] = {
    u.id: u for u in [
        DemoUser("u-aarav", "Aarav Sharma", "sankalp123", "🧑‍💼", Decimal("5000"), Decimal("3000")),
        DemoUser("u-diya",  "Diya Verma",   "sankalp123", "👩‍🎓", Decimal("8000"), Decimal("5000")),
        DemoUser("u-kabir", "Kabir Khan",   "sankalp123", "🧑‍🍳", Decimal("2000"), Decimal("1500")),
        DemoUser("u-meera", "Meera Iyer",   "sankalp123", "👩‍💻", Decimal("10000"), Decimal("6000")),
        DemoUser("u-rohan", "Rohan Gupta",  "sankalp123", "🧑‍🎤", Decimal("4000"), Decimal("2500")),
    ]
}

SESSIONS: dict[str, str] = {}          # token -> user_id
ORDERS: dict[str, list[dict[str, Any]]] = {uid: [] for uid in USERS}  # user_id -> orders, newest first


def _require_user(authorization: str | None) -> DemoUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token. Log in first.")
    token = authorization.removeprefix("Bearer ").strip()
    user_id = SESSIONS.get(token)
    if user_id is None:
        raise HTTPException(401, "Session expired or invalid. Log in again.")
    return USERS[user_id]


def _user_public(user: DemoUser) -> dict[str, Any]:
    return {
        "id": user.id, "name": user.name, "avatar": user.avatar,
        "balance": str(user.balance), "daily_limit": str(user.daily_limit),
        "spent_today": str(user.spent_today),
        "available_today": str(user.daily_limit - user.spent_today),
    }


# ── Auth ─────────────────────────────────────────────────────────────────

@router.get("/users")
def list_users() -> list[dict[str, Any]]:
    """Public roster for the login picker. Password shown deliberately —
    this is a demo, not a real credential store."""
    return [
        {"id": u.id, "name": u.name, "avatar": u.avatar, "demo_password": u.password}
        for u in USERS.values()
    ]


@router.post("/login")
def login(body: dict[str, str]) -> dict[str, Any]:
    user_id, password = body.get("user_id"), body.get("password")
    user = USERS.get(user_id or "")
    if user is None or password != user.password:
        raise HTTPException(401, "Unknown user or wrong password.")
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = user.id
    return {"token": token, "user": _user_public(user)}


@router.get("/session")
def session(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _user_public(_require_user(authorization))


# ── Catalogue ────────────────────────────────────────────────────────────

@router.get("/catalogue")
def get_catalogue() -> list[dict[str, Any]]:
    result = []
    for mid, m in catalogue.MERCHANTS.items():
        result.append({
            "id": mid, "name": m.name, "category": m.category,
            "items": [
                {"name": it.name, "unit_price": str(it.unit_price),
                 "ingredients": list(it.ingredients), "category": it.category}
                for it in m.items.values()
            ],
        })
    return result


# ── Order construction ──────────────────────────────────────────────────

def _build_cart(merchant_id: str, requested: list[dict[str, Any]]) -> Cart:
    merchant = catalogue.MERCHANTS.get(merchant_id)
    if merchant is None:
        raise HTTPException(404, f"Unknown merchant {merchant_id!r}")
    items: list[CartItem] = []
    for row in requested:
        name, qty = str(row.get("name", "")), int(row.get("quantity", 0))
        cat_item = merchant.items.get(name.lower())
        if cat_item is None:
            raise HTTPException(400, f"{name!r} is not sold by {merchant.name}")
        if qty < 1:
            raise HTTPException(400, f"Quantity for {name!r} must be at least 1")
        items.append(CartItem(name=cat_item.name, quantity=qty, unit_price=cat_item.unit_price,
                                ingredients=list(cat_item.ingredients), category=cat_item.category))
    if not items:
        raise HTTPException(400, "An order needs at least one item.")
    total = sum((i.unit_price * i.quantity for i in items), Decimal("0"))
    return Cart(items=items, merchant=Merchant(id=merchant.id, name=merchant.name, category=merchant.category),
                total=total, fulfilment_eta=datetime.now(timezone.utc) + timedelta(minutes=40))


def _build_obligation(user: DemoUser, cart: Cart, body: dict[str, Any]) -> Obligation:
    criteria: list[AcceptanceCriterion] = []

    for ingredient in body.get("excluded_ingredients") or []:
        criteria.append(AcceptanceCriterion(
            field="item.ingredients", operator=CriterionOperator.excludes, value=str(ingredient).lower(),
            source=CriterionSource.stated, confidence=1.0,
        ))
    if body.get("veg_only"):
        criteria.append(AcceptanceCriterion(
            field="item.categories", operator=CriterionOperator.excludes, value="non-veg",
            source=CriterionSource.inferred, confidence=1.0,
        ))
    if body.get("no_dessert"):
        criteria.append(AcceptanceCriterion(
            field="item.categories", operator=CriterionOperator.excludes, value="dessert",
            source=CriterionSource.inferred, confidence=1.0,
        ))

    budget_raw = body.get("budget_ceiling")
    budget_ceiling = Decimal(str(budget_raw)) if budget_raw not in (None, "") else None

    delivery_window = None
    minutes = body.get("delivery_minutes")
    if minutes not in (None, ""):
        delivery_window = DeliveryWindow(latest_by=datetime.now(timezone.utc) + timedelta(minutes=int(minutes)))

    instruction_bits = [f"Order from {cart.merchant.name}: " +
                          ", ".join(f"{i.quantity}x {i.name}" for i in cart.items)]
    if budget_ceiling is not None:
        instruction_bits.append(f"budget under Rs.{budget_ceiling}")
    if body.get("excluded_ingredients"):
        instruction_bits.append("exclude: " + ", ".join(body["excluded_ingredients"]))

    unbound = Obligation(
        raw_instruction=". ".join(instruction_bits),
        user_id=user.id,
        acceptance_criteria=criteria,
        budget_ceiling=budget_ceiling,
        merchant_scope=MerchantScope(merchant_ids=[cart.merchant.id]),
        delivery_window=delivery_window,
        admissibility_floor=EvidenceClass.REC,
    )
    return bind(unbound)


def _extend_envelope_for_criteria(envelope: EvidenceEnvelope, obligation: Obligation) -> EvidenceEnvelope:
    """
    Register each non-semantic criterion's own id as a REC-class evidence
    item, mirroring tests/unit/test_stage5.py::TestSourceEnforcement's
    `index[criterion_id] = EvidenceClass.REC` pattern.

    Without this, a per-criterion VerifierOutput's declared_basis=[c.id, ...]
    would resolve c.id as an UNKNOWN evidence id (core/admissibility/floor.py's
    documented "unknown evidence -> SELF" rule), dragging that verifier's
    effective basis class down to SELF via the meet — and a REC floor would
    then silently exclude it, exactly the evidence-ID bug already caught once
    in Stage 5 (see FAILURES.md), reintroduced here if skipped. Each item's
    evidence is the same catalogue data the deterministic check actually
    used, so REC is the honest class to declare — this is not a workaround,
    it is registering evidence that already exists but had no id of its own.
    """
    extra = [
        bind_evidence(EvidenceItem(
            id=c.id, payload={"checked_field": c.field}, emitter="constraint-per-criterion",
            original_class=EvidenceClass.REC, admissibility_class=EvidenceClass.REC,
            obligation_hash=obligation.hash,
        ))
        for c in obligation.acceptance_criteria
        if c.operator != CriterionOperator.semantic
    ]
    return EvidenceEnvelope(items=[*envelope.items, *extra])


def _friendly_criterion(c: AcceptanceCriterion, verdict: Verdict) -> tuple[str, str]:
    """(plain-language title, one-sentence result) for a single criterion —
    everything a non-technical user needs, no field paths or operator names."""
    if c.field == "item.ingredients" and c.operator == CriterionOperator.excludes:
        ok = verdict == Verdict.PASS
        return (f"No {c.value}",
                f"Confirmed — no {c.value} in your order." if ok else f"Found {c.value} in your order.")
    if c.field == "item.categories" and c.operator == CriterionOperator.excludes:
        pretty = {"non-veg": "vegetarian item", "dessert": "dessert item"}.get(str(c.value), str(c.value))
        title = "Vegetarian only" if c.value == "non-veg" else f"No {pretty}s"
        ok = verdict == Verdict.PASS
        tag = " (your preference, not a hard rule)"
        return (title, f"All items are fine{tag}." if ok else f"This order has a {pretty}{tag}.")
    return (f"{c.field} {c.operator.value} {c.value}", f"{verdict.value.title()}.")


_OBLIGATION_LEVEL_LABELS = {
    "budget_ceiling": ("Within your budget", "Total is within your budget.", "Total goes over your budget."),
    "merchant_scope": ("Right restaurant", "This is the restaurant you chose.", "This isn't the restaurant you chose."),
    "delivery_window": ("Arrives on time", "Will arrive within your requested time.", "Won't arrive in time."),
    "total_arithmetic": ("Bill adds up", "The prices add up correctly.", "Something doesn't add up in the bill."),
}


def _criterion_outputs(obligation: Obligation, cart: Cart, friendly: dict[int, tuple[str, str]]) -> list[VerifierOutput]:
    """Real per-criterion + obligation-level VerifierOutputs. See module
    docstring: 'WHY THIS FILE BUILDS PER-CRITERION VerifierOutputs'. Also
    fills `friendly[id(output)] = (title, plain_sentence)` for the UI —
    every technical (field, operator, value) is translated to English here,
    once, instead of asking the frontend to parse `reasoning` strings."""
    detail = evaluate_constraint_checks(obligation, cart)
    outputs: list[VerifierOutput] = []

    for c in obligation.acceptance_criteria:
        verdict = detail.criterion_verdicts.get(c.id)
        if verdict is None:   # semantic criteria are skipped by evaluate_constraint_checks
            continue
        out = VerifierOutput(
            role="constraint", verdict=verdict, confidence=1.0,
            declared_basis=[c.id, CATALOGUE_EVIDENCE_ID],
            reasoning=f"{c.field} {c.operator.value} {c.value!r} ({c.source.value}): {verdict.value}",
        )
        friendly[id(out)] = _friendly_criterion(c, verdict)
        outputs.append(out)

    obligation_level = [
        ("budget_ceiling", detail.budget_verdict, "budget_ceiling exceeded"),
        ("merchant_scope", detail.merchant_scope_verdict, "merchant outside declared scope"),
        ("delivery_window", detail.delivery_verdict, "fulfilment_eta past delivery_window"),
        ("total_arithmetic", detail.total_arithmetic_verdict, "cart.total does not match line-item arithmetic"),
    ]
    for name, verdict, message in obligation_level:
        if verdict is None:
            continue
        out = VerifierOutput(
            role="constraint", verdict=verdict, confidence=1.0,
            declared_basis=[CATALOGUE_EVIDENCE_ID],
            reasoning=message if verdict == Verdict.FAIL else f"{name}: ok",
        )
        title, ok_text, fail_text = _OBLIGATION_LEVEL_LABELS[name]
        friendly[id(out)] = (title, ok_text if verdict == Verdict.PASS else fail_text)
        outputs.append(out)

    return outputs


def _verdict_row(v: VerifierOutput, evidence_index: dict[str, EvidenceClass], survived: bool,
                  friendly: dict[int, tuple[str, str]]) -> dict[str, Any]:
    basis = None
    for eid in v.declared_basis:
        if eid in evidence_index:
            basis = evidence_index[eid].name
            break
    basis = basis or "SELF"
    title, plain = friendly.get(id(v), (v.role.title(), v.reasoning))
    return {
        "role": v.role, "verdict": v.verdict.value, "confidence": v.confidence,
        "basis_class": basis, "survived": survived, "reasoning": v.reasoning,
        "title": title, "plain_text": plain,
        "trust_note": ("Checked against the restaurant's own real menu and order data."
                        if basis == "REC" else
                        "Only the agent's own claim — not checked against anything independent."),
        "counted_note": "Counted toward the final decision." if survived else "Not trustworthy enough to count — ignored.",
    }


# ── Orders ───────────────────────────────────────────────────────────────

@router.post("/orders")
def place_order(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = _require_user(authorization)

    cart = _build_cart(body.get("merchant_id", ""), body.get("items") or [])
    obligation = _build_obligation(user, cart, body)
    envelope = _extend_envelope_for_criteria(build_evidence(cart, obligation.hash), obligation)

    friendly: dict[int, tuple[str, str]] = {}
    receipt_out = ReceiptVerifier().verify(obligation, cart)
    friendly[id(receipt_out)] = (
        "Order matches the menu",
        "Items and prices match the restaurant's own listing." if receipt_out.verdict == Verdict.PASS
        else receipt_out.reasoning,
    )
    verifiers = _criterion_outputs(obligation, cart, friendly) + [receipt_out]

    outcome = clear(obligation, cart, verifiers, envelope, enforce_floor=True)
    instruction = emit(outcome, "0" * 64)
    evidence_index = envelope.index
    survivor_ids = {id(v) for v in outcome.aggregate.survivors}
    verifier_rows = [_verdict_row(v, evidence_index, id(v) in survivor_ids, friendly) for v in verifiers]

    action = outcome.action.value
    override = bool(body.get("confirm_override")) and action == "CLARIFY"
    effective_action = "EXECUTE" if override else action

    wallet_note = None
    debited = Decimal("0")
    if effective_action == "EXECUTE":
        if cart.total > user.balance:
            effective_action, wallet_note = "BLOCKED_BY_WALLET", "Insufficient wallet balance."
        elif user.spent_today + cart.total > user.daily_limit:
            effective_action, wallet_note = "BLOCKED_BY_WALLET", "Would exceed today's spending limit."
        else:
            user.balance -= cart.total
            user.spent_today += cart.total
            debited = cart.total

    customer_message = {
        "EXECUTE": "Order placed — payment completed.",
        "ABORT": "We stopped this order — it didn't meet one of your must-have requirements.",
        "CLARIFY": "This order has a couple of things that don't fully match your preferences — please confirm.",
        "HOLD": "Your order is on hold for review — no payment has been taken.",
        "BLOCKED_BY_WALLET": "This order is fine, but you don't have enough balance to pay for it.",
    }[effective_action]

    order_id = str(uuid.uuid4())
    record = {
        "order_id": order_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "merchant": cart.merchant.name,
        "items": [{"name": i.name, "quantity": i.quantity} for i in cart.items],
        "total": str(cart.total),
        "sankalp_action": action,
        "effective_action": effective_action,
        "overridden_by_user": override,
        "reason_code": outcome.reason_code,
        "wallet_note": wallet_note,
        "debited": str(debited),
        "settlement_hash": instruction.hash,
        "verifiers": verifier_rows,
        "explanation": explain(outcome.action),
        "customer_message": customer_message,
    }
    ORDERS[user.id].insert(0, record)

    return {
        "order": record,
        "obligation": {
            "raw_instruction": obligation.raw_instruction,
            "criteria": [{"field": c.field, "operator": c.operator.value, "value": c.value,
                           "source": c.source.value} for c in obligation.acceptance_criteria],
            "budget_ceiling": str(obligation.budget_ceiling) if obligation.budget_ceiling else None,
            "admissibility_floor": obligation.admissibility_floor.name,
        },
        "user": _user_public(user),
    }


@router.get("/orders")
def order_history(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    user = _require_user(authorization)
    return ORDERS[user.id]

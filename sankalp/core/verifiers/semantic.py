"""
Semantic verifier — the only LLM in the verification path.

Handles ONLY `operator=semantic` criteria. Those are, by construction, the ones
the closed field registry cannot express ("nothing too spicy", "must be fresh
not frozen"), so they are exactly the violations deterministic verification
provably cannot reach.

TWO PROPERTIES THAT ARE NOT CONVENTIONS
----------------------------------------
1. `loss_estimate` is ALWAYS None.
   Not "usually", not "when unsure". This verifier cannot compute a rupee loss
   without inventing a number, so it never returns one. Enforced by a test that
   asserts None across every verdict path, not by a code comment.

2. The MODEL DOES NOT DECLARE ITS OWN BASIS.
   `declared_basis` is set by this module from the evidence it actually placed
   in the prompt — the model is never asked, and therefore cannot overstate.
   This matters because floor enforcement is only as good as basis honesty: a
   verifier that claimed REC while reading a SELF-class self-report would slip
   past the floor and the whole architecture would be decoration. Making the
   declaration a property of the code rather than of the model's cooperation is
   what turns "the verifier declares honestly" from a hope into a guarantee.

   A test asserts declared_basis equals exactly the ids passed in.

The consequence, live: when the evidence is an agent self-report (SELF class)
and the obligation floor is REC, this verifier's PASS — however confident — is
partitioned out by apply_floor before any aggregation runs. It is not outvoted.
It is absent.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal   # noqa: F401 - imported for the None-loss assertion's meaning

from core.guards.output_validator import find_digit_spans, find_urls
from core.llm.client import DEFAULT_MODEL, LLMClient, LLMRequest
from core.llm.prompts import load_prompt
from core.models.cart import Cart
from core.models.enums import CriterionOperator, EvidenceClass, Verdict
from core.models.evidence import EvidenceItem
from core.models.obligation import AcceptanceCriterion, Obligation
from core.models.verifier import VerifierOutput

PROMPT_FILE = "semantic_verifier_v1.md"
PROMPT_VERSION = "semantic_verifier/v1"

SYSTEM_PROMPT = (
    "You judge whether an order satisfies one subjective requirement. You return only "
    "JSON. You never write a number, price, or URL other than a confidence value."
)


class SemanticVerifierError(Exception):
    """The semantic verifier could not produce a usable verdict."""


def semantic_criteria(obligation: Obligation) -> list[AcceptanceCriterion]:
    return [c for c in obligation.acceptance_criteria if c.operator == CriterionOperator.semantic]


def _render_evidence(evidence: list[EvidenceItem]) -> str:
    if not evidence:
        return "(no evidence supplied)"
    lines = []
    for item in evidence:
        origin = {
            EvidenceClass.SELF: "the agent's own report of its work (a CLAIM, not an observation)",
            EvidenceClass.SIGN: "a signed statement by the acting agent",
            EvidenceClass.WIT:  "a third party present to the action",
            EvidenceClass.REC:  "merchant catalogue data (what was actually ordered)",
            EvidenceClass.ATT:  "an attestation from a hardened runner",
        }[item.admissibility_class]
        lines.append(f"- Source: {origin}\n  Content: {json.dumps(item.payload, sort_keys=True)}")
    return "\n".join(lines)


def _render_criterion(criterion: AcceptanceCriterion) -> str:
    return f'The user required: "{criterion.value}" (recorded against {criterion.field}).'


def _sanitise_reasoning(text: str) -> str:
    """
    Strip authored values from free text rather than failing the verification.

    VerifierOutput.reasoning is explicitly non-load-bearing — it is preserved
    for the audit trail and must not affect the verdict — so a model that slips
    a digit into its prose should not invalidate an otherwise sound judgement.
    A URL is different in kind and is removed entirely.
    """
    if find_urls(text):
        return "[reasoning withheld: contained a URL]"
    if find_digit_spans(text):
        return re.sub(r"\d[\d,._]*", "<number-withheld>", text)
    return text.strip()


def _parse_response(text: str) -> tuple[Verdict, float, str]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise SemanticVerifierError(f"No JSON object in semantic verifier response: {text[:300]!r}")
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SemanticVerifierError(f"Malformed JSON from semantic verifier: {exc}") from exc

    raw_verdict = str(payload.get("verdict", "")).strip().upper()
    if raw_verdict not in ("PASS", "FAIL", "ABSTAIN"):
        # An unparseable verdict is not a licence to guess — the safe reading of
        # "I don't know what it said" is "it didn't tell me", which is ABSTAIN.
        return Verdict.ABSTAIN, 0.0, "[unparseable verdict; treated as ABSTAIN]"

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = _sanitise_reasoning(str(payload.get("reasoning", "") or ""))
    return Verdict(raw_verdict), confidence, reasoning


def verify_semantic_criterion(
    criterion: AcceptanceCriterion,
    cart: Cart,
    evidence: list[EvidenceItem],
    *,
    client: LLMClient,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    effort: str = "medium",
    max_tokens: int = 1024,
) -> VerifierOutput:
    """
    Judge one semantic criterion.

    `declared_basis` is the ids of the evidence THIS FUNCTION put in the prompt.
    The model never sees or supplies it.
    """
    if criterion.operator != CriterionOperator.semantic:
        raise SemanticVerifierError(
            f"verify_semantic_criterion called with operator={criterion.operator!r}; "
            f"only `semantic` criteria belong to this verifier."
        )

    prompt = (
        load_prompt(PROMPT_FILE)
        .replace("{CRITERION}", _render_criterion(criterion))
        .replace("{EVIDENCE}", _render_evidence(evidence))
    )
    response = client.complete(LLMRequest(
        system=SYSTEM_PROMPT, prompt=prompt, max_tokens=max_tokens,
        temperature=temperature, effort=effort,
        prompt_version=PROMPT_VERSION, model=model,
    ))
    verdict, confidence, reasoning = _parse_response(response.text)

    return VerifierOutput(
        role="semantic",
        verdict=verdict,
        confidence=confidence,
        # Set by code from what was actually consulted. The model cannot inflate it.
        declared_basis=[item.id for item in evidence],
        # UNCONDITIONALLY None. See module docstring.
        loss_estimate=None,
        reasoning=reasoning,
    )


def verify_all_semantic(
    obligation: Obligation,
    cart: Cart,
    evidence: list[EvidenceItem],
    *,
    client: LLMClient,
    **kwargs,
) -> list[VerifierOutput]:
    """One VerifierOutput per semantic criterion. Empty list when there are none —
    this verifier never speaks about criteria that are not its own."""
    return [
        verify_semantic_criterion(criterion, cart, evidence, client=client, **kwargs)
        for criterion in semantic_criteria(obligation)
    ]

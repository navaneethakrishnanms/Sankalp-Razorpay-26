"""
Verifier ABC.

Every verifier returns a single VerifierOutput with a mandatory
declared_basis (core/models/verifier.py) — the evidence item IDs it
actually consulted, declared honestly.  Floor enforcement
(core/admissibility/floor.py) computes each verifier's effective
admissibility class from this declaration; a verifier that omits it
defaults to SELF class, which is the safe failure mode (see
ARCHITECTURE.md's note on this deviation from the original spec).

STAGE 3 EVIDENCE STAND-IN
-------------------------
The full evidence envelope (core/evidence/envelope.py) is not built yet —
that is a later stage per the module layout. Both verifiers built this
stage (constraint, receipt) read directly from the catalogue-backed Cart,
so they declare a single synthetic evidence id, CATALOGUE_EVIDENCE_ID
(defined in core/verifiers/constraint.py), at REC class. This is a
deliberate, minimal stand-in — not a shortcut around floor enforcement,
since REC already meets the default REC floor, and it will be replaced by
real per-item evidence when the envelope is built.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.cart import Cart
from core.models.obligation import Obligation
from core.models.verifier import VerifierOutput


class Verifier(ABC):
    role: str

    @abstractmethod
    def verify(self, obligation: Obligation, cart: Cart) -> VerifierOutput:
        """Return a single composite VerifierOutput for this obligation/cart pair."""

"""
Verifier selection. Fixed set this stage — exposure-band-driven selection
(which verifiers run for LOW / MODERATE / ELEVATED) is Stage 6.
"""

from __future__ import annotations

from core.verifiers.base import Verifier
from core.verifiers.constraint import ConstraintVerifier
from core.verifiers.receipt import ReceiptVerifier

STAGE_3_VERIFIERS: tuple[Verifier, ...] = (ConstraintVerifier(), ReceiptVerifier())


def get_verifiers() -> tuple[Verifier, ...]:
    return STAGE_3_VERIFIERS

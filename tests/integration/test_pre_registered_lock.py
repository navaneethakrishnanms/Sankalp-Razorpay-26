"""
eval/PRE_REGISTERED.md is hash-locked.

The lock is the whole value of pre-registration: metrics declared before
results exist cannot be quietly reworded once the results are in. Until this
test existed the README claimed the file was "hash-locked in CI" while nothing
actually checked it — the claim was true in intent and false in fact.

TO CHANGE PRE_REGISTERED.md LEGITIMATELY
-----------------------------------------
Don't. The file's own header says a wrong metric definition is a finding for
FAILURES.md, not a reason to edit. If a genuinely necessary change is agreed,
append a DATED ADDENDUM at the bottom (never edit existing text), update the
hash below in the SAME commit, and record why in FAILURES.md. Updating the hash
without a recorded reason is indistinguishable from tampering.

Model provenance deliberately does NOT live in that file — it changes once per
run, and a lock that is routinely broken protects nothing. See
eval/MODEL_PROVENANCE.md.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_REGISTERED = REPO_ROOT / "eval" / "PRE_REGISTERED.md"

# Recorded on first lock. Update only per the procedure in the module docstring.
EXPECTED_SHA256: str | None = None


def _digest() -> str:
    raw = PRE_REGISTERED.read_bytes()
    # Normalise line endings so a Windows checkout and a Linux CI runner agree.
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


class TestPreRegisteredLock:
    def test_file_exists(self):
        assert PRE_REGISTERED.exists()

    def test_hash_matches_the_recorded_lock(self):
        if EXPECTED_SHA256 is None:
            pytest.skip(
                "PRE_REGISTERED.md hash not yet recorded. Run:\n"
                "  python -m tests.integration.test_pre_registered_lock\n"
                "and paste the printed digest into EXPECTED_SHA256."
            )
        assert _digest() == EXPECTED_SHA256, (
            "eval/PRE_REGISTERED.md changed. Pre-registered metrics must not be "
            "edited after results exist. If the change is legitimate, follow the "
            "procedure in this module's docstring — append a dated addendum, update "
            "EXPECTED_SHA256 in the same commit, and record why in FAILURES.md."
        )

    def test_headline_operating_point_is_still_2_percent(self):
        """Spot-check the single most gameable declaration, independent of the
        hash, so a hash update alone cannot quietly move the goalposts."""
        text = PRE_REGISTERED.read_text(encoding="utf-8")
        assert "2% false-block rate" in text

    def test_source_labelling_metric_is_still_declared(self):
        text = PRE_REGISTERED.read_text(encoding="utf-8")
        assert "stated" in text and "inferred" in text


if __name__ == "__main__":   # pragma: no cover
    print(_digest())

"""
Obligation binder — freeze, validate against the closed field registry, hash.

THE HARD BIND FAILURE
---------------------
core/models/fields.py exists because a compiler will invent field paths the
constraint verifier cannot resolve, and an unresolvable path that is quietly
skipped produces an ABSTAIN that reads to a caller as "no violation found."
That is the single most dangerous silent failure in the system: the user's
requirement disappears and the payment clears.

bind() is where that is caught. Every criterion's `field` is looked up in the
registry, and an unknown path raises BindError. It is never downgraded to a
warning, never logged-and-skipped, and the criterion is never dropped so the
rest can proceed. A compilation that cannot be fully bound is not a
compilation.
"""

from __future__ import annotations

from core.models.fields import get_field_spec
from core.models.obligation import Obligation, _compute_obligation_hash


class BindError(Exception):
    """An obligation could not be bound. Always fatal to the compilation."""


def validate_fields(obligation: Obligation) -> None:
    """Raise BindError if any criterion names a path outside the registry."""
    unresolvable: list[tuple[str, str]] = []
    for criterion in obligation.acceptance_criteria:
        try:
            get_field_spec(criterion.field)
        except KeyError:
            unresolvable.append((criterion.id, criterion.field))

    if unresolvable:
        raise BindError(
            "Obligation contains criteria whose field paths are not in the SANKALP "
            "field registry (core/models/fields.py). This is a hard failure: an "
            "unresolvable path must never become a silent ABSTAIN. Offending "
            f"(criterion_id, field) pairs: {unresolvable!r}"
        )


def bind(obligation: Obligation) -> Obligation:
    """
    Validate and stamp the hash. Returns a new, frozen, bound Obligation.

    Raises BindError if the obligation is already bound (a double-bind means
    a caller lost track of state) or if any field path is unresolvable.
    """
    if obligation.hash != "":
        raise BindError(
            f"Obligation {obligation.id!r} is already bound (hash={obligation.hash!r}). "
            "Bind exactly once, at the end of compilation."
        )
    validate_fields(obligation)
    return obligation.model_copy(update={"hash": _compute_obligation_hash(obligation)})

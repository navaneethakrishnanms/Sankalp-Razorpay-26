"""
EvidenceItem — an immutable, hash-anchored piece of evidence with a
provenance chain that degrades its admissibility class.

IMMUTABILITY
------------
EvidenceItem is frozen.  Extending evidence (when it travels through a
channel) creates a new EvidenceItem; the original is unchanged.  This is
enforced by model_config frozen=True and tested in test_models.py.

ADMISSIBILITY_CLASS COMPUTATION
--------------------------------
admissibility_class is a regular stored field, not a computed_field, so
it appears in the hash computation and is preserved through JSON round-trips.
Its value is validated on every construction: if the declared value does
not equal meet(original_class, *hop channel classes), construction fails.

Users must always pass admissibility_class explicitly.  The factory
functions make_evidence_item() and extend_evidence() compute it correctly;
callers that bypass these helpers must compute it themselves.

This design makes it impossible to accidentally store an inflated class —
the validator rejects it immediately, rather than letting it propagate to
floor enforcement where the error would be harder to trace.

PROVENANCE CHAIN
----------------
provenance_chain records each hop the evidence took after creation.  Each
ProvenanceHop stores:
  - channel_class: the admissibility class of the channel used at that hop
  - predecessor_hash: the SHA-256 of the EvidenceItem before this hop

The predecessor_hash creates a verifiable linked structure.  A chain
verification script (scripts/verify_chain.py) can walk the hops and
confirm that no intermediate item was tampered with.

HASH LIFECYCLE
--------------
Same pattern as Obligation: hash="" is the unbound sentinel.  bind_evidence()
computes the hash and returns a new bound item.  extend_evidence() requires
a bound item (it uses its hash as the predecessor_hash in the new hop),
extends the chain, and returns a new bound item.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4
from typing import Any

from pydantic import BaseModel, Field, model_validator

from core.models.enums import EvidenceClass
from core.admissibility.lattice import meet


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


# ── ProvenanceHop ──────────────────────────────────────────────────────────

class ProvenanceHop(BaseModel):
    """
    One step in an evidence item's provenance chain.

    channel_class      : Admissibility class of the channel used at this hop.
    predecessor_hash   : SHA-256 of the EvidenceItem immediately before this
                         hop.  Enables chain verification in verify_chain.py.
    """
    model_config = {"frozen": True}

    channel_class:    EvidenceClass
    predecessor_hash: str


# ── EvidenceItem ───────────────────────────────────────────────────────────

class EvidenceItem(BaseModel):
    """
    A piece of evidence with a declared admissibility class and a full
    provenance chain.

    The effective admissibility class is always meet(original_class, *channel
    classes across the chain).  The validator enforces this on construction;
    a mismatch raises ValidationError immediately.
    """
    model_config = {"frozen": True}

    id:                 str = Field(default_factory=lambda: str(uuid4()))
    payload:            dict[str, Any]
    emitter:            str          # identifier of the entity that produced this item
    original_class:     EvidenceClass
    provenance_chain:   list[ProvenanceHop] = Field(default_factory=list)
    # admissibility_class MUST equal meet(original_class + channel classes).
    # No default — callers must compute it.  Use make_evidence_item() if unsure.
    admissibility_class: EvidenceClass
    obligation_hash:    str
    hash:               str = Field(default="")   # "" = unbound sentinel

    @model_validator(mode="after")
    def _validate_class_and_hash(self) -> "EvidenceItem":
        # 1. Verify admissibility_class is consistent with the chain.
        chain_classes = [self.original_class] + [
            hop.channel_class for hop in self.provenance_chain
        ]
        expected_class = meet(chain_classes)
        if self.admissibility_class != expected_class:
            raise ValueError(
                f"EvidenceItem admissibility_class mismatch: "
                f"declared={self.admissibility_class!r}, "
                f"computed={expected_class!r} from chain {chain_classes!r}.  "
                f"Use make_evidence_item() or extend_evidence() to construct "
                f"items with the correct class."
            )

        # 2. Verify hash if bound.
        if self.hash == "":
            return self
        expected_hash = _compute_evidence_hash(self)
        if self.hash != expected_hash:
            raise ValueError(
                f"EvidenceItem hash integrity check failed.  "
                f"Stored: {self.hash!r}, Expected: {expected_hash!r}.  "
                f"The item may have been tampered with."
            )
        return self


# ── Hash computation ───────────────────────────────────────────────────────

def _compute_evidence_hash(item: EvidenceItem) -> str:
    payload = item.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ── Factory functions ──────────────────────────────────────────────────────

def make_evidence_item(
    payload:         dict[str, Any],
    emitter:         str,
    original_class:  EvidenceClass,
    obligation_hash: str,
    provenance_chain: list[ProvenanceHop] | None = None,
) -> EvidenceItem:
    """
    Construct an unbound EvidenceItem with the admissibility_class computed
    automatically from the chain.  Returns an unbound item; call
    bind_evidence() on it before using it as evidence.
    """
    chain = provenance_chain or []
    chain_classes = [original_class] + [hop.channel_class for hop in chain]
    admissibility = meet(chain_classes)
    return EvidenceItem(
        payload=payload,
        emitter=emitter,
        original_class=original_class,
        provenance_chain=chain,
        admissibility_class=admissibility,
        obligation_hash=obligation_hash,
    )


def bind_evidence(item: EvidenceItem) -> EvidenceItem:
    """
    Compute and stamp the hash on an unbound EvidenceItem.

    Raises ValueError if the item is already bound (hash != "").
    This prevents double-binding, which would indicate a logic error in the
    caller rather than a user error.
    """
    if item.hash != "":
        raise ValueError(
            f"EvidenceItem {item.id!r} is already bound (hash={item.hash!r}).  "
            "Call bind_evidence() only on unbound items."
        )
    h = _compute_evidence_hash(item)
    return item.model_copy(update={"hash": h})


def extend_evidence(
    item:          EvidenceItem,
    channel_class: EvidenceClass,
) -> EvidenceItem:
    """
    Return a new bound EvidenceItem representing the original after it has
    been transmitted through a channel of the given class.

    The original item's hash appears in the new hop's predecessor_hash,
    creating a verifiable chain.

    The new item's admissibility_class is meet(original.original_class,
    *all channel classes including this new hop).  For example:

        extend_evidence(att_item, channel_class=SELF).admissibility_class
        == SELF   # ATT through SELF channel → SELF

    Requires the original item to be bound (hash must be set).
    """
    if item.hash == "":
        raise ValueError(
            f"Cannot extend an unbound EvidenceItem ({item.id!r}).  "
            "Call bind_evidence() on the original before extending."
        )
    new_hop = ProvenanceHop(
        channel_class=channel_class,
        predecessor_hash=item.hash,
    )
    new_chain = list(item.provenance_chain) + [new_hop]
    chain_classes = [item.original_class] + [hop.channel_class for hop in new_chain]
    new_class = meet(chain_classes)

    unbound = EvidenceItem(
        payload=item.payload,
        emitter=item.emitter,
        original_class=item.original_class,
        provenance_chain=new_chain,
        admissibility_class=new_class,
        obligation_hash=item.obligation_hash,
        hash="",
    )
    return bind_evidence(unbound)

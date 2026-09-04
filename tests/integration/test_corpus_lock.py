"""
Corpus hash-lock verification.

This is the control that makes "held-out" mean something (see the Stage 2
handoff, §6.4, and the generator-determinism requirement that followed it):
eval/corpus/CORPUS_LOCK.json is committed once, generated from
eval/corpus/records.jsonl and eval/corpus/split.json via
eval/generator.write_corpus().  This test regenerates the corpus from
scratch, in-memory, on every run and asserts the resulting hashes match the
committed lock file byte-for-byte.

If this test fails after an intentional corpus change, the fix is to
re-run eval/generator.write_corpus() to regenerate records.jsonl,
split.json, and CORPUS_LOCK.json together and commit all three — never to
hand-edit CORPUS_LOCK.json to make the test pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from eval.generator import (
    DEFAULT_GLOBAL_SEED,
    build_corpus,
    build_split,
    records_jsonl,
)

CORPUS_DIR = Path(__file__).resolve().parents[2] / "eval" / "corpus"


def _canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, default=str, ensure_ascii=True)


def _load_lock() -> dict:
    return json.loads((CORPUS_DIR / "CORPUS_LOCK.json").read_text(encoding="utf-8"))


class TestCorpusLock:
    def test_lock_file_exists(self):
        assert (CORPUS_DIR / "CORPUS_LOCK.json").exists()
        assert (CORPUS_DIR / "records.jsonl").exists()
        assert (CORPUS_DIR / "split.json").exists()

    def test_regenerated_records_hash_matches_lock(self):
        lock = _load_lock()
        records = build_corpus(lock["global_seed"])
        regenerated_hash = hashlib.sha256(records_jsonl(records).encode("utf-8")).hexdigest()
        assert regenerated_hash == lock["records_sha256"], (
            "Corpus regenerated from the locked global_seed does not match "
            "CORPUS_LOCK.json — the generator is no longer deterministic, or "
            "the corpus was hand-edited without regenerating the lock."
        )

    def test_regenerated_split_hash_matches_lock(self):
        lock = _load_lock()
        records = build_corpus(lock["global_seed"])
        split = build_split(records, lock["global_seed"])
        payload = {"holdout_fraction": 0.3, "global_seed": lock["global_seed"], "assignments": split}
        regenerated_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        assert regenerated_hash == lock["split_sha256"]

    def test_committed_records_file_matches_its_own_hash(self):
        """Catches the case where records.jsonl was hand-edited after being written."""
        lock = _load_lock()
        on_disk = (CORPUS_DIR / "records.jsonl").read_text(encoding="utf-8")
        assert hashlib.sha256(on_disk.encode("utf-8")).hexdigest() == lock["records_sha256"]

    def test_committed_split_file_matches_its_own_hash(self):
        lock = _load_lock()
        on_disk = (CORPUS_DIR / "split.json").read_text(encoding="utf-8")
        assert hashlib.sha256(on_disk.encode("utf-8")).hexdigest() == lock["split_sha256"]

    def test_record_count_matches_lock(self):
        lock = _load_lock()
        records = build_corpus(lock["global_seed"])
        assert len(records) == lock["record_count"]

    def test_default_seed_matches_lock(self):
        """The committed corpus was built with the module's default seed."""
        lock = _load_lock()
        assert lock["global_seed"] == DEFAULT_GLOBAL_SEED

    def test_regenerating_three_times_is_stable(self):
        lock = _load_lock()
        hashes = set()
        for _ in range(3):
            records = build_corpus(lock["global_seed"])
            hashes.add(hashlib.sha256(records_jsonl(records).encode("utf-8")).hexdigest())
        assert len(hashes) == 1

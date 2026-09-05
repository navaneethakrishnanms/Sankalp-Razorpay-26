"""
Fail if eval/results/ or eval/corpus/ drifted from what's committed — EXCEPT
for stage3_results' latency figures, which are wall-clock measurements and
will never be bit-identical between two runs (different machine, different
moment, same code). A drift check that includes them fails on every run
regardless of whether anything that matters changed — this script exists
to compare everything else exactly while treating latency as expected noise.

Run after `eval/harness.write_results()` has already regenerated the files
on disk. Compares the regenerated working-tree copy against the version
committed at HEAD. Exit code 0 = no meaningful drift, 1 = drift found.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files with a known-volatile field to strip before comparing.
LATENCY_STRIP_JSON = {REPO_ROOT / "eval/results/stage3_results.json"}
LATENCY_LINE_RE = re.compile(r"^- p(50|95): .*$", re.MULTILINE)
LATENCY_STRIP_MD = {REPO_ROOT / "eval/results/stage3_results.md"}

# Every other file under these directories is compared byte-for-byte.
WATCHED_DIRS = ["eval/results", "eval/corpus"]


def _committed_text(path: Path) -> str | None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=REPO_ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout if result.returncode == 0 else None


def _deep_strip_latency(obj: object) -> object:
    """Recursively drop any "latency_seconds" key, wherever it's nested —
    it lives under constraint_and_receipt_verifiers today, but a future
    section could add its own; a top-level-only pop would miss that."""
    if isinstance(obj, dict):
        return {k: _deep_strip_latency(v) for k, v in obj.items() if k != "latency_seconds"}
    if isinstance(obj, list):
        return [_deep_strip_latency(item) for item in obj]
    return obj


def _strip_latency_json(text: str) -> str:
    payload = _deep_strip_latency(json.loads(text))
    return json.dumps(payload, sort_keys=True, indent=2)


def _strip_latency_md(text: str) -> str:
    return LATENCY_LINE_RE.sub("- p50: <volatile>\n- p95: <volatile>", text)


def _changed_files() -> list[Path]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", *WATCHED_DIRS],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        rel = line[3:].strip()
        if rel:
            paths.append(REPO_ROOT / rel)
    return paths


def main() -> int:
    drifted: list[str] = []
    for path in _changed_files():
        committed = _committed_text(path)
        if committed is None:
            drifted.append(f"{path.relative_to(REPO_ROOT)}: new/untracked file")
            continue

        current = path.read_text(encoding="utf-8")

        if path in LATENCY_STRIP_JSON:
            if _strip_latency_json(current) != _strip_latency_json(committed):
                drifted.append(f"{path.relative_to(REPO_ROOT)}: content differs (excl. latency_seconds)")
            continue

        if path in LATENCY_STRIP_MD:
            if _strip_latency_md(current) != _strip_latency_md(committed):
                drifted.append(f"{path.relative_to(REPO_ROOT)}: content differs (excl. latency lines)")
            continue

        if current != committed:
            drifted.append(f"{path.relative_to(REPO_ROOT)}: content differs")

    if drifted:
        print("check_results_no_drift: FAILED — committed results do not match current code.\n",
              file=sys.stderr)
        for line in drifted:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("check_results_no_drift: clean — no meaningful drift (latency noise excluded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
List every seed whose obligation-level fields contradict its instruction text.

Run this after changing any seed's budget_ceiling or delivery_window. It reports
ALL violations in one pass rather than stopping at the first, so a batch of
corpus edits can be fixed as a batch.

    python scripts/audit_seed_traceability.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the repo root,
# so `import eval` fails. Prepend the root so the script works both ways:
#   python scripts/audit_seed_traceability.py
#   python -m scripts.audit_seed_traceability
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eval.generator import (   # noqa: E402 - must follow the sys.path fix above
    GeneratorError,
    _assert_obligation_fields_traceable,
    _assert_stated_criteria_traceable,
    _seeds,
)


def main() -> int:
    findings: list[str] = []
    for seed in _seeds():
        for check in (_assert_stated_criteria_traceable, _assert_obligation_fields_traceable):
            try:
                check(seed)
            except GeneratorError as exc:
                findings.append(str(exc))

    if not findings:
        print("seed traceability: clean — every stated criterion and obligation field "
               "is recoverable from its instruction text.")
        return 0

    print(f"seed traceability: {len(findings)} violation(s)\n", file=sys.stderr)
    for finding in findings:
        print(f"  - {finding}\n", file=sys.stderr)
    print(
        "A ground truth that contradicts its own instruction text is not ground truth: "
        "a compiler reading the user's words would be scored wrong for being right. "
        "Fix the instruction text or the field, then regenerate the corpus.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

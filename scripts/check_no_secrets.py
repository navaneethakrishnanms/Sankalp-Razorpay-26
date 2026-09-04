"""
Fail if a live-looking API key appears anywhere in the tracked tree.

A leaked key in git history is not fixable by deleting the file in a later
commit — the value stays in the object store and is visible to anyone who
clones the repository. So this runs as a test (and in CI) rather than as a
pre-commit hook someone can skip.

WHY THE PATTERNS ARE ASSEMBLED FROM PARTS
-------------------------------------------
The literal prefixes are built by concatenation below, so that this file's own
source does not contain a matching string. Writing them inline would make the
scanner permanently flag itself, and the usual fix for that — excluding this
file from the scan — would create exactly the blind spot the scanner exists to
close.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Assembled, not literal — see the module docstring.
_GROQ_PREFIX = "gsk" + "_"
_ANTHROPIC_PREFIX = "sk-" + "ant-"
_OPENAI_PREFIX = "sk-" + "proj-"

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "groq": re.compile(re.escape(_GROQ_PREFIX) + r"[A-Za-z0-9]{20,}"),
    "anthropic": re.compile(re.escape(_ANTHROPIC_PREFIX) + r"[A-Za-z0-9_\-]{20,}"),
    "openai": re.compile(re.escape(_OPENAI_PREFIX) + r"[A-Za-z0-9_\-]{20,}"),
}

# Never scanned: not tracked, or not text.
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", ".idea", ".vscode", "dist", "build",
}
SKIP_FILES = {".env"}          # gitignored by design; may legitimately hold a key
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".toml", ".yaml", ".yml",
    ".cfg", ".ini", ".sh", ".ps1", ".bat", ".env", ".example", ".html", ".ts", ".tsx",
    # web/ (React frontend) added late — .jsx/.js/.css must be scanned too.
    ".jsx", ".js", ".css",
}


def iter_candidate_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix and path.suffix not in TEXT_SUFFIXES:
            continue
        yield path


def scan(root: Path | None = None) -> list[tuple[Path, int, str]]:
    """Return [(path, line_number, provider)] for every live-looking key found."""
    root = root or REPO_ROOT
    findings: list[tuple[Path, int, str]] = []
    for path in iter_candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for provider, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append((path, line_number, provider))
    return findings


def main() -> int:
    findings = scan()
    if not findings:
        print("check_no_secrets: clean — no live-looking API keys in the tree.")
        return 0

    print("check_no_secrets: FAILED — live-looking API key(s) found.\n", file=sys.stderr)
    for path, line_number, provider in findings:
        # The matched value is deliberately NOT printed: echoing a leaked key
        # into CI logs spreads it further.
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{line_number}  ({provider} key)", file=sys.stderr)
    print(
        "\nRemove the key, then ROTATE it — a value that reached the tree must be "
        "treated as compromised even if never committed. Put real keys in .env "
        "(gitignored); .env.example holds placeholders only.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())

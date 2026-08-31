"""
No live-looking API key may appear anywhere in the tracked tree.

Runs as a test so it executes in CI on every push. A leaked key in git history
is not fixable by deleting the file in a later commit — the value stays in the
object store and is visible to anyone who clones — so this has to fail BEFORE
the commit lands, not after someone notices.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_no_secrets import SECRET_PATTERNS, scan

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestNoSecretsInTree:
    def test_tree_is_clean(self):
        findings = scan(REPO_ROOT)
        # The matched values are deliberately not included in the failure
        # message — echoing a leaked key into CI logs spreads it further.
        located = [f"{p.relative_to(REPO_ROOT)}:{line} ({provider})" for p, line, provider in findings]
        assert not findings, f"Live-looking API key(s) found: {located}. Remove AND rotate."

    def test_env_example_is_committed_and_has_no_real_key(self):
        example = REPO_ROOT / ".env.example"
        assert example.exists(), ".env.example must be committed as the template"
        text = example.read_text(encoding="utf-8")
        assert "REPLACE_ME" in text
        for pattern in SECRET_PATTERNS.values():
            assert not pattern.search(text)

    def test_dotenv_is_gitignored(self):
        """The ignore rule must exist before any key can reach the tree."""
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in ignore.splitlines()}
        assert ".env" in lines, ".env must be gitignored"

    def test_env_example_is_not_gitignored(self):
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "!.env.example" in ignore

    def test_scanner_detects_a_planted_key(self, tmp_path):
        """A scanner that cannot fail is not a control."""
        planted = tmp_path / "leaked.py"
        planted.write_text(f'KEY = "{"gsk" + "_" + "A" * 40}"\n', encoding="utf-8")
        assert scan(tmp_path)

    def test_scanner_ignores_placeholders(self, tmp_path):
        (tmp_path / "ok.py").write_text('KEY = "REPLACE_ME"\n', encoding="utf-8")
        assert not scan(tmp_path)

    def test_scanner_skips_dotenv_itself(self, tmp_path):
        """.env legitimately holds a real key and is gitignored; flagging it
        would train people to ignore the scanner."""
        (tmp_path / ".env").write_text(f'GROQ_API_KEY={"gsk" + "_" + "B" * 40}\n', encoding="utf-8")
        assert not scan(tmp_path)

"""
Unit tests for core/guards/output_validator.py.

The guard is what makes "the LLM never authors a value" checkable rather
than aspirational, so its failure modes matter more than its happy path.
"""

from __future__ import annotations

import pytest

from core.guards.output_validator import (
    OutputValidationError,
    assert_no_authored_values,
    assert_no_urls,
    assert_quoted_span,
    find_digit_spans,
    find_urls,
    is_quoted_span,
    normalise,
)

INSTRUCTION = "Order dinner for 4 people from Biryani House. No beef. Keep it under ₹1500."


class TestNormalise:
    def test_collapses_whitespace(self):
        assert normalise("a   b\n\tc") == "a b c"

    def test_lowercases(self):
        assert normalise("No BEEF") == "no beef"

    def test_strips_edges(self):
        assert normalise("  hello  ") == "hello"

    def test_nfkc_normalises_unicode(self):
        # Fullwidth digits normalise to ASCII under NFKC.
        assert normalise("４") == "4"


class TestQuotedSpan:
    def test_exact_substring_accepted(self):
        assert is_quoted_span("No beef", INSTRUCTION) is True

    def test_case_insensitive(self):
        assert is_quoted_span("no BEEF", INSTRUCTION) is True

    def test_whitespace_insensitive(self):
        assert is_quoted_span("dinner  for   4  people", INSTRUCTION) is True

    def test_absent_span_rejected(self):
        assert is_quoted_span("under ₹2000", INSTRUCTION) is False

    def test_empty_span_rejected(self):
        assert is_quoted_span("", INSTRUCTION) is False

    def test_assert_raises_on_hallucinated_amount(self):
        """The headline case: a model that inflates the budget must fail loudly."""
        with pytest.raises(OutputValidationError, match="does not appear in the instruction"):
            assert_quoted_span("under ₹2000", INSTRUCTION, context="budget")

    def test_assert_raises_on_empty(self):
        with pytest.raises(OutputValidationError, match="empty span"):
            assert_quoted_span("   ", INSTRUCTION, context="budget")

    def test_assert_passes_on_real_span(self):
        assert_quoted_span("under ₹1500", INSTRUCTION, context="budget")   # must not raise

    def test_punctuation_is_not_stripped(self):
        """'Rs 1,500' must not be accepted for an instruction saying '₹1500' —
        the model cannot reformat a value into existence."""
        assert is_quoted_span("Rs 1,500", INSTRUCTION) is False

    def test_hallucinated_ingredient_rejected(self):
        """A span guard catches authored values that contain no digits at all."""
        assert is_quoted_span("peanuts", INSTRUCTION) is False


class TestDigitAndUrlDetection:
    def test_finds_plain_digits(self):
        assert find_digit_spans("order 12 units") == ["12"]

    def test_finds_separated_digits(self):
        assert find_digit_spans("costs 1,500.00") == ["1,500.00"]

    def test_no_digits_in_clean_text(self):
        assert find_digit_spans("no numbers here") == []

    def test_finds_http_url(self):
        assert find_urls("see https://example.com/x") != []

    def test_finds_bare_host(self):
        assert find_urls("visit evil.com/pay") != []

    def test_no_urls_in_clean_text(self):
        assert find_urls("just words") == []


class TestNoAuthoredValues:
    def test_clean_text_passes(self):
        assert_no_authored_values("the instruction is underspecified", context="reason")

    def test_digits_rejected(self):
        with pytest.raises(OutputValidationError, match="free-floating digit span"):
            assert_no_authored_values("order 12 units", context="reason")

    def test_url_rejected(self):
        with pytest.raises(OutputValidationError, match="URL"):
            assert_no_authored_values("see https://example.com", context="reason")

    def test_url_checked_before_digits(self):
        """A URL containing digits should be reported as a URL, which is the
        more specific and more alarming finding."""
        with pytest.raises(OutputValidationError, match="URL"):
            assert_no_authored_values("https://example.com/123", context="reason")

    def test_assert_no_urls_standalone(self):
        assert_no_urls("no links", context="x")
        with pytest.raises(OutputValidationError):
            assert_no_urls("http://a.com", context="x")

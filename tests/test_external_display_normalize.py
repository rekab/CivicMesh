"""Unit tests for external_display._normalize_text.

No HTTP, no DB — pure string transformations. The ordering of operations
matters (collapse-whitespace before strip-controls) so that whitespace
control chars like \\t \\n \\r get converted to spaces instead of being
deleted, preserving word boundaries in real mesh messages.
"""

import unittest

from external_display import _normalize_text


_BODY = 500
_SENDER = 64


class NormalizeTextTest(unittest.TestCase):

    # --- ASCII passthrough -------------------------------------------------

    def test_ascii_passthrough(self):
        self.assertEqual(_normalize_text("hello", _BODY), "hello")

    def test_empty_string(self):
        self.assertEqual(_normalize_text("", _BODY), "")

    def test_whitespace_only(self):
        self.assertEqual(_normalize_text("   ", _BODY), "")

    # --- ASCII fold --------------------------------------------------------

    def test_accented_latin_folds_to_base(self):
        self.assertEqual(_normalize_text("café", _BODY), "cafe")
        self.assertEqual(_normalize_text("résumé", _BODY), "resume")

    def test_emoji_dropped(self):
        self.assertEqual(_normalize_text("hi 🚀", _BODY), "hi")
        self.assertEqual(_normalize_text("💧water", _BODY), "water")

    def test_cjk_dropped(self):
        self.assertEqual(_normalize_text("日本", _BODY), "")

    # --- Control characters ------------------------------------------------

    def test_control_chars_stripped(self):
        self.assertEqual(_normalize_text("a\x00b\x07c\x7Fd", _BODY), "abcd")

    # --- Whitespace collapse ----------------------------------------------

    def test_tab_and_newline_collapse_to_space(self):
        # The bug the original collapse-after-strip ordering would have caused:
        # \t and \n are both in 0x00-0x1F. If strip ran first, they'd be deleted
        # and "a\t\nb" would become "ab" with no word boundary.
        self.assertEqual(_normalize_text("a\t\nb", _BODY), "a b")

    def test_space_run_collapse(self):
        self.assertEqual(_normalize_text("a   b", _BODY), "a b")

    def test_word_boundary_preservation_newline(self):
        self.assertEqual(
            _normalize_text("Greenwood\nLibrary", _BODY),
            "Greenwood Library",
        )

    def test_word_boundary_preservation_crlf(self):
        self.assertEqual(
            _normalize_text("line1\r\nline2", _BODY),
            "line1 line2",
        )

    def test_word_boundary_preservation_mixed(self):
        self.assertEqual(_normalize_text("a\tb\nc", _BODY), "a b c")

    def test_whitespace_adjacent_to_control_char(self):
        # The \n collapses to a space, the \x00 then gets stripped.
        # Verifies the collapse-then-strip ordering didn't reintroduce
        # the deletion bug.
        self.assertEqual(_normalize_text("a\n\x00b", _BODY), "a b")

    def test_strip_outer_whitespace(self):
        self.assertEqual(_normalize_text("  hi  ", _BODY), "hi")

    # --- Length cap --------------------------------------------------------

    def test_cap_at_max_len(self):
        out = _normalize_text("a" * 600, _BODY)
        self.assertEqual(len(out), 500)
        self.assertFalse(out.endswith("..."), "no ellipsis on hard truncate")

    def test_cap_applied_after_nfkd(self):
        # 600 "é" chars → NFKD decomposes each to e + combining-acute;
        # the combining char is non-ASCII and gets dropped; left with 600 "e";
        # then capped to 500.
        out = _normalize_text("é" * 600, _BODY)
        self.assertEqual(out, "e" * 500)

    def test_sender_cap_independent_of_body_cap(self):
        # Sender uses a tighter cap.
        out = _normalize_text("a" * 200, _SENDER)
        self.assertEqual(len(out), _SENDER)

    # --- Composition -------------------------------------------------------

    def test_combined_pipeline(self):
        # Everything happens in one input: accent fold, emoji drop,
        # newline-to-space, control-strip, outer-whitespace-strip, cap.
        s = "  café\nrésumé 🚀 \x07line\t\nthree  "
        out = _normalize_text(s, _BODY)
        self.assertEqual(out, "cafe resume line three")


if __name__ == "__main__":
    unittest.main()

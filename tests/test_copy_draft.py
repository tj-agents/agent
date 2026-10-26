"""copy_draft.py: pure text transforms, POSIX backend selection/argv, and (Windows-only) a real ctypes round trip.

Never writes to this machine's real clipboard, and never touches the primary selection either: this
machine has a live Wayland session, so the POSIX backend tests inject a fake `run`/`which`/`environ`
rather than calling wl-copy/wl-paste/xclip/xsel/pbcopy for real.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.agents/agent/utility/clip/scripts/copy_draft.py'
SPEC = importlib.util.spec_from_file_location('copy_draft', SCRIPT)
COPY_DRAFT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = COPY_DRAFT
SPEC.loader.exec_module(COPY_DRAFT)


class NormaliseDraftTests(unittest.TestCase):
    def test_crlf_becomes_lf(self):
        self.assertEqual(COPY_DRAFT.normalise_draft('a\r\nb\r\n'), 'a\nb')

    def test_trailing_whitespace_is_trimmed_from_the_very_end_only(self):
        # The blank line between paragraphs is NOT trailing whitespace and must survive; only whitespace
        # at the absolute end of the text is removed, matching copy-draft.ps1's non-multiline '\s+$'.
        self.assertEqual(COPY_DRAFT.normalise_draft('line1\n\nline2\n   '), 'line1\n\nline2')

    def test_a_blank_text_normalises_to_empty(self):
        self.assertEqual(COPY_DRAFT.normalise_draft('   \n\n  '), '')


class ReadDraftFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='copy draft ')
        self.addCleanup(self.temp.cleanup)

    def test_a_missing_file_is_an_error(self):
        missing = str(Path(self.temp.name) / 'nope.txt')
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'not found'):
            COPY_DRAFT.read_draft_file(missing)

    def test_an_empty_file_is_an_error(self):
        path = Path(self.temp.name) / 'empty.txt'
        path.write_text('   \n\n  ', encoding='utf-8')
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'empty'):
            COPY_DRAFT.read_draft_file(str(path))

    def test_a_real_file_is_read_as_utf8_and_normalised(self):
        path = Path(self.temp.name) / 'draft.txt'
        path.write_bytes('café é\r\nsecond line\r\n'.encode('utf-8'))
        self.assertEqual(COPY_DRAFT.read_draft_file(str(path)), 'café é\nsecond line')

    def test_a_leading_bom_is_dropped(self):
        path = Path(self.temp.name) / 'bom.txt'
        path.write_bytes(b'\xef\xbb\xbf' + 'hello\nworld'.encode('utf-8'))
        self.assertEqual(COPY_DRAFT.read_draft_file(str(path)), 'hello\nworld')

    def test_non_utf8_bytes_are_a_clear_one_line_error(self):
        path = Path(self.temp.name) / 'latin1.txt'
        path.write_bytes('café'.encode('latin-1'))  # 0xe9 with no continuation byte is invalid UTF-8.
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'not valid UTF-8'):
            COPY_DRAFT.read_draft_file(str(path))


class UnwrapBacktickSpansTests(unittest.TestCase):
    def test_a_single_span_is_unwrapped(self):
        self.assertEqual(COPY_DRAFT.unwrap_backtick_spans('run `az login` now'), 'run az login now')

    def test_multiple_spans_are_each_unwrapped(self):
        self.assertEqual(
            COPY_DRAFT.unwrap_backtick_spans('`a` and `b` and `c`'),
            'a and b and c',
        )

    def test_text_without_backticks_is_unchanged(self):
        self.assertEqual(COPY_DRAFT.unwrap_backtick_spans('plain text'), 'plain text')


class EscapeHtmlTests(unittest.TestCase):
    def test_ampersand_lt_gt_are_escaped_in_that_order(self):
        self.assertEqual(COPY_DRAFT.escape_html('a < b & c > d'), 'a &lt; b &amp; c &gt; d')


class BuildHtmlFragmentTests(unittest.TestCase):
    def test_a_backtick_span_becomes_a_monospace_code_element(self):
        fragment = COPY_DRAFT.build_html_fragment('run `az login` now')
        self.assertEqual(
            fragment,
            'run <code style="font-family:Consolas,\'Courier New\',monospace;">az login</code> now',
        )

    def test_html_special_characters_are_escaped_outside_and_inside_spans(self):
        fragment = COPY_DRAFT.build_html_fragment('a & b `<tag>`')
        self.assertIn('a &amp; b', fragment)
        self.assertIn('<code style="font-family:Consolas,\'Courier New\',monospace;">&lt;tag&gt;</code>', fragment)

    def test_a_single_newline_within_a_paragraph_becomes_one_br(self):
        fragment = COPY_DRAFT.build_html_fragment('line one\nline two')
        self.assertEqual(fragment, 'line one<br>line two')

    def test_a_blank_line_between_paragraphs_becomes_a_double_br(self):
        fragment = COPY_DRAFT.build_html_fragment('first paragraph\n\nsecond paragraph')
        self.assertEqual(fragment, 'first paragraph<br><br>second paragraph')

    def test_no_block_elements_are_introduced(self):
        fragment = COPY_DRAFT.build_html_fragment('a\n\nb')
        self.assertNotIn('<p>', fragment)
        self.assertNotIn('<div>', fragment)


class BuildFullHtmlTests(unittest.TestCase):
    def test_wraps_the_fragment_in_a_minimal_utf8_document(self):
        document = COPY_DRAFT.build_full_html('a <code>b</code>')
        self.assertTrue(document.startswith('<html>'))
        self.assertIn('<meta charset="utf-8">', document)
        self.assertIn('<body>a <code>b</code></body>', document)
        self.assertTrue(document.endswith('</html>'))


class CountCodeSpansTests(unittest.TestCase):
    def test_counts_every_opening_code_tag(self):
        fragment = COPY_DRAFT.build_html_fragment('`a` `b` `c`')
        self.assertEqual(COPY_DRAFT.count_code_spans(fragment), 3)

    def test_zero_when_there_are_no_spans(self):
        self.assertEqual(COPY_DRAFT.count_code_spans('plain'), 0)


class HtmlVisibleTextTests(unittest.TestCase):
    def test_tags_are_stripped_and_br_becomes_newline_which_collapses_to_a_space(self):
        self.assertEqual(
            COPY_DRAFT.html_visible_text('<html><body>a<br>b<br><br>c</body></html>'),
            'a b c',
        )

    def test_entities_are_unescaped(self):
        self.assertEqual(COPY_DRAFT.html_visible_text('a &amp; b &lt;c&gt;'), 'a & b <c>')

    def test_runs_of_whitespace_collapse_to_one_space(self):
        self.assertEqual(COPY_DRAFT.html_visible_text('a   \n\n  b'), 'a b')

    def test_different_attributes_and_surrounding_whitespace_do_not_affect_the_result(self):
        original = COPY_DRAFT.html_visible_text(
            '<html><body><code style="font-family:Consolas,\'Courier New\',monospace;">az login</code></body></html>'
        )
        reserved = COPY_DRAFT.html_visible_text(
            '<html>\n<body class="x">  <code data-extra="1">az login</code>  </body>\n</html>'
        )
        self.assertEqual(original, reserved)

    def test_different_visible_words_are_not_equal(self):
        self.assertNotEqual(
            COPY_DRAFT.html_visible_text('<html><body>run az login now</body></html>'),
            COPY_DRAFT.html_visible_text('<html><body>something different</body></html>'),
        )


class CollapseWhitespaceTests(unittest.TestCase):
    def test_runs_of_whitespace_including_newlines_collapse_to_one_space(self):
        self.assertEqual(COPY_DRAFT.collapse_whitespace('a \n\n  b\tc'), 'a b c')

    def test_leading_and_trailing_whitespace_is_stripped(self):
        self.assertEqual(COPY_DRAFT.collapse_whitespace('  a b  '), 'a b')


class BuildCfHtmlTests(unittest.TestCase):
    def _assert_offsets_are_exact(self, fragment):
        text, start_html, end_html, start_fragment, end_fragment = COPY_DRAFT.build_cf_html(fragment)
        raw = text.encode('utf-8')
        self.assertEqual(raw[start_fragment:end_fragment], fragment.encode('utf-8'))
        self.assertEqual(
            raw[start_html:end_html],
            (COPY_DRAFT._CF_HTML_PRE + fragment + COPY_DRAFT._CF_HTML_POST).encode('utf-8'),
        )
        # The header itself is ASCII, so its own length in characters equals its length in bytes.
        self.assertEqual(text[:start_html].encode('utf-8'), text[:start_html].encode('ascii'))

    def test_offsets_are_exact_for_ascii_text(self):
        self._assert_offsets_are_exact('plain fragment text')

    def test_offsets_are_exact_for_non_ascii_text(self):
        self._assert_offsets_are_exact('café <code style="font-family:Consolas,\'Courier New\',monospace;">é</code> 日本語 emoji 🎉')

    def test_header_fields_are_present_and_fixed_width(self):
        text, *_ = COPY_DRAFT.build_cf_html('x')
        self.assertTrue(text.startswith('Version:0.9\r\n'))
        for field in ('StartHTML:', 'EndHTML:', 'StartFragment:', 'EndFragment:'):
            self.assertIn(field, text)


class ExtractCfHtmlFragmentTests(unittest.TestCase):
    def test_extracts_exactly_the_text_between_the_markers(self):
        cf_html, *_ = COPY_DRAFT.build_cf_html('run <code>az login</code> now')
        self.assertEqual(COPY_DRAFT.extract_cf_html_fragment(cf_html), 'run <code>az login</code> now')

    def test_returns_none_when_a_marker_is_missing(self):
        self.assertIsNone(COPY_DRAFT.extract_cf_html_fragment('<html><body>no markers here</body></html>'))


def which_only(*names):
    allowed = set(names)
    return lambda name: f'/usr/bin/{name}' if name in allowed else None


class PosixClipboardToolTests(unittest.TestCase):
    def test_wayland_session_selects_wl_copy_with_an_html_flavour(self):
        tool = COPY_DRAFT.posix_clipboard_tool({'WAYLAND_DISPLAY': 'wayland-0'}, which_only('wl-copy', 'wl-paste'))
        self.assertEqual(tool.label, 'wl-copy')
        self.assertEqual(tool.copy_argv, ['wl-copy'])
        self.assertEqual(tool.paste_argv, ['wl-paste', '--no-newline'])
        self.assertEqual(tool.html_copy_argv, ['wl-copy', '--type', 'text/html'])
        self.assertEqual(tool.html_paste_argv, ['wl-paste', '--type', 'text/html'])
        self.assertEqual(tool.env_overrides, {})
        self.assertIsNone(tool.no_html_reason)

    def test_wayland_session_without_wl_clipboard_and_no_display_is_an_error_naming_the_package(self):
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'wl-clipboard') as raised:
            COPY_DRAFT.posix_clipboard_tool({'WAYLAND_DISPLAY': 'wayland-0'}, which_only())
        # Without DISPLAY the X11 tools are never selected, so recommending them would mislead.
        self.assertNotIn('xclip', str(raised.exception))

    def test_wayland_session_without_wl_copy_falls_back_to_xclip_when_display_is_also_set(self):
        # XWayland means a Wayland session can still have DISPLAY set; missing wl-copy is not fatal there.
        tool = COPY_DRAFT.posix_clipboard_tool(
            {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}, which_only('xclip')
        )
        self.assertEqual(tool.label, 'xclip')

    def test_wayland_session_without_wl_copy_falls_back_to_xsel_when_display_is_also_set(self):
        tool = COPY_DRAFT.posix_clipboard_tool(
            {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}, which_only('xsel')
        )
        self.assertEqual(tool.label, 'xsel')

    def test_wayland_and_x11_session_with_nothing_available_recommends_wl_clipboard_first(self):
        # Wayland is the session actually running, so it is named first even though DISPLAY (XWayland) is
        # also set; xclip/xsel are named only as that fallback. One check, one message, either way.
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, r'wl-clipboard.*xclip'):
            COPY_DRAFT.posix_clipboard_tool({'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}, which_only())

    def test_x11_session_prefers_xclip_with_an_html_flavour(self):
        tool = COPY_DRAFT.posix_clipboard_tool({'DISPLAY': ':0'}, which_only('xclip', 'xsel'))
        self.assertEqual(tool.label, 'xclip')
        self.assertEqual(tool.copy_argv, ['xclip', '-selection', 'clipboard'])
        self.assertEqual(tool.paste_argv, ['xclip', '-selection', 'clipboard', '-o'])
        self.assertEqual(tool.html_copy_argv, ['xclip', '-selection', 'clipboard', '-t', 'text/html'])
        self.assertEqual(tool.html_paste_argv, ['xclip', '-selection', 'clipboard', '-t', 'text/html', '-o'])

    def test_x11_session_falls_back_to_xsel_with_no_html_flavour(self):
        tool = COPY_DRAFT.posix_clipboard_tool({'DISPLAY': ':0'}, which_only('xsel'))
        self.assertEqual(tool.label, 'xsel')
        self.assertEqual(tool.copy_argv, ['xsel', '--clipboard', '--input'])
        self.assertEqual(tool.paste_argv, ['xsel', '--clipboard', '--output'])
        self.assertIsNone(tool.html_copy_argv)
        self.assertIsNone(tool.html_paste_argv)
        self.assertEqual(tool.no_html_reason, 'xsel cannot set an HTML flavour')

    def test_x11_session_without_either_tool_is_an_error(self):
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'xclip or xsel'):
            COPY_DRAFT.posix_clipboard_tool({'DISPLAY': ':0'}, which_only())

    def test_darwin_selects_pbcopy_with_no_html_flavour(self):
        real_platform = COPY_DRAFT.sys.platform
        COPY_DRAFT.sys.platform = 'darwin'
        try:
            tool = COPY_DRAFT.posix_clipboard_tool({}, which_only('pbcopy', 'pbpaste'))
        finally:
            COPY_DRAFT.sys.platform = real_platform
        self.assertEqual(tool.label, 'pbcopy')
        self.assertEqual(tool.copy_argv, ['pbcopy'])
        self.assertEqual(tool.paste_argv, ['pbpaste'])
        self.assertIsNone(tool.html_copy_argv)
        self.assertEqual(tool.env_overrides, {'LANG': 'en_US.UTF-8', 'LC_CTYPE': 'en_US.UTF-8'})
        self.assertEqual(tool.no_html_reason, 'pbcopy has no HTML flavour')

    def test_no_session_detected_is_an_error(self):
        real_platform = COPY_DRAFT.sys.platform
        COPY_DRAFT.sys.platform = 'linux'
        try:
            with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'No supported clipboard session'):
                COPY_DRAFT.posix_clipboard_tool({}, which_only('wl-copy', 'xclip', 'pbcopy'))
        finally:
            COPY_DRAFT.sys.platform = real_platform


class IsTransientReadbackFailureTests(unittest.TestCase):
    def test_known_transient_shapes_are_recognised_case_insensitively(self):
        for stderr in ('Nothing is copied', 'NOTHING IS COPIED', 'No suitable type', 'target not available',
                       'no selection'):
            with self.subTest(stderr=stderr):
                self.assertTrue(COPY_DRAFT._is_transient_readback_failure(stderr))

    def test_an_unrelated_error_is_not_transient(self):
        self.assertFalse(COPY_DRAFT._is_transient_readback_failure('Permission denied'))

    def test_empty_or_none_stderr_is_not_transient(self):
        self.assertFalse(COPY_DRAFT._is_transient_readback_failure(''))
        self.assertFalse(COPY_DRAFT._is_transient_readback_failure(None))


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_run(results):
    """A fake `subprocess.run`: records every call, and -- like the real thing -- writes stderr into
    whatever file-like object the call passed as `stderr=` (a copy command's tempfile); when `stderr=`
    is `subprocess.PIPE` instead (a read-back command), it is left alone and the `FakeResult.stderr`
    reaches the caller as `result.stderr`, exactly as a real PIPE capture would."""
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        result = results[len(calls) - 1]
        sink = kwargs.get('stderr')
        if sink is not None and hasattr(sink, 'write'):
            sink.write(result.stderr)
        return result

    run.calls = calls
    return run


class CopyPosixTests(unittest.TestCase):
    """Exercises the exact argv/stdin/env copy_posix sends, with a fake `run` -- never a real clipboard."""

    def setUp(self):
        # The retry loop sleeps for real between attempts; stubbed out so a mismatch test does not cost
        # up to a second of wall-clock time for nothing.
        patcher = mock.patch.object(COPY_DRAFT.time, 'sleep', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- default mode copies HTML when the tool supports it ---

    def test_wl_copy_default_mode_copies_html_with_the_type_flag_and_counts_code_spans(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout=COPY_DRAFT.build_full_html(COPY_DRAFT.build_html_fragment('run `az login` now'))),
        ])
        message = COPY_DRAFT.copy_posix(
            'run `az login` now', plain_only=False,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        (copy_argv, copy_kwargs), (paste_argv, paste_kwargs) = run.calls
        self.assertEqual(copy_argv, ['wl-copy', '--type', 'text/html'])
        self.assertIn('<code', copy_kwargs['input'])
        self.assertEqual(paste_argv, ['wl-paste', '--type', 'text/html'])
        self.assertIn('1 code spans', message)
        self.assertIn('HTML verified', message)

    def test_xclip_default_mode_copies_html_with_the_type_flag(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout=COPY_DRAFT.build_full_html(COPY_DRAFT.build_html_fragment('`a` `b`'))),
        ])
        message = COPY_DRAFT.copy_posix(
            '`a` `b`', plain_only=False, environ={'DISPLAY': ':0'}, which=which_only('xclip'), run=run,
        )
        copy_argv, _ = run.calls[0]
        paste_argv, _ = run.calls[1]
        self.assertEqual(copy_argv, ['xclip', '-selection', 'clipboard', '-t', 'text/html'])
        self.assertEqual(paste_argv, ['xclip', '-selection', 'clipboard', '-t', 'text/html', '-o'])
        self.assertIn('2 code spans', message)

    def test_an_html_readback_with_different_attributes_or_whitespace_still_verifies(self):
        # Verification compares visible text (tags stripped, entities unescaped, <br> -> newline, runs of
        # whitespace collapsed) plus the code-span count -- not the exact bytes that were written -- so a
        # tool re-serving the markup with different attributes or reformatted whitespace still passes.
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout='<html>\n<body class="reserved">  run  '
                               '<code data-extra="1">az login</code>  now  </body>\n</html>'),
        ])
        message = COPY_DRAFT.copy_posix(
            'run `az login` now', plain_only=False,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertIn('HTML verified', message)

    def test_an_html_readback_with_different_visible_text_is_an_error(self):
        run = make_run(
            [FakeResult(stdout='')] + [FakeResult(stdout='<html><body>something different</body></html>')] * 5
        )
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'did not read back the HTML flavour'):
            COPY_DRAFT.copy_posix(
                'run `az login` now', plain_only=False,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_an_html_readback_with_the_wrong_code_span_count_is_an_error_even_with_matching_text(self):
        # Visible text matches ("run az login now" either way) but the <code> wrapping is gone, so the
        # span count does not -- that must still fail, not pass on text alone.
        run = make_run(
            [FakeResult(stdout='')] + [FakeResult(stdout='<html><body>run az login now</body></html>')] * 5
        )
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'did not read back the HTML flavour'):
            COPY_DRAFT.copy_posix(
                'run `az login` now', plain_only=False,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_the_html_readback_is_retried_past_a_stale_selection_before_matching(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout='<html><body></body></html>'),  # stale/empty selection, as wl-paste can see
            FakeResult(stdout=COPY_DRAFT.build_full_html(COPY_DRAFT.build_html_fragment('`az login`'))),
        ])
        message = COPY_DRAFT.copy_posix(
            '`az login`', plain_only=False,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertEqual(len(run.calls), 3)
        self.assertIn('1 code spans', message)

    def test_the_html_readback_retries_past_a_transient_nonzero_exit_before_succeeding(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(returncode=1, stderr='No suitable type'),  # e.g. xclip: "target not available"
            FakeResult(stdout=COPY_DRAFT.build_full_html(COPY_DRAFT.build_html_fragment('`az login`'))),
        ])
        message = COPY_DRAFT.copy_posix(
            '`az login`', plain_only=False,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertEqual(len(run.calls), 3)
        self.assertIn('1 code spans', message)

    # --- xsel/pbcopy have no HTML flavour, so default mode falls back to plain and says so ---

    def test_xsel_only_falls_back_to_plain_text_and_says_so(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='run `az login` now')])
        message = COPY_DRAFT.copy_posix(
            'run `az login` now', plain_only=False,
            environ={'DISPLAY': ':0'}, which=which_only('xsel'), run=run,
        )
        copy_argv, _ = run.calls[0]
        self.assertEqual(copy_argv, ['xsel', '--clipboard', '--input'])
        self.assertIn('xsel cannot set an HTML flavour', message)

    def test_pbcopy_falls_back_to_plain_text_and_says_so(self):
        real_platform = COPY_DRAFT.sys.platform
        COPY_DRAFT.sys.platform = 'darwin'
        try:
            run = make_run([FakeResult(stdout=''), FakeResult(stdout='run `az login` now')])
            message = COPY_DRAFT.copy_posix(
                'run `az login` now', plain_only=False,
                environ={}, which=which_only('pbcopy', 'pbpaste'), run=run,
            )
        finally:
            COPY_DRAFT.sys.platform = real_platform
        self.assertIn('pbcopy has no HTML flavour', message)

    def test_pbcopy_and_pbpaste_run_with_a_utf8_locale_env(self):
        real_platform = COPY_DRAFT.sys.platform
        COPY_DRAFT.sys.platform = 'darwin'
        try:
            run = make_run([FakeResult(stdout=''), FakeResult(stdout='café')])
            COPY_DRAFT.copy_posix(
                'café', plain_only=False, environ={'PATH': '/usr/bin'},
                which=which_only('pbcopy', 'pbpaste'), run=run,
            )
        finally:
            COPY_DRAFT.sys.platform = real_platform
        copy_env = run.calls[0][1]['env']
        paste_env = run.calls[1][1]['env']
        self.assertEqual(copy_env['LANG'], 'en_US.UTF-8')
        self.assertEqual(copy_env['LC_CTYPE'], 'en_US.UTF-8')
        self.assertEqual(copy_env['PATH'], '/usr/bin')
        self.assertEqual(paste_env['LANG'], 'en_US.UTF-8')

    # --- --plain-only always copies the literal text, everywhere ---

    def test_plain_only_copies_literal_text_even_when_the_tool_supports_html(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='run `az login` now')])
        message = COPY_DRAFT.copy_posix(
            'run `az login` now', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        copy_argv, copy_kwargs = run.calls[0]
        self.assertEqual(copy_argv, ['wl-copy'])
        self.assertEqual(copy_kwargs['input'], 'run `az login` now')
        self.assertIn('plain text only', message)

    def test_backticks_travel_intact_through_plain_only(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='run `az login` now')])
        COPY_DRAFT.copy_posix(
            'run `az login` now', plain_only=True,
            environ={'DISPLAY': ':0'}, which=which_only('xclip'), run=run,
        )
        copy_kwargs = run.calls[0][1]
        self.assertEqual(copy_kwargs['input'], 'run `az login` now')

    def test_trailing_whitespace_in_the_plain_readback_is_stripped_before_comparing(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='hello world   \n')])
        message = COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertIn('Copied 11 chars', message)

    def test_the_plain_readback_is_retried_past_a_stale_selection_before_matching(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout=''), FakeResult(stdout='hello world')])
        message = COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'DISPLAY': ':0'}, which=which_only('xclip'), run=run,
        )
        self.assertEqual(len(run.calls), 3)
        self.assertIn('plain text only', message)

    def test_the_plain_readback_retries_past_a_transient_nonzero_exit_before_succeeding(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(returncode=1, stderr='Nothing is copied'),  # wl-paste with no owner answering yet
            FakeResult(stdout='hello world'),
        ])
        message = COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertEqual(len(run.calls), 3)
        self.assertIn('plain text only', message)

    def test_the_plain_readback_retries_past_a_transient_nonzero_exit_case_insensitively(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(returncode=1, stderr='NOTHING IS COPIED'),
            FakeResult(stdout='hello world'),
        ])
        message = COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        self.assertEqual(len(run.calls), 3)
        self.assertIn('plain text only', message)

    def test_a_non_transient_nonzero_readback_fails_immediately_without_retrying(self):
        run = make_run([FakeResult(stdout=''), FakeResult(returncode=1, stderr='Permission denied')])
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'Permission denied'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )
        self.assertEqual(len(run.calls), 2)  # the copy, and exactly one read-back attempt -- no retry

    def test_a_plain_readback_mismatch_is_an_error_after_retries_are_exhausted(self):
        run = make_run([FakeResult(stdout='')] + [FakeResult(stdout='something else')] * 5)
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'did not read back'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_a_readback_that_always_exits_nonzero_reports_the_last_stderr(self):
        run = make_run([FakeResult(stdout='')] + [FakeResult(returncode=1, stderr='Nothing is copied')] * 5)
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'Nothing is copied'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_a_zero_exit_wrong_content_attempt_wins_over_later_transient_failures_in_the_final_error(self):
        # One attempt actually read back the wrong content; the rest merely failed to answer at all. The
        # content mismatch is the more informative -- and more correct -- failure to report.
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout='something else'),
            FakeResult(returncode=1, stderr='Nothing is copied'),
            FakeResult(returncode=1, stderr='Nothing is copied'),
            FakeResult(returncode=1, stderr='Nothing is copied'),
            FakeResult(returncode=1, stderr='Nothing is copied'),
        ])
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'did not read back what was copied'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    # --- the copy command's own stdio, and failures of it ---

    def test_the_copy_command_discards_stdout_and_captures_stderr_via_a_tempfile_not_a_pipe(self):
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='hello world')])
        COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        copy_kwargs = run.calls[0][1]
        self.assertEqual(copy_kwargs['stdout'], subprocess.DEVNULL)
        self.assertNotIn(copy_kwargs['stderr'], (subprocess.DEVNULL, subprocess.PIPE))
        self.assertTrue(hasattr(copy_kwargs['stderr'], 'write'))
        self.assertNotIn('stdin', copy_kwargs)
        self.assertEqual(copy_kwargs['encoding'], 'utf-8')
        self.assertEqual(copy_kwargs['errors'], 'replace')

    def test_the_readback_command_captures_stdout_and_stderr_via_pipes_not_a_tempfile(self):
        # A read-back command never forks a persistent server, so there is no hang risk to route around
        # with a tempfile the way the copy command's stderr is.
        run = make_run([FakeResult(stdout=''), FakeResult(stdout='hello world')])
        COPY_DRAFT.copy_posix(
            'hello world', plain_only=True,
            environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
        )
        paste_kwargs = run.calls[1][1]
        self.assertEqual(paste_kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(paste_kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(paste_kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(paste_kwargs['encoding'], 'utf-8')
        self.assertEqual(paste_kwargs['errors'], 'replace')

    def test_a_nonzero_exit_from_the_copy_tool_is_an_error_including_its_stderr(self):
        run = make_run([FakeResult(returncode=1, stderr='no selection owner')])
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'wl-copy copy failed.*no selection owner'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_a_timeout_from_the_copy_tool_is_an_error(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'did not answer within'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )

    def test_no_clipboard_tool_found_is_an_error_naming_the_package(self):
        with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'wl-clipboard'):
            COPY_DRAFT.copy_posix(
                'hello world', plain_only=True,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only(), run=make_run([]),
            )


class LinuxSingleFlavourTechDebtTests(unittest.TestCase):
    """Pins https://github.com/tj-agents/core/issues/389 -- see there for why. If copy_posix ever grows a
    second copy call, this test must change, and that issue should be closed in the same change.
    """

    def test_default_mode_issues_exactly_one_copy_call_and_it_is_html_only(self):
        run = make_run([
            FakeResult(stdout=''),
            FakeResult(stdout=COPY_DRAFT.build_full_html(COPY_DRAFT.build_html_fragment('`a`'))),
        ])
        with mock.patch.object(COPY_DRAFT.time, 'sleep', return_value=None):
            COPY_DRAFT.copy_posix(
                '`a`', plain_only=False,
                environ={'WAYLAND_DISPLAY': 'wayland-0'}, which=which_only('wl-copy', 'wl-paste'), run=run,
            )
        copy_calls = [call for call in run.calls if call[0] and call[0][0] == 'wl-copy']
        self.assertEqual(len(copy_calls), 1)
        self.assertEqual(copy_calls[0][0], ['wl-copy', '--type', 'text/html'])


@unittest.skipUnless(os.name == 'nt', 'Exercises the real Windows clipboard via ctypes; Windows only.')
class WindowsCtypesRoundTripTests(unittest.TestCase):
    """Runs only on Windows. Writes to the real clipboard there -- never on this (Linux) machine."""

    def _read_clipboard(self, user32, kernel32, html_format):
        hwnd = COPY_DRAFT._create_message_window(user32)
        try:
            return COPY_DRAFT._read_windows_clipboard(user32, kernel32, html_format, hwnd)
        finally:
            user32.DestroyWindow(hwnd)

    def test_both_flavours_round_trip_through_the_real_clipboard(self):
        text = "run `az login` now\n\nsecond paragraph"
        message = COPY_DRAFT.copy_windows(text, plain_only=False)
        self.assertIn('HTML + plain verified', message)

        user32, kernel32 = COPY_DRAFT._windows_api()
        html_format = user32.RegisterClipboardFormatW('HTML Format')
        cf_html, plain = self._read_clipboard(user32, kernel32, html_format)
        self.assertIsNotNone(cf_html)
        fragment = COPY_DRAFT.extract_cf_html_fragment(cf_html)
        self.assertIsNotNone(fragment)
        self.assertIn('<code', fragment)
        self.assertEqual(plain, COPY_DRAFT.unwrap_backtick_spans(text))

    def test_plain_only_writes_literal_text_with_no_html_flavour_and_is_verified(self):
        text = 'a literal `command` to run'
        message = COPY_DRAFT.copy_windows(text, plain_only=True)
        self.assertIn('plain text only', message)

        user32, kernel32 = COPY_DRAFT._windows_api()
        html_format = user32.RegisterClipboardFormatW('HTML Format')
        cf_html, plain = self._read_clipboard(user32, kernel32, html_format)
        self.assertIsNone(cf_html)
        self.assertEqual(plain, text)

    def test_a_message_window_creation_failure_is_raised_before_the_clipboard_is_touched(self):
        user32, _ = COPY_DRAFT._windows_api()
        with mock.patch.object(user32, 'CreateWindowExW', return_value=0):
            with mock.patch.object(user32, 'OpenClipboard') as open_clipboard:
                with self.assertRaisesRegex(COPY_DRAFT.ClipboardError, 'CreateWindowExW failed'):
                    COPY_DRAFT.copy_windows('hello', plain_only=True)
                open_clipboard.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Bare visualize references leave the hub in the desktop's sentinel form."""
import random
import unittest

from responses_visualize import (CLOSE, OPEN, SEPARATOR, VisualizeRewriter, wrap_line, wrap_message_item,
                                 wrap_output, wrap_text)

PATH = "/Users/me/.codex/visualizations/2026/10/10/thread/busiest-days.html"
BODY = '{"path":"%s"}' % PATH
BARE = "visualize" + BODY
WRAPPED = OPEN + "visualize" + SEPARATOR + BODY + CLOSE


class WrapTextTests(unittest.TestCase):
    def test_a_bare_reference_line_gains_the_desktop_sentinels(self):
        text = "Yes, here it is.\n\n" + BARE + "\n\n**Patterns worth noticing**\n"
        self.assertEqual(wrap_text(text), "Yes, here it is.\n\n" + WRAPPED + "\n\n**Patterns worth noticing**\n")
        self.assertEqual(wrap_text(BARE), WRAPPED)

    def test_indent_line_ending_and_extra_keys_survive_verbatim(self):
        body = '{"path":"%s","mode":"wide","title":"Busy days"}' % PATH
        wrapped = OPEN + "visualize" + SEPARATOR + body + CLOSE
        self.assertEqual(wrap_line("   visualize" + body + " \t"), "   " + wrapped + " \t")
        self.assertEqual(wrap_text("visualize" + body + "\r\nNext\r\n"), wrapped + "\r\nNext\r\n")

    def test_wrapping_is_idempotent_so_gpt_routes_are_untouched(self):
        text = "Done.\n" + WRAPPED + "\n"
        self.assertEqual(wrap_text(text), text)
        once = wrap_text("x\n" + BARE)
        self.assertEqual(wrap_text(once), once)

    def test_lines_that_are_not_references_are_left_alone(self):
        for line in ["    " + BARE,  # four spaces is indented code
                     "See " + BARE, BARE + " and more",
                     'visualize{"path":""}', 'visualize{"path":"   "}', 'visualize{"path":3}',
                     'visualize{"mode":"wide"}', 'visualize{"path":"a.html"', "visualize{not json}",
                     'visualize{"path":"a.html","value":NaN}', 'visualize{"path":"a.html","value":Infinity}',
                     'visualize["a.html"]', "Visualize" + BODY, 'visualize {"path":"a.html"}']:
            self.assertEqual(wrap_line(line), line, line)
            self.assertEqual(wrap_text("intro\n" + line + "\n"), "intro\n" + line + "\n", line)

    def test_fenced_code_is_skipped_until_a_matching_fence_closes_it(self):
        self.assertEqual(wrap_text("```text\n" + BARE + "\n```\n" + BARE + "\n"),
                         "```text\n" + BARE + "\n```\n" + WRAPPED + "\n")
        # A shorter run or the other character does not close the fence.
        self.assertEqual(wrap_text("````\n```\n" + BARE + "\n~~~~\n" + BARE + "\n ```` \n" + BARE),
                         "````\n```\n" + BARE + "\n~~~~\n" + BARE + "\n ```` \n" + WRAPPED)
        self.assertEqual(wrap_text("~~~\n" + BARE + "\n~~~~\n" + BARE), "~~~\n" + BARE + "\n~~~~\n" + WRAPPED)
        # An unclosed fence hides everything after it.
        self.assertEqual(wrap_text("```\n" + BARE), "```\n" + BARE)

    def test_text_without_a_reference_is_returned_as_is(self):
        for text in ["", "plain prose\n```\ncode\n```\n", "visualize the data\n", None, 3, ["x"]]:
            self.assertIs(wrap_text(text), text)

    def test_responses_output_is_wrapped_in_place_for_message_text_only(self):
        message = {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": BARE}, {"type": "refusal", "refusal": BARE}, "odd"]}
        call = {"type": "function_call", "name": "visualize", "arguments": BARE}
        wrap_output([message, call, "junk"])
        self.assertEqual(message["content"][0]["text"], WRAPPED)
        self.assertEqual(message["content"][1]["refusal"], BARE)
        self.assertEqual(call["arguments"], BARE)
        wrap_message_item(None)
        wrap_output({"output": [message]})


class StreamingRewriterTests(unittest.TestCase):
    TEXT = ("Yes, here it is.\r\n\n" + BARE + "\n\n- **Tokens** peak on 13 July.\n```\n" + BARE + "\n```\n"
            "  " + BARE + " \nvisualize{\"path\":\"\"}\nvery last\n" + BARE)

    def test_every_chunking_streams_exactly_the_whole_block_rewrite(self):
        expected = wrap_text(self.TEXT)
        self.assertNotEqual(expected, self.TEXT)
        self.assertEqual(expected.count(OPEN), 3)
        rng = random.Random(2026)
        chunkings = [[self.TEXT], list(self.TEXT)]
        for _ in range(300):
            text, chunks = self.TEXT, []
            while text:
                size = rng.randint(1, 14)
                chunks.append(text[:size])
                text = text[size:]
            chunkings.append(chunks)
        for chunks in chunkings:
            rewriter = VisualizeRewriter()
            streamed = "".join(rewriter.feed(chunk) for chunk in chunks) + rewriter.flush()
            self.assertEqual(streamed, expected, chunks)

    def test_only_a_possible_reference_line_is_held_back(self):
        rewriter = VisualizeRewriter()
        self.assertEqual(rewriter.feed("Here is the chart"), "Here is the chart")
        # "v" could start the keyword, so it waits for the next delta.
        self.assertEqual(rewriter.feed(" you asked for.\nv"), " you asked for.\n")
        self.assertEqual(rewriter.feed("ery busy.\n"), "very busy.\n")
        self.assertEqual(rewriter.feed('visualize{"path":'), "")
        self.assertEqual(rewriter.feed('"%s"}' % PATH), "")
        self.assertEqual(rewriter.feed("\nAfter."), WRAPPED + "\nAfter.")
        self.assertEqual(rewriter.flush(), "")
        self.assertEqual(rewriter.flush(), "")

    def test_flush_releases_a_held_final_line(self):
        rewriter = VisualizeRewriter()
        self.assertEqual(rewriter.feed("Text\n" + BARE), "Text\n")
        self.assertEqual(rewriter.flush(), WRAPPED)
        rewriter = VisualizeRewriter()
        self.assertEqual(rewriter.feed("visualize{oops"), "")
        self.assertEqual(rewriter.flush(), "visualize{oops")


if __name__ == "__main__":
    unittest.main()

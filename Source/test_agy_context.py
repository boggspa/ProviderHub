import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import agy_cli_agent as adapter
import agy_context as context


class ContextPackagingTests(unittest.TestCase):
    def package(self, directory, messages, system=None, suffix=""):
        return context.package_prompt(messages, system=system, suffix=suffix,
                                      directory=directory, render=adapter.render_prompt)

    def test_small_prompt_remains_verbatim_without_files(self):
        messages = [{"role": "user", "content": "hello"}]
        prompt, paths = self.package(None, messages)
        self.assertEqual((prompt, paths), ("hello", []))

    def test_oversized_unicode_preamble_is_lossless_and_recent_turns_stay_inline(self):
        original = "Policy start\n" + "🙂 café " * 24_000 + "\nPolicy end"
        messages = [{"role": "user", "content": "Original request"},
                    {"role": "assistant", "content": "A host tool was requested"},
                    {"role": "user", "content": "Result: tool returned 42"},
                    {"role": "user", "content": "Steer: use the newest result"}]
        with tempfile.TemporaryDirectory() as directory:
            prompt, paths = self.package(directory, messages, original, "\nNative finish fields.")
            self.assertLessEqual(len(prompt.encode()), context.MAX_PROMPT_BYTES)
            self.assertEqual("".join(Path(p).read_text() for p in paths), original)
            positions = [prompt.index(m["content"]) for m in messages]
            self.assertEqual(positions, sorted(positions))
            self.assertTrue(prompt.endswith("Native finish fields."))
            for path in paths:
                data = Path(path).read_bytes()
                self.assertLessEqual(len(data), context.PART_BYTES)
                self.assertLessEqual(len(data.splitlines()), context.PART_LINES)
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
                self.assertIn(path, prompt)

    def test_large_history_and_single_large_latest_request_are_retained(self):
        for role in ("assistant", "user"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                large = "First\n" + "line\n" * 48_000 + "Last"
                messages = [{"role": "user", "content": "Old request"},
                            {"role": role, "content": large}]
                if role == "assistant":
                    messages.append({"role": "user", "content": "Newest steer"})
                prompt, paths = self.package(directory, messages)
                self.assertEqual("".join(Path(p).read_text() for p in paths), large)
                self.assertLessEqual(len(prompt.encode()), context.MAX_PROMPT_BYTES)
                self.assertIn(f'<turn role="{role}">', prompt)
                if role == "assistant":
                    self.assertIn("Newest steer", prompt)

    def test_many_small_turns_are_packaged_without_dropping_the_latest_request(self):
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}: " + "word " * 100}
                    for i in range(400)] + [{"role": "user", "content": "Final steer: preserve me"}]
        with tempfile.TemporaryDirectory() as directory:
            prompt, paths = self.package(directory, messages)
            self.assertLessEqual(len(prompt.encode()), context.MAX_PROMPT_BYTES)
            self.assertEqual("".join(Path(p).read_text() for p in paths), adapter.render_prompt(messages))

    def test_partial_reads_do_not_count_as_whole_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.txt"
            path.write_text("a\nb\nc\n")
            reads = context.ContextReads([str(path)])
            reads.record(str(path), {"StartLine": 2, "EndLine": 3})
            self.assertFalse(reads.complete())
            reads.record(str(path), {"StartLine": True, "EndLine": 1})
            self.assertFalse(reads.complete())
            reads.record(str(path), {"StartLine": 1, "EndLine": 1})
            self.assertTrue(reads.complete())

    def test_hook_grants_only_exact_private_context_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            part = str((Path(directory) / "context-0001.txt").resolve())
            hook = adapter._install_host_hook(directory, [], [part])
            for index, (name, path, decision) in enumerate([
                ("view_file", part, "allow"), ("view_file", part + ".other", "deny"),
                ("view_file", str(hook / "context.json"), "deny"),
                ("write_to_file", part, "deny"), ("run_command", part, "deny")]):
                with self.subTest(name=name, path=path):
                    payload = {"stepIdx": index, "toolCall": {"name": name, "args": {"AbsolutePath": path}}}
                    result = subprocess.run([sys.executable, str(hook / "host_handoff.py")],
                                            input=json.dumps(payload), text=True, capture_output=True, check=True)
                    self.assertEqual(json.loads(result.stdout)["decision"], decision)

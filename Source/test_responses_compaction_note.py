import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import responses_native
from responses_native import (CODEX_COMPACTION_PROMPT, COMPACTION_NOTE, COMPACTION_TOOLS_NOTE,
                              compaction_note)


PROMPT = (CODEX_COMPACTION_PROMPT + " Create a handoff summary for another LLM that will resume the task.\n\n"
          "Include:\n- Current progress and key decisions made")
HISTORY = [
    {"role": "user", "content": "add the MiniMax provider"},
    {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": "{\"cmd\": \"ls\"}"},
    {"type": "function_call_output", "call_id": "c1", "output": "Source"},
]


class CompactionNoteTests(unittest.TestCase):
    """Codex's tool-less compaction request must yield a handoff, on every route."""

    def _plan(self, *, input_items, tools=(), instructions=None, route="grok/grok-4.6", root=None):
        provider_id = route.split("/", 1)[0]
        runtime = SimpleNamespace(
            settings={"providers": {provider_id: {"base_url": "https://x.invalid"}},
                      "_model_specs": {route: {"context": 500000, "max_output": 16384,
                                               "reasoning": True, "effort_modes": ["low", "high"]}}},
            replay_key="replay", token="token", upstream_url=None, root=root,
            provider_key=lambda pid: "provider-key")
        payload = {"model": route, "store": False, "stream": False,
                   "tools": copy.deepcopy(list(tools)), "input": copy.deepcopy(input_items)}
        if instructions is not None:
            payload["instructions"] = instructions
        with patch.object(responses_native, "validate_connection",
                          return_value={"base_url": "https://x.invalid"}), \
                patch.object(responses_native, "_auth_headers", return_value={}), \
                patch.object(responses_native, "connection_signature", return_value="sig"):
            return responses_native.prepare_native(runtime, payload)

    def test_tool_less_compaction_is_told_the_tools_are_intact(self):
        plan = self._plan(input_items=HISTORY + [{"role": "user", "content": PROMPT}],
                          instructions="You are Codex.")
        self.assertEqual(plan["body"]["instructions"],
                         "You are Codex.\n\n" + COMPACTION_NOTE + COMPACTION_TOOLS_NOTE)

    def test_structured_prompt_content_is_recognised(self):
        item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]}
        self.assertEqual(compaction_note({"input": HISTORY + [item]}, []),
                         COMPACTION_NOTE + COMPACTION_TOOLS_NOTE)

    def test_tools_present_drops_the_tools_sentence(self):
        note = compaction_note({"input": [{"role": "user", "content": PROMPT}]},
                               [{"type": "function", "name": "exec_command"}])
        self.assertEqual(note, COMPACTION_NOTE)

    def test_ordinary_turns_are_left_alone(self):
        for items in (HISTORY,
                      [{"role": "user", "content": "quote this: " + PROMPT}],
                      [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": "ok"}],
                      [{"role": "developer", "content": PROMPT}]):
            with self.subTest(items=items):
                self.assertIsNone(compaction_note({"input": items}, []))
        self.assertIsNone(compaction_note({"input": PROMPT}, []))
        plan = self._plan(input_items=HISTORY + [{"role": "user", "content": "next"}], instructions="Base.")
        self.assertEqual(plan["body"]["instructions"], "Base.")

    def test_note_reaches_the_bridged_messages_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._plan(input_items=HISTORY + [{"role": "user", "content": PROMPT}],
                              route="mistral/mistral-medium-2508", root=Path(tmp))
        self.assertEqual(plan["protocol"], "messages_bridge")
        self.assertEqual(plan["body"]["system"], COMPACTION_NOTE + COMPACTION_TOOLS_NOTE)


if __name__ == "__main__":
    unittest.main()

import copy
import unittest
from unittest.mock import patch

import cli_routes
from cli_image_history import compact_image_history
from test_cli_images import IMAGE
from test_cli_host_tools import MODELS, TOOLS


class ImageHistoryTests(unittest.TestCase):
    def history(self, count):
        return [{"role": "user", "content": [
            {"type": "text", "text": "User correction stays here"},
            {"type": "tool_result", "tool_use_id": "screen", "is_error": False,
             "content": [{"type": "text", "text": "Screenshot result"},
                         *[copy.deepcopy(IMAGE) for _ in range(count)]]}]}]

    def test_count_limit_removes_only_oldest_images_and_does_not_mutate(self):
        history = self.history(22)
        original = copy.deepcopy(history)
        compacted, stats = compact_image_history(history)
        blocks = compacted[0]["content"][1]["content"]
        self.assertEqual(stats["removed"], 2)
        self.assertEqual(stats["kept"], 20)
        self.assertIn("omitted this earlier image", blocks[1]["text"])
        self.assertEqual(blocks[3:], history[0]["content"][1]["content"][3:])
        self.assertEqual(compacted[0]["content"][0]["text"], "User correction stays here")
        self.assertEqual(compacted[0]["content"][1]["tool_use_id"], "screen")
        self.assertEqual(history, original)

    def test_total_byte_limit_keeps_newest_contiguous_images(self):
        from cli_images import image_bytes
        size = len(image_bytes(IMAGE))
        with patch('cli_image_history.MAX_TOTAL_BYTES', size * 2):
            compacted, stats = compact_image_history(self.history(5))
        self.assertEqual((stats['removed'], stats['kept']), (3, 2))
        self.assertEqual([block['type'] for block in compacted[0]['content'][1]['content']],
                         ['text', 'text', 'text', 'text', 'image', 'image'])

    def test_every_cli_route_and_codex_native_history_receive_trimmed_images(self):
        for provider, model in MODELS.items():
            for surface in ('messages', 'responses'):
                with self.subTest(provider=provider, surface=surface):
                    plan = cli_routes.plan_turn(provider, model, {'messages': self.history(25),
                        'tools': TOOLS, '_provider_hub_surface': surface}, {}, wanted_output=128)
                    self.assertEqual(len(plan['body']['images']), 20)
                    self.assertEqual(plan['compatibility']['cli_image_compaction']['removed'], 5)
                    self.assertIn('User correction stays here', plan['body']['messages'][0]['content'])
                    self.assertIn('Screenshot result', plan['body']['messages'][0]['content'])
                    if provider == 'codex':
                        content = plan['body']['history'][0]['content'][1]['content']
                        self.assertEqual(sum(part['type'] == 'image' for part in content), 20)


if __name__ == '__main__':
    unittest.main()

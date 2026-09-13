"""Cerebras org Limits-page context, completion, and image-input metadata."""
import unittest

from providers import discover


class CerebrasOrgLimitsTests(unittest.TestCase):
    def test_org_limits_beat_stale_public_65k_and_advertise_image_input(self):
        def transport(plan):
            if plan["url"] == "https://api.cerebras.ai/v1/models":
                return {"object": "list", "data": [
                    {"id": "gpt-oss-120b"},
                    {"id": "gemma-4-31b"},
                    {"id": "qwen-3.8-27b"},
                ]}
            return {"object": "list", "data": [
                {
                    "id": "gpt-oss-120b",
                    "limits": {"max_context_length": 65536, "max_completion_tokens": 8192},
                    "capabilities": {"vision": False},
                },
                {
                    "id": "gemma-4-31b",
                    "limits": {"max_context_length": 65536, "max_completion_tokens": 8192},
                    "capabilities": {"vision": False},
                },
                {
                    "id": "qwen-3.8-27b",
                    "limits": {"max_context_length": 65536, "max_completion_tokens": 8192},
                    "capabilities": {"vision": False},
                },
            ]}

        by_id = {model["id"]: model for model in discover(
            "cerebras", {}, "secret", transport=transport,
        )["models"]}
        self.assertEqual(by_id["gpt-oss-120b"]["context"], 131000)
        self.assertEqual(by_id["gpt-oss-120b"]["max_output"], 40000)
        self.assertFalse(by_id["gpt-oss-120b"]["vision"])
        self.assertEqual(by_id["gemma-4-31b"]["context"], 131072)
        self.assertEqual(by_id["gemma-4-31b"]["max_output"], 40000)
        self.assertTrue(by_id["gemma-4-31b"]["vision"])
        self.assertEqual(by_id["qwen-3.8-27b"]["context"], 131072)
        self.assertEqual(by_id["qwen-3.8-27b"]["max_output"], 40960)
        self.assertTrue(by_id["qwen-3.8-27b"]["vision"])
        self.assertEqual(by_id["gemma-4-31b"]["context_kind"], "verified_documentation")
        self.assertEqual(by_id["qwen-3.8-27b"]["context_kind"], "verified_documentation")


if __name__ == "__main__":
    unittest.main(verbosity=2)

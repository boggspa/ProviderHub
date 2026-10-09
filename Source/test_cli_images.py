"""Image bytes, tool-result identity, transport framing, and cleanup regressions."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli_images
import cli_routes
import codex_cli_agent
from test_cli_host_tools import Session, MODELS

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=")
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(PNG).decode()}}


def payload():
    return {"messages": [
        {"role": "user", "content": "Click the target visible in the screenshot."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "screen_1",
         "name": "screenshot", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "screen_1",
         "content": [{"type": "text", "text": "Current screen:"}, copy.deepcopy(IMAGE)]}]},
    ]}


class ImageValidationTests(unittest.TestCase):
    def test_tool_result_recovery_preserves_text_images_and_explains_unsupported_blocks(self):
        content = [{"type": "text", "text": "Saved the document."},
                   {"type": "document", "source": {"data": "PRIVATE-DATA"}}, IMAGE,
                   {"type": "image", "source": {"type": "url", "url": "https://example.test/signed-secret"}}]
        original = copy.deepcopy(content)
        result = cli_images.recover_tool_content(content)
        self.assertEqual(result[0], content[0])
        self.assertEqual(result[2], IMAGE)
        for index in (1, 3):
            self.assertIn("could not relay", result[index]["text"])
        self.assertNotIn("PRIVATE-DATA", json.dumps(result))
        self.assertNotIn("signed-secret", json.dumps(result))
        self.assertEqual(content, original)

    def test_malformed_tool_media_does_not_expose_raw_content_or_poison_the_result(self):
        content = [{"type": {}}, {"type": "image", "source": "secret-source"},
                   {"type": "image", "source": {}, "detail": []}, None]
        result = cli_images.recover_tool_content(content)
        self.assertEqual(len(result), 4)
        self.assertTrue(all("could not relay" in part["text"] for part in result))
        self.assertNotIn("secret-source", json.dumps(result))

    def test_responses_tool_document_no_longer_poisons_codex_history(self):
        from responses_bridge import to_messages
        body = {"input": [{"type": "function_call_output", "call_id": "read-1", "output": [
            {"type": "input_text", "text": "Extracted title"}, {"type": "document", "source": {"data": "BINARY"}}]}],
            "stream": True}
        translated = to_messages(body, "codex/gpt-6-luna", {}, None, "scope")
        plan = cli_routes.plan_turn("codex", "gpt-6-luna", translated, {}, wanted_output=64)
        output = codex_cli_agent._history_items(plan["body"]["history"])[0]
        self.assertEqual(output["call_id"], "read-1")
        self.assertEqual(output["output"][0]["text"], "Extracted title")
        self.assertIn("could not relay", output["output"][1]["text"])

    def test_bytes_are_unchanged_in_each_encoding(self):
        for content in (cli_images.prompt_content("p", [IMAGE]),
                        cli_images.prompt_content("p", [IMAGE], acp=True)):
            image = content[-1]
            encoded = image.get("data") or image["source"]["data"]
            self.assertEqual(base64.b64decode(encoded), PNG)
        output = cli_images.responses_content([IMAGE])[0]
        self.assertEqual(base64.b64decode(output["image_url"].split(",", 1)[1]), PNG)

    def test_invalid_unsupported_and_remote_images_are_explicit_errors(self):
        for source in ({"type": "url", "url": "https://example.test/screen.png"},
                       {"type": "base64", "media_type": "image/png", "data": "bad$"},
                       {"type": "base64", "media_type": "image/jpeg", "data": IMAGE["source"]["data"]},
                       {"type": "base64", "media_type": "image/svg+xml", "data": "PHN2Zz4="}):
            with self.subTest(source=source), self.assertRaises(cli_images.CliImageError):
                cli_images.normalize_image({"type": "image", "source": source})

    def test_count_and_byte_limits_do_not_drop_images(self):
        with self.assertRaises(cli_images.CliImageError):
            cli_images.normalize_images([IMAGE] * 21)
        with patch.object(cli_images, "MAX_TOTAL_BYTES", 1), self.assertRaises(cli_images.CliImageError):
            cli_images.normalize_images([IMAGE])

    def test_oversized_tool_image_becomes_a_placeholder_but_user_input_stays_strict(self):
        from cli_image_history import compact_image_history
        original = payload()["messages"]
        with patch.object(cli_images, "MAX_IMAGE_BYTES", 8):
            result, _ = compact_image_history(original, recover_tool_results=True)
            output = codex_cli_agent._history_items(result)[-1]
            self.assertEqual(output["output"][0]["text"], "Current screen:")
            self.assertIn("8 MiB limit", output["output"][1]["text"])
            with self.assertRaises(cli_images.CliImageError):
                compact_image_history([{"role": "user", "content": [IMAGE]}], recover_tool_results=True)

    def test_resumed_tool_result_retains_success_and_other_blocks_when_one_image_cannot_relay(self):
        from types import SimpleNamespace
        from test_codex_cli_agent import FakeCodexSession
        session = FakeCodexSession([])
        lease = SimpleNamespace(session=session, thread_id="t", turn_id="u", pending={"rpc_id": 3})
        result = {"content": [{"type": "text", "text": "Saved output"}, IMAGE], "is_error": False}
        with patch.object(cli_images, "MAX_IMAGE_BYTES", 8):
            codex_cli_agent._resume_host_call(lease, result, [], timeout=1)
        reply = session.sent[0]
        self.assertTrue(reply["result"]["success"])
        self.assertEqual(reply["result"]["contentItems"][0]["text"], "Saved output")
        self.assertIn("could not relay", reply["result"]["contentItems"][1]["text"])
        self.assertIsNone(lease.pending)

    def test_private_image_files_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = cli_images.write_images([IMAGE], directory)
            path = Path(paths[0])
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(path.exists())

    def test_dimensions_label_coordinate_space_without_resizing(self):
        self.assertEqual(cli_images.image_dimensions(IMAGE), (1, 1))
        jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x02\xd0\x05\x00\x01\x01\x11\x00\xff\xd9"
        image = {"source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode()}}
        self.assertEqual(cli_images.image_dimensions(image), (1280, 720))
        self.assertIn("width 1280, height 720", cli_images.image_label(image, 2))

    def test_responses_screenshot_tool_result_reaches_cli_native_history(self):
        from responses_bridge import to_messages
        body = {"input": [{"role": "user", "content": "Inspect the screen"},
                {"type": "function_call", "call_id": "screen_1", "name": "screenshot", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "screen_1", "output": [
                    {"type": "input_image", "image_url": "data:image/png;base64," + IMAGE["source"]["data"]}]}],
                "stream": False, "store": False, "tools": []}
        translated = to_messages(body, "codex/gpt-6-astra", {}, None, "scope")
        plan = cli_routes.plan_turn("codex", "gpt-6-astra", translated, {}, wanted_output=64)
        item = codex_cli_agent._history_items(plan["body"]["history"])[-1]
        self.assertEqual(item["call_id"], "screen_1")
        self.assertEqual(item["output"][0]["type"], "input_image")


class ImageRoutingTests(unittest.TestCase):
    def test_supported_routes_collect_images_with_numbered_tool_result_context(self):
        for provider in ("codex", "claude", "grok", "muse", "antigravity"):
            original = payload()
            preserved = copy.deepcopy(original)
            plan = cli_routes.plan_turn(provider, MODELS[provider], original, {}, wanted_output=64)
            self.assertEqual(original, preserved)
            self.assertEqual(plan["body"]["images"], [IMAGE])
            context = plan["body"]["messages"][-1]["content"]
            self.assertIn("screen_1", context)
            self.assertIn("Image 1 attached", context)
            self.assertNotIn(IMAGE["source"]["data"], context)
            self.assertEqual(plan["compatibility"]["cli_images"], 1)
            self.assertIn("x * width / 1000", plan["body"]["system"])

    def test_missing_transport_and_nonvision_models_reject_images(self):
        with patch.object(codex_cli_agent, "IMAGE_TRANSPORT", None), \
                self.assertRaisesRegex(cli_routes.CliRouteError, "does not support screenshot"):
            cli_routes.plan_turn("codex", "gpt-6-astra", payload(), {}, wanted_output=64)
        with self.assertRaisesRegex(cli_routes.CliRouteError, "model does not support"):
            cli_routes.plan_turn("codex", "text-model", payload(), {"vision": False}, wanted_output=64)

    def test_model_without_vision_is_not_advertised_as_image_capable(self):
        row = cli_routes._hub_row("antigravity", {"id": "gpt-oss-120b", "vision": False})
        self.assertIs(row["vision"], False)

    def test_verified_cli_models_advertise_images_to_codex_desktop(self):
        from codex_catalogue import project_codex
        from test_codex_catalogue import settings
        rows = []
        for provider in ("claude", "grok", "muse", "antigravity"):
            row = cli_routes._hub_row(provider, {"id": MODELS[provider]})
            self.assertIs(row["vision"], True)
            rows.append({**row, "id": provider + "/" + MODELS[provider],
                         "provider_id": provider, "model_id": MODELS[provider]})
        selected = [row["id"] for row in rows]
        projection = project_codex(settings(codex_catalogue=selected, codex_model=selected[0]), {"models": rows})
        # The desktop must offer image input, not merely the bridge accept it.
        for row in projection["models"]:
            self.assertIn("image", row["input_modalities"])
        self.assertEqual(len(projection["models"]), 4)
        self.assertIsNone(cli_routes._hub_row("muse", {"id": "unknown-model"})["vision"])

    def test_codex_native_tool_output_contains_real_image_blocks(self):
        items = codex_cli_agent._history_items(payload()["messages"])
        call, result = items[-2:]
        self.assertEqual(call["call_id"], result["call_id"])
        self.assertEqual([p["type"] for p in result["output"]], ["input_text", "input_image"])
        self.assertEqual(base64.b64decode(result["output"][1]["image_url"].split(",", 1)[1]), PNG)

    def test_codex_user_images_and_direct_turn_images_are_preserved(self):
        items = codex_cli_agent._history_items([{"role": "user", "content": [IMAGE]}])
        self.assertEqual(items[0]["content"][0]["type"], "input_image")
        params = codex_cli_agent._turn_params({"model": "m", "effort": None, "prompt": "look",
                                              "images": [IMAGE]}, "t")
        self.assertEqual(params["input"][1]["type"], "image")

    def test_text_transports_deliver_images_and_remove_private_files(self):
        for provider in ("claude", "grok", "muse", "antigravity"):
            with self.subTest(provider=provider):
                adapter = cli_routes.adapter_for(provider)
                captured = {}
                def factory(argv, **kwargs):
                    session = Session([])
                    session.sent = ""
                    session.write = lambda text: setattr(session, "sent", session.sent + text)
                    session.close_stdin = lambda: None
                    captured.update(argv=argv, session=session, cwd=kwargs.get("cwd"))
                    if provider in {"muse", "antigravity"}:
                        image_path = (Path(argv[argv.index("--image") + 1]) if provider == "muse"
                                      else Path(kwargs["cwd"]) / "image-1.png")
                        captured.update(image_path=image_path, image=image_path.read_bytes())
                        if provider == "muse":
                            session.script = [{"payload_type": "run.terminal.completed", "payload": {"terminal": "completed", "text": "ok"}}]
                        else:
                            self.assertEqual(argv[argv.index("--add-dir") + 1], kwargs["cwd"])
                            session.script = [{"event": "step_update", "step_update": {
                                "step_type": "tool", "state": "DONE", "tool_info": {"name": "view_file",
                                "parameters": {"AbsolutePath": str(image_path)}}}},
                                {"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}]
                    elif provider == "grok":
                        prompt_path = Path(argv[argv.index("--prompt-file") + 1])
                        captured.update(prompt_path=prompt_path, content=json.loads(prompt_path.read_text()))
                        session.script = [{"type": "result", "subtype": "success", "result": "ok"}]
                    else:
                        session.script = [{"type": "result", "subtype": "success", "result": "ok"}]
                    return session
                plan = cli_routes.plan_turn(provider, MODELS[provider], payload(), {}, wanted_output=64)
                with patch.object(adapter, "StdioSession", side_effect=factory), \
                        patch.object(adapter, "_resolve_binary", return_value="/fake/cli"):
                    events = list(adapter.run_turn(plan["body"]))
                self.assertNotIn("error", [e["type"] for e in events], events)
                self.assertNotIn(IMAGE["source"]["data"], " ".join(captured["argv"]))
                if provider in {"muse", "antigravity"}:
                    self.assertEqual(captured["image"], PNG)
                    self.assertFalse(captured["image_path"].exists())
                elif provider == "grok":
                    self.assertEqual(base64.b64decode(captured["content"][-1]["data"]), PNG)
                    self.assertFalse(captured["prompt_path"].exists())
                else:
                    content = json.loads(captured["session"].sent)["message"]["content"]
                    self.assertEqual(content[-1]["source"], IMAGE["source"])

    def test_cancelling_an_image_turn_removes_private_media(self):
        for provider in ("grok", "muse", "antigravity"):
            adapter = cli_routes.adapter_for(provider)
            captured = {}
            def factory(argv, **kwargs):
                captured["directory"] = Path(kwargs["cwd"])
                script = ([{"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"}]}}]
                          if provider == "grok" else
                          [{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "looking"}}]
                          if provider == "antigravity" else
                          [{"payload_type": "run.output.delta", "payload": {"text": "looking"}}])
                return Session(script)
            plan = cli_routes.plan_turn(provider, MODELS[provider], payload(), {}, wanted_output=64)
            with patch.object(adapter, "StdioSession", side_effect=factory), \
                    patch.object(adapter, "_resolve_binary", return_value="/fake/cli"):
                events = adapter.run_turn(plan["body"])
                self.assertEqual(next(events)["type"], "text_delta")
                self.assertTrue(captured["directory"].exists())
                events.close()
            self.assertFalse(captured["directory"].exists())

    def test_antigravity_native_reader_only_accepts_declared_image_files(self):
        import agy_cli_agent as agy
        for name, path in (("run_command", "/tmp/screenshot.png"),
                           ("view_file", "/tmp/unoffered.png")):
            state = agy._TurnState()
            state.image_paths = [str(Path("/tmp/screenshot.png").resolve())]
            events = agy._translate({"event": "step_update", "step_update": {
                "step_type": "tool", "state": "ACTIVE", "tool_info": {"name": name,
                "parameters": {"AbsolutePath": path}}}}, state)
            self.assertEqual(events, [])
            self.assertIn("outside the supplied image", state.failure)


if __name__ == "__main__":
    unittest.main()

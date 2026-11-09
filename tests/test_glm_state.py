import io
import tempfile
import unittest
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

from panel.app import PanelHandler, PanelState, init_db
from panel.chat_service import ChatSendError, ImageValidationError
from panel.glm_service import GeneratedImage, GLMResult


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PUBLIC_RESOLVER = lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))]


class StateClient:
    def __init__(self):
        self.send_calls = []
        self.fail_mode = None

    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]

    def get_chats(self, _session, _limit, _offset):
        return [{"id": "8613800138000@c.us", "name": "客户", "lastMessage": {"body": "图片"}}]

    def get_messages(self, _session, _chat_id, _limit, _offset, _before=None, download_media=False):
        return [{
            "id": "incoming-image",
            "timestamp": 10,
            "fromMe": False,
            "body": "请看看",
            "hasMedia": True,
            "media": {"mimetype": "image/png", "filename": "incoming.png"},
        }]

    def get_message(self, _session, _chat_id, _message_id, download_media=True):
        return {
            "id": "incoming-image",
            "hasMedia": True,
            "media": {"url": "http://waha.internal/media/incoming.png", "mimetype": "image/png"},
        }

    def get_media_bytes(self, _media_url):
        return 200, "image/png", png_bytes()

    def send_image(self, _session, chat_id, filename, mimetype, data_b64, caption):
        self.send_calls.append((chat_id, filename, mimetype, data_b64, caption))
        if self.fail_mode == "unknown":
            raise TimeoutError("simulated timeout")
        if self.fail_mode == "failed":
            raise ValueError("simulated failure")
        return {"id": "generated-image-message"}


class FakeGLM:
    def __init__(self):
        self.vision_calls = []
        self.image_calls = []

    def chat_vision(self, profile, prompt, image_bytes, mime_type, reasoning=False, context=None, tools=None):
        self.vision_calls.append({
            "profile": profile,
            "prompt": prompt,
            "data": image_bytes,
            "mime_type": mime_type,
            "reasoning": reasoning,
            "context": context,
            "tools": tools,
        })
        return GLMResult("图片识别结果", "内部推理")

    def generate_image(self, profile, prompt, size=None):
        self.image_calls.append((profile, prompt, size))
        return GeneratedImage("image/png", png_bytes())


def profiles():
    common = {
        "provider": "glm",
        "base_url": "https://example.com/api/paas/v4",
        "api_key": "state-secret",
        "reasoning_mode": "disabled",
    }
    return {
        "vision_understanding": {**common, "model": "glm-5.3-flash"},
        "vision_reasoning": {**common, "model": "glm-5.3-flash", "reasoning_mode": "high"},
        "image_generation": {**common, "model": "glm-image"},
    }


class GLMStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = StateClient()
        self.state = PanelState(
            self.database,
            self.client,
            "waha-key",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.ai_profiles.resolver = PUBLIC_RESOLVER
        self.state.save_ai_profiles("default", profiles())
        self.fake_glm = FakeGLM()
        self.state.glm = self.fake_glm

    def tearDown(self):
        self.temp.cleanup()

    def chat_refs(self):
        chat_ref = self.state.chat.overview("default")["items"][0]["chat_ref"]
        message_ref = self.state.chat.messages("default", chat_ref)["items"][0]["message_ref"]
        return chat_ref, message_ref

    def test_vision_for_message_reads_current_image_and_rejects_cross_session_reference(self):
        chat_ref, message_ref = self.chat_refs()
        result = self.state.vision_for_message(
            "default", chat_ref, message_ref, "请识别图片", "vision_understanding"
        )
        self.assertEqual(result["content"], "图片识别结果")
        self.assertEqual(result["mode"], "vision_understanding")
        self.assertEqual(self.fake_glm.vision_calls[0]["mime_type"], "image/png")
        with self.assertRaises(Exception):
            self.state.vision_for_message(
                "sales", chat_ref, message_ref, "请识别图片", "vision_understanding"
            )

    def test_generate_preview_and_duplicate_send_are_idempotent(self):
        chat_ref, _message_ref = self.chat_refs()
        generated = self.state.generate_image("default", chat_ref, "一只白猫", "1024x1024")
        self.assertTrue(generated["image_ref"])
        self.assertEqual(self.state.generated_image("default", chat_ref, generated["image_ref"])[0], "image/png")
        request_id = str(uuid.uuid4())
        first = self.state.send_generated_image(
            "default", chat_ref, generated["image_ref"], "生成图", request_id
        )
        second = self.state.send_generated_image(
            "default", chat_ref, generated["image_ref"], "生成图", request_id
        )
        self.assertEqual(first["state"], "SENT")
        self.assertEqual(second["state"], "SENT")
        self.assertEqual(len(self.client.send_calls), 1)

    def test_definite_send_failure_releases_image_but_unknown_consumes_it(self):
        chat_ref, _message_ref = self.chat_refs()
        failed = self.state.generate_image("default", chat_ref, "失败后重试")
        self.client.fail_mode = "failed"
        with self.assertRaises(ChatSendError) as caught:
            self.state.send_generated_image(
                "default", chat_ref, failed["image_ref"], "", str(uuid.uuid4())
            )
        self.assertEqual(caught.exception.state, "FAILED")
        self.client.fail_mode = None
        retry = self.state.send_generated_image(
            "default", chat_ref, failed["image_ref"], "", str(uuid.uuid4())
        )
        self.assertEqual(retry["state"], "SENT")

        unknown = self.state.generate_image("default", chat_ref, "未知结果")
        self.client.fail_mode = "unknown"
        with self.assertRaises(ChatSendError) as caught:
            self.state.send_generated_image(
                "default", chat_ref, unknown["image_ref"], "", str(uuid.uuid4())
            )
        self.assertEqual(caught.exception.state, "UNKNOWN")
        self.client.fail_mode = None
        with self.assertRaises(Exception):
            self.state.send_generated_image(
                "default", chat_ref, unknown["image_ref"], "", str(uuid.uuid4())
            )

    def test_invalid_caption_releases_reserved_reference(self):
        chat_ref, _message_ref = self.chat_refs()
        generated = self.state.generate_image("default", chat_ref, "caption 校验")
        with self.assertRaises(ImageValidationError):
            self.state.send_generated_image(
                "default", chat_ref, generated["image_ref"], "x" * 4097, str(uuid.uuid4())
            )
        retry = self.state.send_generated_image(
            "default", chat_ref, generated["image_ref"], "", str(uuid.uuid4())
        )
        self.assertEqual(retry["state"], "SENT")

    def test_image_generation_connection_test_is_configuration_only(self):
        result = self.state.test_ai_profile("default", "image_generation")
        self.assertTrue(result["ok"])
        self.assertTrue(result["configuration_only"])
        self.assertEqual(self.fake_glm.image_calls, [])

    def test_chat_route_accepts_glm_actions_without_accepting_unknown_actions(self):
        self.assertEqual(
            PanelHandler.chat_route("/api/chat/sessions/default/vision"),
            ("default", "vision"),
        )
        self.assertEqual(
            PanelHandler.chat_route("/api/chat/sessions/default/generate-image"),
            ("default", "generate-image"),
        )
        self.assertEqual(
            PanelHandler.chat_route("/api/chat/sessions/default/generated-image"),
            ("default", "generated-image"),
        )
        with self.assertRaises(ValueError):
            PanelHandler.chat_route("/api/chat/sessions/default/not-a-glm-action")


if __name__ == "__main__":
    unittest.main()

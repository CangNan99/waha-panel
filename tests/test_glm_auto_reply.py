import hashlib
import hmac
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

from panel.app import PanelState, init_db
from panel.glm_service import GLMError, GLMResult


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


PUBLIC_RESOLVER = lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))]


class WebhookClient:
    def __init__(self):
        self.sent_text = []
        self.fail_media = False

    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]

    def get_message(self, _session, _chat_id, _message_id, download_media=True):
        return {
            "id": "image-1",
            "hasMedia": True,
            "media": {"url": "http://waha.internal/media/customer.png", "mimetype": "image/png"},
        }

    def get_media_bytes(self, _url):
        if self.fail_media:
            raise OSError("media unavailable")
        return 200, "image/png", png_bytes()

    def send_text(self, _session, chat_id, text):
        self.sent_text.append((chat_id, text))
        return {"id": "reply-1"}


class VisionStub:
    def __init__(self):
        self.calls = []
        self.text_calls = []
        self.fail = False

    def chat_text(self, profile, messages, tools=None):
        self.text_calls.append({"profile": profile, "messages": messages, "tools": tools})
        return GLMResult("文本 GLM 回复")

    def chat_vision(self, profile, prompt, image_bytes, mime_type, reasoning=False, context=None, tools=None):
        self.calls.append({
            "profile": profile,
            "prompt": prompt,
            "bytes": image_bytes,
            "mime_type": mime_type,
            "reasoning": reasoning,
            "context": context,
            "tools": tools,
        })
        if self.fail:
            raise GLMError("视觉服务失败", "AI_UPSTREAM_ERROR")
        return GLMResult("这是图片中的内容。", "隐藏推理")


def vision_profiles(include_reasoning=False):
    base = {
        "provider": "glm",
        "base_url": "https://example.com/api/paas/v4",
        "api_key": "vision-secret",
        "reasoning_mode": "disabled",
    }
    values = {"vision_understanding": {**base, "model": "glm-5.3-flash"}}
    if include_reasoning:
        values["vision_reasoning"] = {
            **base,
            "model": "glm-5.3-flash",
            "reasoning_mode": "high",
        }
    return values


class GLMAutoReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = WebhookClient()
        self.state = PanelState(
            self.database,
            self.client,
            "waha-key",
            sleep_fn=lambda _seconds: None,
            uniform_fn=lambda _low, _high: 1.0,
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.ai_profiles.resolver = PUBLIC_RESOLVER
        self.state.save_settings({
            "auto_reply_all_day": True,
            "persona": "专业客服",
            "system_prompt": "只根据业务事实回答",
            "default_reply_text": "固定兜底不应发送",
        })
        self.state.commerce_config = replace(self.state.commerce_config, enabled=True)
        self.vision = VisionStub()
        self.state.glm = self.vision

    def tearDown(self):
        self.temp.cleanup()

    def webhook_body(self, message_id="image-1", caption="请看看这张图"):
        event = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": message_id,
                "from": "8613800138000@c.us",
                "to": "8613900000000@c.us",
                "fromMe": False,
                "hasMedia": True,
                "type": "image",
                "body": caption,
                "caption": caption,
                "timestamp": 100,
            },
        }
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        signature = hmac.new(self.state.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return body, {"X-Webhook-Signature": "sha256=" + signature}

    def test_customer_image_is_understood_and_replied_once(self):
        self.state.save_ai_profiles("default", vision_profiles())
        body, headers = self.webhook_body()
        result = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        duplicate = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(result["action"], "replied")
        self.assertEqual(result["mode"], "glm_vision")
        self.assertEqual(duplicate["reason"], "重复消息")
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(self.vision.calls[0]["prompt"], "请看看这张图")
        self.assertIn("专业客服", self.vision.calls[0]["context"]["system"])
        self.assertEqual(self.vision.calls[0]["tools"][0]["function"]["name"], "query_order")

    def test_no_caption_gets_safe_image_prompt(self):
        self.state.save_ai_profiles("default", vision_profiles())
        body, headers = self.webhook_body(message_id="image-no-caption", caption="")
        result = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(result["action"], "replied")
        self.assertIn("图片", self.vision.calls[0]["prompt"])

    def test_reasoning_profile_wins_when_both_visual_profiles_exist(self):
        self.state.save_ai_profiles("default", vision_profiles(include_reasoning=True))
        body, headers = self.webhook_body(message_id="image-reasoning")
        self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertTrue(self.vision.calls[0]["reasoning"])
        self.assertEqual(self.vision.calls[0]["profile"]["model"], "glm-5.3-flash")

    def test_missing_profile_or_model_failure_never_sends_fixed_text(self):
        body, headers = self.webhook_body(message_id="image-no-profile")
        missing = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(missing["action"], "ignored")
        self.assertEqual(len(self.client.sent_text), 0)

        self.state.save_ai_profiles("default", vision_profiles())
        self.vision.fail = True
        body, headers = self.webhook_body(message_id="image-failure")
        failed = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(failed["action"], "ignored")
        self.assertEqual(len(self.client.sent_text), 0)

    def test_human_takeover_and_media_failure_do_not_send(self):
        self.state.save_ai_profiles("default", vision_profiles())
        chat_ref = self.state.chat._encode_chat("default", "8613800138000@c.us")
        self.state.chat.takeover("default", chat_ref)
        body, headers = self.webhook_body(message_id="image-takeover")
        paused = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(paused["reason"], "人工接管中")

        self.state.chat.resume_ai("default", chat_ref)
        self.client.fail_media = True
        body, headers = self.webhook_body(message_id="image-media-failure")
        failed = self.state.handle_webhook(body, headers, remote_addr="127.0.0.1")
        self.assertEqual(failed["action"], "ignored")
        self.assertEqual(len(self.client.sent_text), 0)

    def test_language_profile_uses_glm_adapter_for_text_reply(self):
        self.state.save_ai_profiles("default", {
            "language": {
                "provider": "glm",
                "base_url": "https://example.com/api/paas/v4",
                "model": "glm-5.3",
                "reasoning_mode": "disabled",
                "api_key": "language-secret",
            }
        })
        reply, mode = self.state._ai_reply(
            self.state._settings("default"),
            "客户问候",
            session_name="default",
        )
        self.assertEqual(reply, "文本 GLM 回复")
        self.assertEqual(mode, "ai")
        self.assertEqual(self.vision.text_calls[0]["profile"]["model"], "glm-5.3")


if __name__ == "__main__":
    unittest.main()

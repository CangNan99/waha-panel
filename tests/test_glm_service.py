import base64
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from panel.glm_service import GLMError, GLMService, GeneratedImageStore


def png_bytes():
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes():
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


class Response:
    def __init__(self, payload, status=200, content_type="application/json"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


PROFILE = {
    "provider": "glm",
    "capability": "vision_understanding",
    "base_url": "https://example.com/api/paas/v4",
    "model": "glm-5.3-flash",
    "api_key": "secret-key",
    "reasoning_mode": "disabled",
}


class GLMServiceTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

    def opener_for(self, payload):
        def opener(request, timeout=0):
            self.requests.append((request, timeout))
            return Response(payload)

        return opener

    def test_text_request_uses_chat_completions_and_preserves_tools(self):
        service = GLMService(
            opener=self.opener_for({"choices": [{"message": {"content": "回答"}}]}),
            resolver=lambda host, port, type=None: [(None, None, None, None, ("93.184.216.34", port))],
        )
        result = service.chat_text(
            {**PROFILE, "capability": "language"},
            [{"role": "user", "content": "你好"}],
            tools=[{"type": "function", "function": {"name": "query_order"}}],
        )
        request, timeout = self.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.com/api/paas/v4/chat/completions")
        self.assertEqual(timeout, 30)
        self.assertEqual(payload["model"], "glm-5.3-flash")
        self.assertEqual(payload["tools"][0]["function"]["name"], "query_order")
        self.assertEqual(result.content, "回答")
        self.assertEqual(request.headers["Authorization"], "Bearer secret-key")

    def test_vision_request_contains_bounded_image_data_uri(self):
        service = GLMService(
            opener=self.opener_for({"choices": [{"message": {"content": "图片回复"}}]}),
        )
        result = service.chat_vision(PROFILE, "请识别图片", png_bytes(), "image/png")
        payload = json.loads(self.requests[0][0].data.decode("utf-8"))
        content = payload["messages"][-1]["content"]
        self.assertEqual(result.content, "图片回复")
        self.assertEqual(content[0], {"type": "text", "text": "请识别图片"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertLess(len(content[1]["image_url"]["url"]), 10000)

    def test_vision_accepts_jpeg_input(self):
        service = GLMService(
            opener=self.opener_for({"choices": [{"message": {"content": "JPEG 回复"}}]}),
        )
        service.chat_vision(PROFILE, "请识别 JPEG", jpeg_bytes(), "image/jpeg")
        content = json.loads(self.requests[0][0].data.decode("utf-8"))["messages"][-1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_reasoning_request_sends_thinking_but_returns_only_final_content(self):
        service = GLMService(
            opener=self.opener_for({
                "choices": [{
                    "message": {
                        "content": "最终答案",
                        "reasoning_content": "内部推理，不应发送",
                    }
                }]
            }),
        )
        profile = {**PROFILE, "reasoning_mode": "high", "capability": "vision_reasoning"}
        result = service.chat_vision(profile, "分析图片", png_bytes(), "image/png", reasoning=True)
        payload = json.loads(self.requests[0][0].data.decode("utf-8"))
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(result.content, "最终答案")
        self.assertEqual(result.reasoning_content, "内部推理，不应发送")

    def test_image_generation_parses_base64_response(self):
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        service = GLMService(
            opener=self.opener_for({"data": [{"b64_json": encoded}]}),
        )
        result = service.generate_image(
            {**PROFILE, "capability": "image_generation", "model": "glm-image"},
            "一只白猫",
            size="1024x1024",
        )
        payload = json.loads(self.requests[0][0].data.decode("utf-8"))
        self.assertEqual(self.requests[0][0].full_url, "https://example.com/api/paas/v4/images/generations")
        self.assertEqual(payload["size"], "1024x1024")
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.data, png_bytes())

    def test_invalid_upstream_response_does_not_expose_api_key(self):
        service = GLMService(opener=self.opener_for({"choices": []}))
        with self.assertRaises(GLMError) as caught:
            service.chat_text({**PROFILE, "capability": "language"}, [{"role": "user", "content": "x"}])
        self.assertEqual(caught.exception.code, "AI_RESPONSE_INVALID")
        self.assertNotIn("secret-key", str(caught.exception))

    def test_configured_full_glm_endpoints_are_normalized_without_duplicate_suffixes(self):
        root = "https://example.com/api/paas/v4"
        configured_urls = [
            root,
            root + "/",
            root + "/chat/completions",
            root + "/images/generations",
            root + "/tenant-a",
        ]
        encoded = base64.b64encode(png_bytes()).decode("ascii")

        def opener(request, timeout=0):
            self.requests.append((request, timeout))
            if request.full_url.endswith("/images/generations"):
                return Response({"data": [{"b64_json": encoded}]})
            return Response({"choices": [{"message": {"content": "回答"}}]})

        for configured in configured_urls:
            with self.subTest(configured=configured):
                self.requests.clear()
                service = GLMService(opener=opener)
                profile = {**PROFILE, "base_url": configured}
                service.chat_text(profile, [{"role": "user", "content": "你好"}])
                service.generate_image(profile, "一只白猫")
                urls = [request.full_url for request, _ in self.requests]
                expected_root = configured.rstrip("/")
                for known_suffix in ("/chat/completions", "/images/generations"):
                    if expected_root.endswith(known_suffix):
                        expected_root = expected_root[: -len(known_suffix)]
                self.assertEqual(
                    urls,
                    [
                        expected_root + "/chat/completions",
                        expected_root + "/images/generations",
                    ],
                )
                self.assertEqual(urls[0].count("/chat/completions"), 1)
                self.assertEqual(urls[1].count("/images/generations"), 1)


class GeneratedImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [100]
        self.store = GeneratedImageStore(clock=lambda: self.now[0])

    def test_reference_is_bound_and_unknown_outcome_cannot_be_retried(self):
        reference = self.store.put("default", "chat-hmac", "image/png", png_bytes())
        self.assertEqual(self.store.get("default", "chat-hmac", reference)[0], "image/png")
        self.store.reserve("default", "chat-hmac", reference, "request-1")
        self.store.finalize("default", "chat-hmac", reference, "request-1", "UNKNOWN")
        with self.assertRaises(GLMError):
            self.store.reserve("default", "chat-hmac", reference, "request-2")
        with self.assertRaises(GLMError):
            self.store.get("sales", "chat-hmac", reference)

    def test_definite_failure_releases_reference_and_expiry_removes_it(self):
        reference = self.store.put("default", "chat-hmac", "image/png", png_bytes())
        self.store.reserve("default", "chat-hmac", reference, "request-1")
        self.store.finalize("default", "chat-hmac", reference, "request-1", "FAILED")
        self.store.reserve("default", "chat-hmac", reference, "request-2")
        self.now[0] = 701
        with self.assertRaises(GLMError):
            self.store.get("default", "chat-hmac", reference)


if __name__ == "__main__":
    unittest.main()

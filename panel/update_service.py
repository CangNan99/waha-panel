"""Read-only Docker Hub update checks for the two independently managed images."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DOCKER_HUB_API = "https://hub.docker.com/v2/repositories"
VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
UNSTABLE_WORDS = {"alpha", "beta", "dev", "development", "nightly", "rc", "test", "unstable"}


class UpdateCheckError(RuntimeError):
    code = "UPDATE_CHECK_FAILED"


def _version_from_tag(tag):
    match = VERSION_RE.search(str(tag or ""))
    if not match:
        return None
    suffix = (match.group(4) or "").lower()
    if suffix and any(word in suffix.split(".") for word in UNSTABLE_WORDS):
        return None
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def _safe_error(error):
    message = str(error or "")
    message = re.sub(r"(?i)(authorization|token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", message)
    return message[:300]


class UpdateService:
    """Query public metadata, cache it briefly, and never mutate Docker state."""

    def __init__(self, current_panel="1.0.4", current_waha="latest-2026.8.2",
                 panel_repository="cangnan88/waha-panel", waha_repository="devlikeapro/waha",
                 opener=None, clock=None, cache_ttl=900):
        self.current = {"panel": str(current_panel or ""), "waha": str(current_waha or "")}
        self.repositories = {"panel": panel_repository, "waha": waha_repository}
        self.opener = opener or urlopen
        self.clock = clock or time.time
        self.cache_ttl = max(0, int(cache_ttl))
        self._cache = {}

    @staticmethod
    def _tag_allowed(component, tag):
        name = str(tag or "").strip()
        if not name or name.lower() in {"latest", "dev"}:
            return False
        if component == "panel":
            return bool(re.fullmatch(r"v?\d+\.\d+\.\d+", name)) and _version_from_tag(name) is not None
        # The portable release pins the stable WAHA "latest-YYYY.M.D" family.
        return bool(re.fullmatch(r"latest-\d{4}\.\d{1,2}\.\d{1,2}", name)) and _version_from_tag(name) is not None

    def _fetch_tags(self, component):
        repository = self.repositories[component].strip().strip("/")
        if "/" not in repository:
            raise UpdateCheckError("镜像仓库名称无效")
        url = f"{DOCKER_HUB_API}/{quote(repository, safe='/')}/tags?page_size=100&ordering=last_updated"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with self.opener(request, timeout=8) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise UpdateCheckError(_safe_error(error)) from error
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise UpdateCheckError("镜像仓库返回格式不正确")
        tags = [item.get("name") for item in results if isinstance(item, dict)]
        return [tag for tag in tags if self._tag_allowed(component, tag)]

    def _latest(self, component):
        now = float(self.clock())
        cached = self._cache.get(component)
        if cached and cached[0] > now:
            return cached[1]
        tags = self._fetch_tags(component)
        if not tags:
            raise UpdateCheckError("没有找到稳定版本标签")
        latest = max(tags, key=lambda tag: _version_from_tag(tag) or (0, 0, 0))
        result = {"latest_tag": latest, "latest_version": ".".join(map(str, _version_from_tag(latest)))}
        self._cache[component] = (now + self.cache_ttl, result)
        return result

    def _component_result(self, component):
        current_tag = self.current[component]
        current_version = ".".join(map(str, _version_from_tag(current_tag))) if _version_from_tag(current_tag) else current_tag
        result = {
            "repository": self.repositories[component],
            "current_tag": current_tag,
            "current_version": current_version,
            "latest_tag": None,
            "latest_version": None,
            "update_available": False,
            "status": "unknown",
        }
        try:
            latest = self._latest(component)
            result.update(latest)
            current_tuple = _version_from_tag(current_tag)
            latest_tuple = _version_from_tag(latest["latest_tag"])
            result["update_available"] = bool(current_tuple and latest_tuple and latest_tuple > current_tuple)
            result["status"] = "update_available" if result["update_available"] else "current"
        except UpdateCheckError as error:
            result["status"] = "error"
            result["error"] = _safe_error(error)
        return result

    def check(self):
        return {"panel": self._component_result("panel"), "waha": self._component_result("waha")}

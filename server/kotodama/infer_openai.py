"""Voice casting via OpenAI chat completions with a strict JSON schema."""

from __future__ import annotations

import http.client
import json
import urllib.parse
from typing import Any

from .archetypes import Archetypes
from .config import Config
from .infer import SYSTEM_PROMPT, InferenceError, build_user_prompt, parse_result


class OpenAIInferer:
    def __init__(self, cfg: Config, arch: Archetypes, schema: dict[str, Any], tts_provider: str):
        self.arch = arch
        self.schema = schema
        self.tts_provider = tts_provider
        self.model: str = cfg.get("inference.openai_model", "gpt-4o-mini")
        self.key = cfg.api_key("inference", "openai_api_key", "openai_api_key_env") or cfg.api_key("tts")
        if not self.key:
            raise InferenceError("no OpenAI API key for voice casting (OPENAI_API_KEY)")
        parsed = urllib.parse.urlparse(str(cfg.get("tts.base_url")))
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname or "api.openai.com"
        self._port = parsed.port or (443 if self._https else 80)
        self._prefix = parsed.path.rstrip("/")

    def infer(self, context: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(context)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "voice_profile", "strict": True, "schema": self.schema},
            },
        }).encode("utf-8")
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=30)
        try:
            conn.request("POST", f"{self._prefix}/v1/chat/completions", body=body, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise InferenceError(f"request failed: {exc}") from exc
        finally:
            conn.close()
        if resp.status != 200:
            raise InferenceError(f"HTTP {resp.status}: {data[:300].decode('utf-8', 'replace')}")
        try:
            choice = json.loads(data)["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InferenceError("unexpected response shape") from exc
        if message.get("refusal"):
            raise InferenceError("the model declined the request")
        return parse_result(message.get("content") or "", self.arch, self.tts_provider)

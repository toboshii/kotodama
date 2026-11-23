"""Voice casting via the Anthropic Messages API (official `anthropic` SDK)."""

from __future__ import annotations

import sys
from typing import Any, Callable

from .archetypes import Archetypes
from .config import Config
from .infer import SYSTEM_PROMPT, InferenceError, build_user_prompt, parse_result

BETA_FALLBACK = "server-side-fallback-2026-07-01"
# Models whose safety classifiers can decline a request. For these we opt in
# to the server-side fallback, so a spurious refusal is retried automatically.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class AnthropicInferer:
    on_usage: Callable[[str, int, int], None] | None = None

    def __init__(self, cfg: Config, arch: Archetypes, schema: dict[str, Any], tts_provider: str,
                 client: Any = None):
        self.arch = arch
        self.schema = schema
        self.tts_provider = tts_provider
        self.model: str = cfg.get("inference.model")
        self.effort: str = cfg.get("inference.effort", "low")
        if client is None:
            try:
                import anthropic  # lazy: only needed when casting is enabled
            except ImportError as exc:
                launcher = "server\\kotodama.cmd" if sys.platform == "win32" else "server/kotodama.sh"
                raise InferenceError(
                    "inference.provider is 'anthropic' but this Python has no anthropic SDK: "
                    f"install it in server/.venv and start the sidecar with {launcher}") from exc
            key = cfg.api_key("inference")
            if not key:
                raise InferenceError("no Anthropic API key (ANTHROPIC_API_KEY or inference.api_key)")
            client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
        self.client = client

    def infer(self, context: dict[str, Any]) -> dict[str, Any]:
        output_config = {"effort": self.effort, "format": {"type": "json_schema", "schema": self.schema}}
        common = {
            "model": self.model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_prompt(context)}],
            "output_config": output_config,
        }
        try:
            if self.model in FALLBACK_MODELS:
                resp = self.client.beta.messages.create(
                    **common, betas=[BETA_FALLBACK], fallbacks="default")
            else:
                resp = self.client.messages.create(**common)
        except TypeError as exc:
            raise InferenceError(f"installed anthropic SDK is too old for this request: {exc}") from exc

        self._report_usage(resp)
        if resp.stop_reason == "refusal":
            raise InferenceError("the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise InferenceError("response was cut off (max_tokens)")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise InferenceError("response had no text block")
        return parse_result(text, self.arch, self.tts_provider)

    def _report_usage(self, resp: Any) -> None:
        """Pass the call's tokens to on_usage. A refused or cut-off reply is
        billed too, so this runs before those are checked. The model is the
        one that answered, which a server-side fallback can change."""
        usage = getattr(resp, "usage", None)
        if self.on_usage is None or usage is None:
            return
        model = getattr(resp, "model", None)
        tokens = [getattr(usage, f, None) for f in ("input_tokens", "output_tokens")]
        self.on_usage(model if isinstance(model, str) and model else self.model,
                      *(t if isinstance(t, int) else 0 for t in tokens))

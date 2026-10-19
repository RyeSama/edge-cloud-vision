"""Cloud decision service: LLM first, deterministic conservative fallback.

Failure policy is explicit: if the LLM is unavailable, timed out, returned
malformed JSON, or is not configured, the event is *not* silently accepted. It
becomes ``risk_level=unknown`` and ``action=request_human_review``. That is the
"degrade gracefully" story in the README, implemented rather than claimed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ecvision.cloud.prompts import SYSTEM_PROMPT, build_user_prompt
from ecvision.cloud.settings import CloudSettings
from ecvision.contracts import Decision, Event

log = logging.getLogger(__name__)

_VALID_RISK = {"low", "medium", "high", "unknown"}


def fallback_decision(event_id: str, reason: str) -> Decision:
    return Decision(
        event_id=event_id,
        risk_level="unknown",
        action="request_human_review",
        rationale=f"LLM 未配置或调用失败（{reason}），按保守策略转人工复核。",
        source="rule_fallback",
        model="rule",
        latency_ms=0.0,
    )


class CloudDecisionService:
    def __init__(self, settings: CloudSettings) -> None:
        self._settings = settings
        # Do not let httpx parse ambient NO_PROXY/HTTP_PROXY. On Windows a
        # common NO_PROXY value contains "[::1]", which httpx 0.28 fails to
        # parse at client construction (InvalidURL: Invalid port ':1]').
        # Proxy use is explicit via ECV_LLM_PROXY instead.
        self._client = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(settings.llm_timeout_s),
                trust_env=False,
                proxy=settings.llm_proxy,
            )
            if settings.llm_enabled
            else None
        )
        self.last_error: str | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _call(self, event: Event, *, json_mode: bool) -> str:
        assert self._client is not None
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(event)},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = await self._client.post(
            f"{self._settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty LLM content")
        return content

    async def decide(self, event: Event) -> Decision:
        started = time.perf_counter()
        if self._client is None:
            return fallback_decision(event.event_id, "API key not configured")

        try:
            try:
                content = await self._call(event, json_mode=True)
            except httpx.HTTPStatusError as exc:
                # Some OpenAI-compatible gateways reject response_format with 400.
                if exc.response.status_code != 400:
                    raise
                log.info("gateway rejected JSON mode; retrying without response_format")
                content = await self._call(event, json_mode=False)
            return self._parse(event, content, started)
        except Exception as exc:  # network, schema, JSON, timeout: all must degrade
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("LLM fallback for %s: %s", event.event_id, self.last_error)
            decision = fallback_decision(event.event_id, type(exc).__name__)
            decision.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            return decision

    def _parse(self, event: Event, content: str, started: float) -> Decision:
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM response contains no JSON object")
        data = json.loads(text[start : end + 1])

        risk = str(data.get("risk_level", "")).strip().lower()
        action = str(data.get("action", "")).strip()
        rationale = str(data.get("rationale", "")).strip()
        if risk not in _VALID_RISK:
            raise ValueError(f"invalid risk_level: {risk!r}")
        if not action or not rationale:
            raise ValueError("LLM response missing action/rationale")

        return Decision(
            event_id=event.event_id,
            risk_level=risk,  # type: ignore[arg-type]
            action=action[:256],
            rationale=rationale[:512],
            source="llm",
            model=self._settings.llm_model,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )

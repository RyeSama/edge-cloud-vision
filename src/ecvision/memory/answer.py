"""Optional RAG answer generator.

The retrieval/context path is fully local. This module only adds a natural
language answer when an OpenAI-compatible endpoint is configured; otherwise the
API returns the raw JSON timeline, which is still useful and still private.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from ecvision.memory.models import ContextBundle

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是边缘时序事件记忆助手。你只能依据用户提供的时间轴回答，不得编造未出现的人、物体、时间或地点。
要求：
1. 先给结论，再给依据；依据必须带时间戳。
2. 如果时间轴不足以回答，明确说“现有事件不足以判断”，并建议缩小时间范围或补充设备。
3. 不要输出与时间轴无关的推测。
4. 用简洁中文回答。"""


class Answerer(Protocol):
    async def answer(self, question: str, context: ContextBundle) -> str | None: ...

    async def aclose(self) -> None: ...


class AnswerSettings(Protocol):
    """Structural settings so memory never imports the cloud package."""

    llm_enabled: bool
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_s: float
    llm_proxy: str | None


class OpenAIMemoryAnswerer:
    def __init__(self, settings: AnswerSettings) -> None:
        self._settings = settings
        self.last_error: str | None = None
        self._client = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(settings.llm_timeout_s),
                trust_env=False,
                proxy=settings.llm_proxy,
            )
            if settings.llm_enabled
            else None
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def answer(self, question: str, context: ContextBundle) -> str | None:
        if self._client is None:
            return None
        try:
            response = await self._client.post(
                f"{self._settings.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                json={
                    "model": self._settings.llm_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"问题：{question}\n\n时间轴：\n{context.text}",
                        },
                    ],
                    "temperature": 0.0,
                    "max_tokens": 512,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty answer")
            self.last_error = None
            return content.strip()
        except Exception as exc:  # noqa: BLE001 - answer is optional
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("memory answer fallback: %s", self.last_error)
            return None

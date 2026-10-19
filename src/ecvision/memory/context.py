"""Token-bounded context assembly.

The context builder is deliberately deterministic: it sorts by time, renders a
compact table or JSON timeline, and stops adding rows before the budget is
exceeded. It does not call a tokenizer service, which keeps the edge gateway
usable offline and makes the tests exact.
"""

from __future__ import annotations

import json
import math

from ecvision.memory.models import ContextBundle, MemoryItem


def estimate_tokens(text: str) -> int:
    """Conservative estimate: CJK chars count 1, ASCII counts ~4 chars/token."""
    cjk = 0
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or char in "，。！？；：、（）【】「」":
            cjk += 1
    ascii_chars = len(text) - cjk
    return max(1, math.ceil(cjk + ascii_chars / 4.0))


class ContextBuilder:
    def __init__(self) -> None:
        self._header = (
            "| 时间 | 设备 | 标签 | 置信度 | 层级 | 次数 | 风险/动作 | 说明 |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )

    def build(
        self,
        items: list[MemoryItem],
        *,
        token_budget: int = 4000,
        format: str = "markdown",
    ) -> ContextBundle:
        if token_budget < 32:
            raise ValueError("token_budget must be >= 32")
        ordered = sorted(items, key=lambda item: item.ts)
        if format == "json":
            return self._build_json(ordered, token_budget)
        return self._build_markdown(ordered, token_budget)

    def _build_markdown(self, items: list[MemoryItem], token_budget: int) -> ContextBundle:
        header = self._header
        if estimate_tokens(header) > token_budget:
            return ContextBundle(
                format="markdown",
                text="",
                token_count=0,
                truncated=True,
                item_count=0,
            )
        rows: list[str] = []
        used = estimate_tokens(header)
        truncated = False
        for item in items:
            row = self._row(item)
            cost = estimate_tokens(row)
            if used + cost > token_budget:
                truncated = True
                break
            rows.append(row)
            used += cost
        text = header + "".join(rows)
        if truncated:
            note = "\n> 结果已按 token 预算截断。"
            if used + estimate_tokens(note) <= token_budget:
                text += note
                used += estimate_tokens(note)
            else:
                truncated = True
        return ContextBundle(
            format="markdown",
            text=text,
            token_count=estimate_tokens(text),
            truncated=truncated,
            item_count=len(rows),
        )

    def _build_json(self, items: list[MemoryItem], token_budget: int) -> ContextBundle:
        selected: list[dict] = []
        truncated = False
        for item in items:
            candidate = selected + [self._json_item(item)]
            text = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if estimate_tokens(text) > token_budget:
                truncated = True
                break
            selected.append(self._json_item(item))
        text = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
        return ContextBundle(
            format="json",
            text=text,
            token_count=estimate_tokens(text),
            truncated=truncated or len(selected) < len(items),
            item_count=len(selected),
        )

    def _row(self, item: MemoryItem) -> str:
        stamp = item.ts.isoformat(timespec="seconds")
        label = _escape(item.label)
        note = _escape(_truncate(item.text, 160))
        risk = item.risk_level or "-"
        action = item.action or "-"
        return (
            f"| {stamp} | {_escape(item.device_id)} | {label} | {item.confidence:.3f} | "
            f"{item.tier.value} | {item.summary_count} | {_escape(risk)}/{_escape(action)} | {note} |\n"
        )

    def _json_item(self, item: MemoryItem) -> dict:
        return {
            "event_id": item.event_id,
            "ts": item.ts.isoformat(),
            "device_id": item.device_id,
            "label": item.label,
            "confidence": round(item.confidence, 4),
            "tier": item.tier.value,
            "summary_count": item.summary_count,
            "risk_level": item.risk_level,
            "action": item.action,
            "text": item.text,
        }


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"

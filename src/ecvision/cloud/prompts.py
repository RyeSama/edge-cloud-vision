"""Prompt construction for the cloud fallback.

The LLM never receives pixels. It receives the edge model's structured belief
plus gate metadata, which keeps bandwidth and privacy costs low and makes the
prompt auditable.
"""

from __future__ import annotations

import json

from ecvision.contracts import Event

SYSTEM_PROMPT = """你是工业视觉系统的边缘复核助手。
端侧轻量模型只能提供低分辨率画面的分类结果，无法提供原始图片。
你的任务是根据这些结构化信息做保守的安全分级，不要臆造画面细节。

只输出一个 JSON 对象，不要 Markdown，不要额外解释：
{"risk_level":"low|medium|high|unknown","action":"给现场执行的动作，<=80字","rationale":"判断依据，<=120字"}

规则：
1. 信息不足、类别模糊、置信度低于 0.5 时，risk_level 用 "unknown"，action 用 "request_human_review"。
2. 只有存在明确安全风险时才用 "high"；不要为了显得谨慎而全部判为 high。
3. action 必须是可执行的短指令，例如 "continue_monitoring"、"request_human_review"、"stop_conveyor"。
"""


def build_user_prompt(event: Event) -> str:
    payload = {
        "event_id": event.event_id,
        "device_id": event.device_id,
        "ts": event.ts.isoformat(),
        "resolution": f"{event.source.width}x{event.source.height}",
        "gate": {
            "decision": event.gate.decision.value,
            "reason": event.gate.reason.value,
            "accept_threshold": event.gate.accept_threshold,
            "review_threshold": event.gate.review_threshold,
        },
        "inference": {
            "model": event.inference.model,
            "label": event.inference.label,
            "label_id": event.inference.label_id,
            "confidence": event.inference.confidence,
            "top_k": [
                {"label": item.label, "confidence": item.confidence}
                for item in event.inference.top_k
            ],
        },
        "note": "以上仅为端侧结构化结果，没有原始图像。",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

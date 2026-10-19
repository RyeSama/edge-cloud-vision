"""Cloud settings, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ecvision.config import MqttConfig, env_float, env_int


@dataclass(frozen=True, slots=True)
class CloudSettings:
    mqtt: MqttConfig
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_s: float
    store_size: int
    queue_size: int
    llm_proxy: str | None = None

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @classmethod
    def from_env(cls) -> CloudSettings:
        return cls(
            mqtt=MqttConfig.from_env(client_id=os.getenv("ECV_CLOUD_CLIENT_ID", "ecv-cloud")),
            llm_base_url=os.getenv("ECV_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            llm_api_key=os.getenv("ECV_LLM_API_KEY", "").strip(),
            llm_model=os.getenv("ECV_LLM_MODEL", "deepseek-chat"),
            llm_timeout_s=env_float("ECV_LLM_TIMEOUT_S", 8.0),
            store_size=env_int("ECV_STORE_SIZE", 500),
            queue_size=env_int("ECV_QUEUE_SIZE", 1000),
            llm_proxy=os.getenv("ECV_LLM_PROXY") or None,
        )

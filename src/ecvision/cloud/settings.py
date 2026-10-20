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
    memory_db_path: str = "data/edge_mem.sqlite3"
    memory_timezone: str = "Asia/Shanghai"
    memory_hot_minutes: int = 10
    memory_warm_hours: int = 2
    memory_bucket_minutes: int = 15
    memory_token_budget: int = 4000
    memory_vector_backend: str = "memory"
    memory_chroma_path: str = "data/chroma"
    memory_vector_dim: int = 256
    memory_auto_compress: bool = True
    memory_late_seconds: int = 300

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
            memory_db_path=os.getenv("ECV_MEMORY_DB_PATH", "data/edge_mem.sqlite3"),
            memory_timezone=os.getenv("ECV_MEMORY_TIMEZONE", "Asia/Shanghai"),
            memory_hot_minutes=env_int("ECV_MEMORY_HOT_MINUTES", 10),
            memory_warm_hours=env_int("ECV_MEMORY_WARM_HOURS", 2),
            memory_bucket_minutes=env_int("ECV_MEMORY_BUCKET_MINUTES", 15),
            memory_token_budget=env_int("ECV_MEMORY_TOKEN_BUDGET", 4000),
            memory_vector_backend=os.getenv("ECV_MEMORY_VECTOR_BACKEND", "memory"),
            memory_chroma_path=os.getenv("ECV_MEMORY_CHROMA_PATH", "data/chroma"),
            memory_vector_dim=env_int("ECV_MEMORY_VECTOR_DIM", 256),
            memory_auto_compress=os.getenv("ECV_MEMORY_AUTO_COMPRESS", "1").lower()
            not in {"0", "false", "no", "off"},
            memory_late_seconds=env_int("ECV_MEMORY_LATE_SECONDS", 300),
        )

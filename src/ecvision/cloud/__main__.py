"""``python -m ecvision.cloud`` entry point."""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    uvicorn.run(
        "ecvision.cloud.main:app",
        host=os.getenv("ECV_API_HOST", "127.0.0.1"),
        port=int(os.getenv("ECV_API_PORT", "8000")),
        log_level=os.getenv("ECV_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()

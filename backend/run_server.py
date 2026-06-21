from __future__ import annotations

import traceback
from pathlib import Path

import uvicorn

from app import app


LOG_PATH = Path(__file__).with_name("server-startup.log")


def main() -> None:
    try:
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
    except BaseException:
        LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()

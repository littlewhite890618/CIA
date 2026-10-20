"""日誌設定。

開發環境輸出人類可讀格式，正式環境輸出 JSON 以便日後接入日誌收集系統。

Windows 主控台預設非 UTF-8，中文會變成亂碼，因此強制以 UTF-8 輸出。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# 這些欄位是 LogRecord 內建的，不屬於呼叫端傳入的額外脈絡
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class JsonFormatter(logging.Formatter):
    """正式環境用。每行一筆 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload["context"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """開發環境用。額外脈絡以 key=value 附在訊息後方。"""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s  %(levelname)-7s  %(name)-24s  %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            pairs = " ".join(f"{k}={v!r}" for k, v in extras.items())
            base = f"{base}  |  {pairs}"
        return base


def setup_logging(level: str = "INFO", environment: str = "development") -> None:
    """設定根 logger。應於應用啟動時呼叫一次。"""

    # Windows 主控台預設 cp950，中文會亂碼。強制 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass  # 某些執行環境不支援，略過即可

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if environment == "production" else HumanFormatter()
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 的存取日誌太吵，且與我們自己的 webhook 日誌重複
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.LoggerAdapter | logging.Logger:
    """取得具名 logger。

    用法：
        log = get_logger(__name__)
        log.info("收到訊息", extra={"igsid": "123", "persona": "sunny"})
    """
    return logging.getLogger(name)

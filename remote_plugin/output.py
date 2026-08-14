"""stdout 单行 JSON / stderr 进度契约。"""
from __future__ import annotations

import json
import sys
from typing import Any


def emit(obj: dict[str, Any]) -> None:
    """最终结果：stdout 单行 JSON。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def progress(obj: dict[str, Any]) -> None:
    """进度：stderr 单行 JSON。"""
    sys.stderr.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stderr.flush()

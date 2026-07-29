"""UTF-8 JSON 文件读写辅助。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

JsonDict = dict[str, Any]


def read_json_dict(path: Path | str) -> JsonDict:
    """读取 JSON 对象；文件缺失、损坏或根不是对象时返回空 dict。"""
    p = Path(path)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(
    path: Path | str,
    payload: Any,
    *,
    ensure_parent: bool = False,
) -> bool:
    """以 UTF-8 写入 JSON；成功返回 True。"""
    p = Path(path)
    try:
        if ensure_parent:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except (OSError, TypeError, ValueError):
        return False


def update_json_dict(
    path: Path | str,
    *,
    updates: Mapping[str, Any] | None = None,
    mutator: Callable[[JsonDict], None] | None = None,
    ensure_parent: bool = False,
) -> bool:
    """读取对象后原地更新并回写；异常时返回 False。"""
    data = read_json_dict(path)
    if updates:
        data.update(updates)
    if mutator is not None:
        mutator(data)
    return write_json(path, data, ensure_parent=ensure_parent)

"""Generate release metadata from CS2TH's canonical marketplace ID file.

This developer-only fallback never runs inside the desktop application. The
normal release workflow uses ``cs2th refresh-platform-ids --export-tools``;
this script remains useful for a local preview or a manual export.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


META_FILES = (
    Path("meta/SkinTemplate.jsonl"),
    Path("meta/SkinTemplate_st.jsonl"),
    Path("meta/SkinTemplate_mem.jsonl"),
)
PLATFORM_KEYS = {
    "buff": "buff",
    "yyyp": "youpin",
    "c5": "c5",
    "eco": "eco",
}


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _load_meta() -> list[tuple[Path, list[dict[str, Any]]]]:
    result: list[tuple[Path, list[dict[str, Any]]]] = []
    for path in META_FILES:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        result.append((path, rows))
    return result


def _load_snapshot(path: Path) -> dict[str, dict[str, int]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("ID 主库必须是以 Steam Market Hash Name 为键的 JSON 对象")
    result: dict[str, dict[str, int]] = {}
    for hash_name, item in raw.items():
        if not isinstance(item, dict):
            continue
        ids = {
            local_key: _positive_int(item.get(master_key))
            for local_key, master_key in PLATFORM_KEYS.items()
        }
        result[str(hash_name)] = {key: value for key, value in ids.items() if value}
    return result


def _merge_source(
    data: list[tuple[Path, list[dict[str, Any]]]],
    source: dict[str, dict[str, int]],
    *,
    replace: bool,
) -> Counter[str]:
    changed: Counter[str] = Counter()
    for _path, rows in data:
        for row in rows:
            for wear, hash_name in (row.get("steam") or {}).items():
                ids = source.get(str(hash_name)) or {}
                for platform, value in ids.items():
                    mapping = row.setdefault(platform, {})
                    current = _positive_int(mapping.get(wear))
                    if current == value or (current and not replace):
                        continue
                    mapping[wear] = value
                    changed[platform] += 1
    return changed


def _coverage(data: list[tuple[Path, list[dict[str, Any]]]]) -> dict[str, tuple[int, int]]:
    total = 0
    present: Counter[str] = Counter()
    for _path, rows in data:
        for row in rows:
            for wear in (row.get("steam") or {}):
                total += 1
                for platform in PLATFORM_KEYS:
                    if _positive_int((row.get(platform) or {}).get(wear)):
                        present[platform] += 1
    return {platform: (present[platform], total) for platform in PLATFORM_KEYS}


def _write_meta(data: list[tuple[Path, list[dict[str, Any]]]]) -> None:
    for path, rows in data:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 CS2TH 平台 ID 主库生成本地 SkinTemplate 发版数据"
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("D:/cs2th/data/market_platform_ids.json"),
        help="CS2TH 主库路径",
    )
    parser.add_argument("--replace", action="store_true", help="以主库正 ID 覆盖已有 ID")
    parser.add_argument("--write", action="store_true", help="确认写入三个 SkinTemplate 文件")
    args = parser.parse_args()

    data = _load_meta()
    changes = _merge_source(
        data,
        _load_snapshot(args.snapshot),
        replace=args.replace,
    )
    print("本次变更:", dict(changes) or "无")
    for platform, (present, total) in _coverage(data).items():
        print(f"{platform}: {present}/{total}，缺 {total - present}")

    if args.write:
        _write_meta(data)
        print("已写入 meta/SkinTemplate*.jsonl；重新构建后随安装包发布")
    else:
        print("当前为预览，未写文件；确认结果后追加 --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

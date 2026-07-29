"""SkinTemplate*.jsonl 与 pid2img.json：优先嵌入数据（发布），否则读 meta/（开发）。"""

from __future__ import annotations

import base64
import json
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from config import META_DIR

_META_NORMAL = "SkinTemplate.jsonl"
_META_ST = "SkinTemplate_st.jsonl"
_META_MEM = "SkinTemplate_mem.jsonl"
_MEM_PID2PID = "mem_pid2pid.json"
_WEAPON_BOX_NORMAL = "WeaponBox.jsonl"
_WEAPON_BOX_ST = "WeaponBox_st.jsonl"


@lru_cache(maxsize=1)
def _embedded_compressed_b64() -> tuple[str, str, str] | None:
    try:
        from . import _skin_template_embedded_data as d
    except ImportError:
        return None
    n = getattr(d, "NORMAL_COMPRESSED_B64", None)
    s = getattr(d, "ST_COMPRESSED_B64", None)
    m = getattr(d, "MEM_COMPRESSED_B64", None)
    if not isinstance(n, str) or not isinstance(s, str) or not isinstance(m, str) or not n or not s or not m:
        return None
    return (n, s, m)


@lru_cache(maxsize=1)
def _embedded_raw_bytes_pair() -> tuple[bytes, bytes, bytes] | None:
    pair = _embedded_compressed_b64()
    if pair is None:
        return None
    try:
        return (
            zlib.decompress(base64.b64decode(pair[0])),
            zlib.decompress(base64.b64decode(pair[1])),
            zlib.decompress(base64.b64decode(pair[2])),
        )
    except Exception:
        return None


@lru_cache(maxsize=1)
def _embedded_weapon_box_compressed_b64() -> tuple[str, ...] | None:
    """由 ``scripts/embed_skin_templates.py`` 写入的 zlib+base64（普通 + 暗金，无第三项亦可）。"""
    try:
        from . import _skin_template_embedded_data as d
    except ImportError:
        return None
    n = getattr(d, "WEAPONBOX_NORMAL_COMPRESSED_B64", None)
    s = getattr(d, "WEAPONBOX_ST_COMPRESSED_B64", None)
    if not isinstance(n, str) or not isinstance(s, str) or not n or not s:
        return None
    m = getattr(d, "WEAPONBOX_MEM_COMPRESSED_B64", None)
    if isinstance(m, str) and m:
        return (n, s, m)
    return (n, s)


@lru_cache(maxsize=1)
def _embedded_weapon_box_raw_pair() -> tuple[bytes, bytes] | None:
    pair = _embedded_weapon_box_compressed_b64()
    if pair is None:
        return None
    try:
        blobs = [
            zlib.decompress(base64.b64decode(b64))
            for b64 in pair[:2]
        ]
        return (blobs[0], blobs[1])
    except Exception:
        return None


def weapon_box_jsonl_text(*, st: bool) -> str:
    """武器箱 jsonl 全文：优先嵌入式（zlib），否则 ``meta/WeaponBox(_st).jsonl`` 明文。"""
    emb = _embedded_weapon_box_raw_pair()
    if emb is not None:
        blob = emb[1] if st else emb[0]
        return blob.decode("utf-8")
    path = META_DIR / (_WEAPON_BOX_ST if st else _WEAPON_BOX_NORMAL)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def iter_meta_skin_lines_in_order() -> Iterator[str]:
    """先普通模板各行，再暗金模板各行（与原先双文件顺序一致）。"""
    emb = _embedded_raw_bytes_pair()
    if emb is not None:
        for blob in emb:
            for line in blob.decode("utf-8").splitlines():
                s = line.strip()
                if s:
                    yield s
        return
    for fname in (_META_NORMAL, _META_ST, _META_MEM):
        path: Path = META_DIR / fname
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    yield s


def _iter_jsonl_dicts_from_text(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def iter_skin_normal_json_objects() -> Iterator[dict]:
    emb = _embedded_raw_bytes_pair()
    if emb is not None:
        yield from _iter_jsonl_dicts_from_text(emb[0].decode("utf-8"))
        return
    path = META_DIR / _META_NORMAL
    if path.is_file():
        try:
            yield from _iter_jsonl_dicts_from_text(path.read_text(encoding="utf-8"))
        except OSError:
            return


def iter_skin_st_json_objects() -> Iterator[dict]:
    emb = _embedded_raw_bytes_pair()
    if emb is not None:
        yield from _iter_jsonl_dicts_from_text(emb[1].decode("utf-8"))
        return
    path = META_DIR / _META_ST
    if path.is_file():
        try:
            yield from _iter_jsonl_dicts_from_text(path.read_text(encoding="utf-8"))
        except OSError:
            return

def iter_skin_mem_json_objects() -> Iterator[dict]:
    emb = _embedded_raw_bytes_pair()
    if emb is not None:
        yield from _iter_jsonl_dicts_from_text(emb[2].decode("utf-8"))
        return
    path = META_DIR / _META_MEM
    if path.is_file():
        try:
            yield from _iter_jsonl_dicts_from_text(path.read_text(encoding="utf-8"))
        except OSError:
            return


@lru_cache(maxsize=1)
def _embedded_pid2img_compressed_b64() -> str | None:
    try:
        from . import _skin_template_embedded_data as d
    except ImportError:
        return None
    p = getattr(d, "PID2IMG_COMPRESSED_B64", None)
    if not isinstance(p, str) or not p:
        return None
    return p


@lru_cache(maxsize=1)
def _embedded_mem_pid2pid_compressed_b64() -> str | None:
    try:
        from . import _skin_template_embedded_data as d
    except ImportError:
        return None
    p = getattr(d, "MEM_PID2PID_COMPRESSED_B64", None)
    if not isinstance(p, str) or not p:
        p = getattr(d, "PIDS_MEM2NORMAL_COMPRESSED_B64", None)
    if not isinstance(p, str) or not p:
        return None
    return p


def _normalize_mem_pid2pid_dict(obj: object) -> dict[str, str]:
    if not isinstance(obj, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in obj.items()
        if k is not None and v is not None
    }


@lru_cache(maxsize=1)
def get_mem_pid2pid_dict() -> dict[str, str]:
    """纪念品 paint_index（如 ``*379``）→ pid2img 键（如 ``379`` 或 ``46000000``）。"""
    b64 = _embedded_mem_pid2pid_compressed_b64()
    if b64 is not None:
        try:
            raw = zlib.decompress(base64.b64decode(b64))
            return _normalize_mem_pid2pid_dict(json.loads(raw.decode("utf-8")))
        except Exception:
            pass
    path = META_DIR / _MEM_PID2PID
    if not path.is_file():
        return {}
    try:
        return _normalize_mem_pid2pid_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def paint_index_key_for_pid2img(paint_index: str, *, stat_trak: bool = False) -> str:
    """查 pid2img 前规范化：暗金去负号；``*`` 开头 pid 经 ``mem_pid2pid.json`` 映射后再查图。"""
    s = (paint_index or "").strip()
    if stat_trak and s.startswith("-"):
        s = s[1:].strip() or s
    if s.startswith("*"):
        mapped = get_mem_pid2pid_dict().get(s)
        if mapped is not None:
            ms = str(mapped).strip()
            if ms:
                return ms
        if len(s) > 1:
            return s[1:].strip() or s
    return s


@lru_cache(maxsize=1)
def get_pid2img_dict() -> dict[str, str]:
    """paint_index 字符串 → weapon_images 下 webp 文件名（与 inventory_icons 原逻辑一致）。"""
    b64 = _embedded_pid2img_compressed_b64()
    if b64 is not None:
        try:
            raw = zlib.decompress(base64.b64decode(b64))
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict):
                return {
                    str(k): str(v).replace(".png", ".webp")
                    for k, v in obj.items()
                    if v is not None
                }
        except Exception:
            pass
    path = META_DIR / "pid2img.json"
    if path.is_file():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return {
                    str(k): str(v).replace(".png", ".webp")
                    for k, v in obj.items()
                    if v is not None
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return {}


def clear_embedded_cache() -> None:
    """供测试或热重载嵌入数据后调用（一般不需要）。"""
    _embedded_compressed_b64.cache_clear()
    _embedded_raw_bytes_pair.cache_clear()
    _embedded_pid2img_compressed_b64.cache_clear()
    _embedded_mem_pid2pid_compressed_b64.cache_clear()
    get_mem_pid2pid_dict.cache_clear()
    get_pid2img_dict.cache_clear()
    _embedded_weapon_box_compressed_b64.cache_clear()
    _embedded_weapon_box_raw_pair.cache_clear()

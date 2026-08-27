"""Desktop client update metadata from cs2th.cn."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DEFAULT_DOWNLOAD_URL = "https://cs2th.cn/tradeup-assistant"


@dataclass(frozen=True, slots=True)
class ClientUpdateInfo:
    latest_version: str
    download_url: str = DEFAULT_DOWNLOAD_URL


def version_tuple(version: str) -> tuple[int, ...]:
  parts: list[int] = []
  for piece in re.split(r"[.\-+]", str(version or "").strip()):
      if piece.isdigit():
          parts.append(int(piece))
      elif parts:
          break
  return tuple(parts) if parts else (0,)


def is_version_older(current: str, latest: str) -> bool:
    return version_tuple(current) < version_tuple(latest)


def parse_client_update(payload: dict[str, Any]) -> ClientUpdateInfo | None:
    block = payload.get("client_update")
    if isinstance(block, dict):
        latest_version = str(block.get("latest_version") or "").strip()
        if not latest_version:
            return None
        download_url = str(block.get("download_url") or DEFAULT_DOWNLOAD_URL).strip()
        return ClientUpdateInfo(
            latest_version=latest_version,
            download_url=download_url or DEFAULT_DOWNLOAD_URL,
        )
    latest_version = str(payload.get("latest_version") or "").strip()
    if not latest_version:
        return None
    download_url = str(payload.get("download_url") or DEFAULT_DOWNLOAD_URL).strip()
    return ClientUpdateInfo(
        latest_version=latest_version,
        download_url=download_url or DEFAULT_DOWNLOAD_URL,
    )

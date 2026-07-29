"""Build the CS2TH Tools alchemy-price cache from a CS2TH SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fallback", type=Path)
    args = parser.parse_args()

    sys.path.insert(0, str(args.tools_root.resolve()))
    from core.product_price_sync import (
        build_product_price_payload,
        merge_missing_groups,
        write_product_price_payload,
    )

    payload = build_product_price_payload(args.snapshot)
    merge_missing_groups(payload, args.fallback)
    write_product_price_payload(payload, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "bytes": args.output.stat().st_size,
                "fetch_time": payload["fetch_time"],
                "snapshot_synced_at": payload["snapshot_synced_at"],
                "snapshot_item_count": payload["snapshot_item_count"],
                "ordinary_boxes": len(payload["ordinary"]),
                "stat_trak_boxes": len(payload["stat_trak"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

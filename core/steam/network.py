"""网络相关工具。"""

import re


def is_network_error(exc: Exception) -> bool:
    msg = str(getattr(exc, "message", "")) + str(exc)
    return bool(
        re.search(
            r"fetch failed|ConnectTimeoutError|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|"
            r"ENETUNREACH|UND_ERR_CONNECT",
            msg,
            re.I,
        )
    )

"""Steam 侧数据结构。"""

from dataclasses import dataclass


@dataclass
class SteamWebProfile:
    """从 Steam 社区个人页解析的当前登录账号信息。"""

    steam_id: str
    personaname: str = ""
    avatar_url: str = ""
    avatar_local_path: str = ""

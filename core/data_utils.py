"""皮肤数据模型 - SkinTemplate、SkinInstance 及常量"""

import bisect
import struct
from dataclasses import dataclass, field
import numpy as np

LARGE_VALUE_LIST = [0.07, 0.15, 0.38, 0.45, 1.0]


def _list_or_singleton(v) -> list:
    return v if isinstance(v, list) else [v]


MID_VALUE_LIST = [
    0,
    0.01,
    0.02,
    0.03,
    0.04,
    0.07,
    0.08,
    0.09,
    0.1,
    0.11,
    0.15,
    0.18,
    0.21,
    0.24,
    0.27,
    0.38,
    0.39,
    0.4,
    0.41,
    0.42,
    0.45,
    0.5,
    0.63,
    0.76,
    0.9,
    1.0,
]


def get_interval_value(float_value: float) -> tuple[float, float]:
    """将浮点磨损映射到相邻两档 MID 锚点 (lower, upper)；与 ContractOptimizer 一致。"""
    upper_index = bisect.bisect_left(MID_VALUE_LIST, float_value)
    upper_index = max(upper_index, 1)
    if upper_index >= len(MID_VALUE_LIST):
        upper_index = len(MID_VALUE_LIST) - 1
    lower_index = upper_index - 1
    upper_value = MID_VALUE_LIST[upper_index]
    lower_value = MID_VALUE_LIST[lower_index]
    return lower_value, upper_value


APPEARANCE = ["崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累"]
APPEARANCE_MAP = {
    "Factory New": "崭新出厂",
    "Minimal Wear": "略有磨损",
    "Field-Tested": "久经沙场",
    "Well-Worn": "破损不堪",
    "Battle-Scarred": "战痕累累",
}

# 与库存页磨损刻度五色条一致，顺序对应 float 从低到高（同 LARGE_VALUE_LIST / APPEARANCE）
WEAR_ZONE_COLORS = ("#008000", "#5cb85c", "#f0ad4e", "#d9534f", "#993A38")
# 库存徽章文字统一白色
INVENTORY_WEAR_BADGE_TEXT_COLOR = "#ffffff"


def wear_zone_index(float_val: float | None) -> int | None:
    """0..4 磨损区间索引；无 float 为 None。与 get_wear_level / 刻度条分区一致。"""
    if float_val is None:
        return None
    fv = max(0.0, min(1.0, float(float_val)))
    idx = bisect.bisect_right(LARGE_VALUE_LIST, fv)
    if idx >= len(APPEARANCE):
        idx = len(APPEARANCE) - 1
    return idx


def inventory_wear_chinese(item: dict) -> str | None:
    """库存 dict：有 float 时与刻度一致用 APPEARANCE；否则用 wear 英文字段经 APPEARANCE_MAP。"""
    zi = wear_zone_index(item.get("float"))
    if zi is not None:
        return APPEARANCE[zi]
    w = (item.get("wear") or "").strip()
    if w in APPEARANCE_MAP:
        return APPEARANCE_MAP[w]
    if w in APPEARANCE:
        return w
    return None


def wear_as_float32(v: float | int) -> float:
    """将磨损按 IEEE754 binary32 舍入（与游戏内 float 一致）。"""
    x = float(v)
    return struct.unpack(">f", struct.pack(">f", x))[0]


QUALITY_MAP = {
    "消费级": "Consumer",
    "工业级": "Industrial",
    "军规级": "MilSpec",
    "受限": "Restricted",
    "保密": "Classified",
    "隐秘": "Covert",
    "非凡": "Extraordinary",
}
UPPER_QUALITY_MAP = {
    "消费级": "工业级",
    "工业级": "军规级",
    "军规级": "受限",
    "受限": "保密",
    "保密": "隐秘",
    "隐秘": "非凡",
}

# CS 品质标签颜色（参照游戏内品质色，用于 UI 展示）
QUALITY_COLORS = {
    "消费级": ("#b0c3d9", "#4a5568"),
    "工业级": ("#5e98d9", "#ffffff"),
    "军规级": ("#4b69ff", "#ffffff"),
    "受限": ("#8847ff", "#ffffff"),
    "保密": ("#d32ce6", "#ffffff"),
    "隐秘": ("#eb4b4b", "#ffffff"),
    "非凡": ("#e4ae39", "#ffffff"),
}


@dataclass(slots=True)
class SkinTemplate:
    paint_index: str
    weapon_name: str
    skin_name: str
    quality: str
    stat_trak: bool
    weapon_box_id: list[int] = field(default_factory=list)
    weapon_box_name: list[str] = field(default_factory=list)
    min_float: float = 0.0
    max_float: float = 1.0
    upper_skins: list = field(default_factory=list)
    lower_skins: list = field(default_factory=list)
    # meta SkinTemplate*.jsonl：外观（中文）→ 各平台 goods_id / Steam 英名
    steam: dict[str, str] = field(default_factory=dict)
    buff: dict[str, int] = field(default_factory=dict)
    yyyp: dict[str, int] = field(default_factory=dict)
    c5: dict[str, int] = field(default_factory=dict)
    eco: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        assert 0.0 <= self.min_float <= self.max_float <= 1.0, f"磨损度范围错误: {self.min_float}~{self.max_float}"
        assert self.quality in {"消费级", "工业级", "军规级", "受限", "保密", "隐秘", "非凡"}, f"品质错误: {self.quality}"

    def __hash__(self) -> int:
        return hash(self.paint_index)

    def __eq__(self, other) -> bool:
        if not isinstance(other, SkinTemplate):
            return False
        return self.paint_index == other.paint_index

    @staticmethod
    def float_to_normalized(float_value: float, min_float: float, max_float: float) -> float:
        span = max_float - min_float
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (float_value - min_float) / span))

    @staticmethod
    def normalized_to_float(normalized_value: float, min_float: float, max_float: float) -> float:
        n = max(0.0, min(1.0, normalized_value))
        return min_float + n * (max_float - min_float)

    @classmethod
    def from_dict(cls, data: dict):
        return SkinTemplate(
            paint_index=str(data["paint_index"]),
            weapon_name=data["weapon_name"],
            skin_name=data["skin_name"],
            quality=data["quality"],
            weapon_box_id=_list_or_singleton(data["weapon_box_id"]),
            weapon_box_name=_list_or_singleton(data["weapon_box_name"]),
            min_float=float(data["min_float"]),
            max_float=float(data["max_float"]),
            upper_skins=data["upper_skins"],
            lower_skins=data["lower_skins"],
            stat_trak=data["stat_trak"],
            steam=data['steam'],
            buff=data['buff'],
            yyyp=data['yyyp'],
            c5=data['c5'],
            eco=data['eco'],
        )


def is_star_knife_skin_template(tpl: SkinTemplate) -> bool:
    """★ 近战刀具（不含手套、裹手）。此类在 meta 中非暗金多为「非凡」，暗金条目常被标成「隐秘」。"""
    w = (tpl.weapon_name or "").strip()
    if "（★StatTrak™）" not in w and "（★）" not in w:
        return False
    if "手套" in w or "裹手" in w:
        return False
    return True


def tradeup_display_quality(tpl: SkinTemplate) -> str:
    """炼金模拟等 UI 用品质：★ 刀具在暗金 meta 为「隐秘」时按「非凡」展示，与游戏层级一致。"""
    if is_star_knife_skin_template(tpl) and tpl.quality == "隐秘":
        return "非凡"
    return tpl.quality


@dataclass(slots=True)
class SkinInstance:
    skin_template: SkinTemplate
    float_value: float = None
    price: float = None
    expectation: float = field(init=False, default=0.0)
    appearance: str = None
    normalized_value: float = None
    platform: str = "buff"
    purchase_link: str | None = None
    value: float = field(init=False, default=0.0)
    name: str = field(init=False, default="")
    uuid: int = field(init=False, default=-1)
    dominanced: int = field(init=False, default=0)

    def __post_init__(self):
        if self.float_value is None and self.normalized_value is None:
            raise ValueError("磨损度或归一化磨损度必须提供一个")

        if self.normalized_value is None:
            self.float_value = np.float32(self.float_value)
            min_float = self.skin_template.min_float
            max_float = self.skin_template.max_float
            assert min_float <= self.float_value <= max_float, f"磨损度{self.float_value}不在{min_float}~{max_float}区间"
            normalized_value = SkinTemplate.float_to_normalized(self.float_value, min_float, max_float)
            self.normalized_value = normalized_value

        if self.float_value is None:
            self.normalized_value = np.float32(self.normalized_value)
            self.float_value = SkinTemplate.normalized_to_float(self.normalized_value, self.skin_template.min_float, self.skin_template.max_float)

        if self.appearance is None:
            self.appearance = self.get_appearance(self.float_value)

        if self.skin_template.skin_name == "":
            self.name = f"{self.skin_template.weapon_name}"
        else:
            self.name = f"{self.skin_template.weapon_name} | {self.skin_template.skin_name}（{self.appearance}）"

        self.uuid = self.__hash__()

    def __hash__(self) -> int:
        return hash((self.skin_template.paint_index, self.normalized_value, self.skin_template.stat_trak))

    def __eq__(self, other) -> bool:
        if not isinstance(other, SkinInstance):
            return False
        return self.uuid == other.uuid

    @staticmethod
    def get_appearance(float_value):
        idx = bisect.bisect_right(LARGE_VALUE_LIST, float_value)
        if 0 <= idx < len(LARGE_VALUE_LIST):
            return APPEARANCE[idx]
        return None

    def apply_rate(self, rate):
        self.value = self.expectation - self.price * rate

"""炼金页面 - 读取 JSONL 文件，按 goods_name 聚合展示"""

import time
import json
import logging
import math
import hashlib

import numpy as np
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QFrame,
    QScrollArea, QLabel, QSizePolicy, QLayout,
    QStackedWidget, QButtonGroup, QToolTip, QProgressBar, QDialog,
    QSpinBox, QCheckBox, QInputDialog,
)
from PySide6.QtCore import Qt, QEvent, QTimer, QObject, Signal
from PySide6.QtGui import QFont, QFontMetrics, QCursor, QPalette, QShowEvent

from ui.app_settings import (
    load_alchemy_step2_wear_ui,
    load_alchemy_recipe_save_mode,
    load_alchemy_wear_step2_notice_dismissed,
    save_alchemy_recipe_save_mode,
    save_alchemy_step2_wear_ui,
    save_last_recipe_save_folder_id,
)
from ui.feedback import ask_confirmation, show_alert
from ui.dialogs.alert_dialog import WearInputNoticeDialog
from ui.dialogs.exclude_saved_recipes_dialog import ExcludeSavedRecipesDialog
from ui.dialogs.import_steam_inventory_dialog import ImportSteamInventoryDialog
from ui.dialogs.custom_alchemy_item_dialog import CustomAlchemyItemDialog
from ui.dialogs.move_recipe_folder_dialog import (
    MoveRecipeFolderDialog,
    build_all_recipe_folder_pick_targets,
)
from ui.icons import load_svg_icon
from ui.pages.alchemy_modes import AlchemyModeMixin
from ui.qt_workers import worker_is_running
from ui.widgets.collapsible_group import (
    CollapsibleGroup,
    substrate_identity_key,
    substrate_row_lookup_key,
    substrate_slot_lookup_key,
)
from ui.widgets.float_line_edit import WearFloatLineEditWithIeee
from ui.widgets.calc_setting_product_group import CalcSettingProductGroup
from ui.widgets.segmented_switch import SegmentedCheckSwitch
from ui.widgets.purchase_qr_label import QrSlot
from ui.widgets.recipe_result_group import RecipeResultGroup
from ui.widgets.toast import show_toast

from config import (
    ALCHEMY_ICON_PATH,
    ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS,
    COLLECTED_JSON_DIR,
    CONTENT_PAGE_LAYOUT_MARGINS,
)
from ui.dialogs.wide_text_input_dialog import get_wide_text_input
from core.alchemy_quality import (
    canonical_goods_name_for_lookup,
    get_quality_from_goods_name,
    get_template_from_goods_name,
    resolve_inventory_skin_template,
    strip_appearance_suffix_from_goods_name,
)
from core.alchemy_calc import (
    lookup_inventory_item_price_value,
    lookup_template_price_value,
    partition_selected_data_by_tradeup_group,
    try_build_product_price_map_from_disk,
)
from core.data_utils import SkinInstance, SkinTemplate, inventory_wear_chinese
from core.inventory_steam_accounts import (
    combo_display_name_for_profile,
    get_active_profile_id,
    list_profile_entries,
    load_steam_account_config_dict,
    profile_inventory_data_path,
)
from core.purchase_tracking import load_profile_inventory_items
from core.purchase_batches import (
    add_recipe_to_purchase_batch,
    create_purchase_batch,
    list_purchase_batches,
    purchase_batch_summary,
)
from core.saved_recipes import (
    SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY,
    SUBSTRATE_ALCHEMY_META_LOCKED_KEY,
    default_save_recipe_dialog_title,
    list_saved_recipes,
    save_recipe_file,
)
from ui.workers.alchemy_workers import FetchPriceWorker

if TYPE_CHECKING:
    from ui.workers.alchemy_workers import (
        CalcProcessRunner,
        SpecialWearCalcRunner,
    )

REQUIRED_KEYS = frozenset({"float_value", "goods_id", "goods_name", "platform", "price"})

# 与 CollapsibleGroup 中「库存」组标签一致；用于合并导入时识别非库存行
_INVENTORY_PLATFORMS = frozenset({"inventory", "steam_inventory"})
_NEW_PURCHASE_BATCH_LABEL = "＋ 新建采购批次"
logger = logging.getLogger(__name__)


def _recipe_exclude_float_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-10)


def _recipe_exclude_template_key(tpl: SkinTemplate) -> tuple[str, bool]:
    return (str(tpl.paint_index), tpl.stat_trak)




class NextStepButton(QPushButton):
    """下一步按钮"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._file_loaded = False

    def set_file_loaded(self, loaded: bool):
        self._file_loaded = loaded


_SELECT_FILE_HELP_TIP = (
    "上传格式：JSONL 或 JSON。\n"
    "JSONL：每行一件饰品；JSON：可为对象数组，或单件对象。\n"
    "必填字段：float_value、goods_id、goods_name、platform、price。\n\n"
    "示例：\n"
    '{"float_value":0.123456,"goods_id":"12345",'
    '"goods_name":"AK-47 | 夜愿（略有磨损）",'
    '"platform":"buff","price":128.50}'
)

# 底物组列表：品级从高到低
_SUBSTRATE_QUALITY_SORT_RANK: dict[str, int] = {
    "非凡": 6,
    "隐秘": 5,
    "保密": 4,
    "受限": 3,
    "军规级": 2,
    "工业级": 1,
    "消费级": 0,
}


def _substrate_group_sort_key(goods_name: str) -> tuple:
    quality = get_quality_from_goods_name(goods_name) or ""
    rank = _SUBSTRATE_QUALITY_SORT_RANK.get(quality)
    # 未知品质排最后，同品级按名称
    if rank is None:
        return (1, 0, goods_name)
    return (0, -rank, goods_name)


class AlchemyPage(AlchemyModeMixin, QWidget):
    """炼金页面 - 右上角选择文件按钮，按 goods_name 聚合展示"""

    navigation_route_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("alchemyPage")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        main_layout.setSpacing(16)

        self.step_stack = QStackedWidget()
        self.step_stack.setObjectName("alchemyStepStack")

        # 步骤1：标题 + 选择文件 + 清除文件 + 下一步 + 数据列表
        self.step1_widget = QWidget()
        self.step1_widget.setObjectName("alchemyPage")
        step1_layout = QVBoxLayout(self.step1_widget)
        step1_layout.setContentsMargins(0, 0, 0, 0)
        step1_layout.setSpacing(16)

        step1_top = QHBoxLayout()
        step1_top.setSpacing(16)
        self._step1_title_icon = QLabel(self)
        self._step1_title_icon.setObjectName("contentPageTitleIcon")
        self._step1_title_icon.setFixedSize(28, 28)
        self._step1_title_icon.setAlignment(Qt.AlignCenter)
        step1_top.addWidget(self._step1_title_icon, 0, Qt.AlignVCenter)
        title_label = QLabel("底物数据选择")
        title_label.setObjectName("alchemyPageTitle")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_label.setFont(title_font)
        step1_top.addWidget(title_label, 0, Qt.AlignVCenter)
        self.step1_hint_label = QLabel("上传文件或导入 Steam 库存，并勾选要参与计算的数据")
        self.step1_hint_label.setObjectName("alchemyStep1Hint")
        step1_top.addWidget(self.step1_hint_label, 0, Qt.AlignVCenter)
        step1_top.addStretch(1)

        self.select_file_btn = QPushButton("选择文件")
        self.select_file_btn.setObjectName("alchemySelectFileBtn")
        self.select_file_btn.setCursor(Qt.PointingHandCursor)
        self.select_file_btn.setToolTip(_SELECT_FILE_HELP_TIP)
        self.select_file_btn.clicked.connect(self._on_select_file)
        step1_top.addWidget(self.select_file_btn, 0, Qt.AlignVCenter)

        self.import_inventory_btn = QPushButton("导入库存")
        self.import_inventory_btn.setObjectName("alchemySelectFileBtn")
        self.import_inventory_btn.setCursor(Qt.PointingHandCursor)
        self.import_inventory_btn.setToolTip("一键导入所选 Steam 账号本地缓存的全部库存")
        self.import_inventory_btn.clicked.connect(self._on_import_steam_inventory)
        step1_top.addWidget(self.import_inventory_btn, 0, Qt.AlignVCenter)

        self.custom_item_btn = QPushButton("自定义饰品")
        self.custom_item_btn.setObjectName("alchemySelectFileBtn")
        self.custom_item_btn.setCursor(Qt.PointingHandCursor)
        self.custom_item_btn.setToolTip(
            "按收藏品 / 武器箱选择饰品，并自定义数量和磨损区间"
        )
        self.custom_item_btn.clicked.connect(self._on_add_custom_items)
        step1_top.addWidget(self.custom_item_btn, 0, Qt.AlignVCenter)

        self.clear_file_btn = QPushButton("清除数据")
        self.clear_file_btn.setObjectName("alchemyClearFileBtn")
        self.clear_file_btn.setCursor(Qt.PointingHandCursor)
        self.clear_file_btn.clicked.connect(self._on_clear_file)
        step1_top.addWidget(self.clear_file_btn, 0, Qt.AlignVCenter)

        self.exclude_recipe_btn = QPushButton("配方数据")
        self.exclude_recipe_btn.setObjectName("alchemyExcludeRecipeBtn")
        self.exclude_recipe_btn.setCursor(Qt.PointingHandCursor)
        self.exclude_recipe_btn.clicked.connect(self._on_recipe_data_clicked)
        step1_top.addWidget(self.exclude_recipe_btn, 0, Qt.AlignVCenter)

        self.next_btn = NextStepButton(self)
        self.next_btn.setText("下一步")
        self.next_btn.setObjectName("alchemyNextBtn")
        self.next_btn.set_file_loaded(False)
        self.next_btn.clicked.connect(self._on_next)
        step1_top.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        step1_layout.addLayout(step1_top)

        step1_count_card = QFrame()
        step1_count_card.setObjectName("alchemyStep1CountCard")
        step1_count_card.setAttribute(Qt.WA_StyledBackground)
        count_layout = QHBoxLayout(step1_count_card)
        count_layout.setContentsMargins(16, 10, 16, 10)
        self.step1_count_label = QLabel("已选择 0 种皮肤，0 条数据，其中包含 0 个必选底物")
        self.step1_count_label.setObjectName("alchemyStep1Count")
        count_layout.addWidget(self.step1_count_label, 0, Qt.AlignVCenter)
        step1_layout.addWidget(step1_count_card, 0, Qt.AlignLeft)

        # 滚动区域：放置聚合后的可展开组
        scroll = QScrollArea()
        scroll.setObjectName("alchemyScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setAttribute(Qt.WA_StyledBackground)

        self.groups_container = QWidget()
        self.groups_container.setObjectName("alchemyGroupsContainer")
        self.groups_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.groups_layout = QVBoxLayout(self.groups_container)
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(8)
        self.groups_layout.setSizeConstraint(QLayout.SetMinimumSize)

        scroll.setWidget(self.groups_container)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        step1_layout.addWidget(scroll, 1)
        self.step_stack.addWidget(self.step1_widget)

        # 步骤2：产物磨损
        self.step2_widget = QWidget()
        self.step2_widget.setObjectName("alchemyPage")
        step2_layout = QVBoxLayout(self.step2_widget)
        step2_layout.setContentsMargins(0, 0, 0, 0)
        step2_layout.setSpacing(16)

        step2_top = QHBoxLayout()
        step2_top.setSpacing(16)
        self._step2_title_icon = QLabel(self)
        self._step2_title_icon.setObjectName("contentPageTitleIcon")
        self._step2_title_icon.setFixedSize(28, 28)
        self._step2_title_icon.setAlignment(Qt.AlignCenter)
        step2_top.addWidget(self._step2_title_icon, 0, Qt.AlignVCenter)
        step2_title = QLabel("产物磨损设置")
        step2_title.setObjectName("alchemyPageTitle")
        step2_title.setFont(title_font)
        step2_top.addWidget(step2_title, 0, Qt.AlignVCenter)
        self.step2_wear_notice_btn = QPushButton("使用须知")
        self.step2_wear_notice_btn.setObjectName("alchemyStep2WearNoticeBtn")
        self.step2_wear_notice_btn.setCursor(Qt.PointingHandCursor)
        self.step2_wear_notice_btn.setFlat(True)
        self.step2_wear_notice_btn.setAutoDefault(False)
        self.step2_wear_notice_btn.setDefault(False)
        self.step2_wear_notice_btn.clicked.connect(self._on_step2_wear_notice_clicked)
        step2_top.addWidget(self.step2_wear_notice_btn, 0, Qt.AlignVCenter)
        step2_top.addStretch(1)
        self.step2_mode_row = QWidget()
        self.step2_mode_row.setObjectName("alchemyStep2ModeRow")
        step2_mode_row_layout = QHBoxLayout(self.step2_mode_row)
        step2_mode_row_layout.setContentsMargins(0, 0, 0, 0)
        step2_mode_row_layout.setSpacing(6)
        self.step2_mode_hint_icon = QLabel("ⓘ")
        self.step2_mode_hint_icon.setObjectName("alchemyStep2ModeHint")
        self._step2_mode_tooltip_text = (
            "扫描模式：在给定磨损度范围内找到最大收益率配方\n"
            "目标模式：仅在目标磨损度下找到最大收益率配方\n"
            "特殊磨损：在给定产物磨损度范围内，找到最低成本配方"
        )
        self.step2_mode_hint_icon.setCursor(Qt.WhatsThisCursor)
        step2_mode_row_layout.addWidget(self.step2_mode_hint_icon, 0, Qt.AlignVCenter)
        self.step2_mode_row.installEventFilter(self)
        step2_top.addWidget(self.step2_mode_row, 0, Qt.AlignVCenter)
        self.step2_mode_container = SegmentedCheckSwitch(
            container_object_name="alchemyStep2ModeSegmented",
            slider_object_name="alchemyStep2ModeSlider",
            segments=(
                ("alchemySegmentLeft", "扫描模式"),
                ("alchemySegmentMiddle", "目标模式"),
                ("alchemySegmentRight", "特殊磨损"),
            ),
        )
        self.step2_mode_scan_btn = self.step2_mode_container.buttons[0]
        self.step2_mode_target_btn = self.step2_mode_container.buttons[1]
        self.step2_mode_special_btn = self.step2_mode_container.buttons[2]
        self.step2_mode_group = QButtonGroup(self)
        self.step2_mode_group.addButton(self.step2_mode_scan_btn)
        self.step2_mode_group.addButton(self.step2_mode_target_btn)
        self.step2_mode_group.addButton(self.step2_mode_special_btn)
        self.step2_mode_group.setExclusive(True)
        self.step2_mode_scan_btn.clicked.connect(self._on_step2_mode_changed)
        self.step2_mode_target_btn.clicked.connect(self._on_step2_mode_changed)
        self.step2_mode_special_btn.clicked.connect(self._on_step2_mode_changed)
        step2_top.addWidget(self.step2_mode_container, 0, Qt.AlignVCenter)
        self.step2_reset_wear_btn = QPushButton("重置磨损度")
        self.step2_reset_wear_btn.setObjectName("alchemyResetWearBtn")
        self.step2_reset_wear_btn.setCursor(Qt.PointingHandCursor)
        self.step2_reset_wear_btn.clicked.connect(self._on_step2_reset_wear)
        step2_top.addWidget(self.step2_reset_wear_btn, 0, Qt.AlignVCenter)
        self.step2_prev_btn = QPushButton("上一步")
        self.step2_prev_btn.setObjectName("alchemyClearFileBtn")
        self.step2_prev_btn.setCursor(Qt.PointingHandCursor)
        self.step2_prev_btn.clicked.connect(self._on_step2_prev)
        step2_top.addWidget(self.step2_prev_btn, 0, Qt.AlignVCenter)
        self.step2_next_btn = QPushButton("下一步")
        self.step2_next_btn.setObjectName("alchemyNextBtn")
        self.step2_next_btn.setCursor(Qt.PointingHandCursor)
        self.step2_next_btn.clicked.connect(self._on_step2_next)
        step2_top.addWidget(self.step2_next_btn, 0, Qt.AlignVCenter)
        step2_layout.addLayout(step2_top)

        self.step2_norm_card = QFrame()
        self.step2_norm_card.setObjectName("alchemyStep2NormCard")
        self.step2_norm_card.setAttribute(Qt.WA_StyledBackground)
        self.step2_norm_layout = QHBoxLayout(self.step2_norm_card)
        self.step2_norm_layout.setContentsMargins(16, 12, 16, 12)
        self.step2_norm_layout.setSpacing(20)
        self.step2_norm_min_label = QLabel("最小归一化磨损度")
        self.step2_norm_min_label.setObjectName("alchemyStep2NormLabel")
        self.step2_norm_min_edit = WearFloatLineEditWithIeee(0.0, 1.0, self.step2_norm_card)
        self.step2_norm_min_edit.line_edit().setObjectName("alchemyStep2NormEdit")
        self.step2_norm_min_edit.setFixedWidth(140)
        self.step2_norm_max_label = QLabel("最大归一化磨损度")
        self.step2_norm_max_label.setObjectName("alchemyStep2NormLabel")
        self.step2_norm_max_edit = WearFloatLineEditWithIeee(0.0, 1.0, self.step2_norm_card)
        self.step2_norm_max_edit.line_edit().setObjectName("alchemyStep2NormEdit")
        self.step2_norm_max_edit.setValue(1.0)
        self.step2_norm_max_edit.setFixedWidth(140)
        self.step2_norm_target_label = QLabel("归一化目标磨损度")
        self.step2_norm_target_label.setObjectName("alchemyStep2NormLabel")
        self.step2_norm_target_edit = WearFloatLineEditWithIeee(0.0, 1.0, self.step2_norm_card)
        self.step2_norm_target_edit.line_edit().setObjectName("alchemyStep2NormEdit")
        self.step2_norm_target_edit.setValue(1.0)
        self.step2_norm_target_edit.setFixedWidth(140)
        self.step2_norm_layout.addWidget(self.step2_norm_min_label, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addWidget(self.step2_norm_min_edit, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addWidget(self.step2_norm_max_label, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addWidget(self.step2_norm_max_edit, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addWidget(self.step2_norm_target_label, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addWidget(self.step2_norm_target_edit, 0, Qt.AlignVCenter)
        self.step2_norm_layout.addStretch(1)
        step2_layout.addWidget(self.step2_norm_card, 0, Qt.AlignLeft)

        self.step2_special_card = QFrame()
        self.step2_special_card.setObjectName("alchemyStep2NormCard")
        self.step2_special_card.setAttribute(Qt.WA_StyledBackground)
        special_layout = QHBoxLayout(self.step2_special_card)
        special_layout.setContentsMargins(16, 12, 16, 12)
        special_layout.setSpacing(16)
        self.step2_special_hint_label = QLabel(
            "选择目标产物并在右侧输入目标磨损，范围会自动推导"
        )
        self.step2_special_hint_label.setObjectName("alchemyStep1Hint")
        self.step2_special_hint_label.setWordWrap(False)
        self.step2_special_rounds_label = QLabel("搜索轮数")
        self.step2_special_rounds_label.setObjectName("alchemyStep2NormLabel")
        self.step2_special_rounds_spin = QSpinBox(self.step2_special_card)
        self.step2_special_rounds_spin.setObjectName("alchemyStep2NormSpin")
        self.step2_special_rounds_spin.setRange(1, 50)
        self.step2_special_rounds_spin.setValue(ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS)
        _sw_fm = QFontMetrics(self.step2_special_rounds_spin.font())
        # pages_alchemy.qss：左右 padding 12+12、右侧内嵌按钮区约 34px
        self.step2_special_rounds_spin.setFixedWidth(
            12 + 12 + 34 + _sw_fm.horizontalAdvance("99") + 10
        )
        self.step2_special_rounds_spin.setToolTip(
            "每轮独立分层+价格偏置抽样后搜索；多轮结果合并去重，取成本最低的至多 10 组\n"
            "轮数越多结果越准确，但耗时也越长"
        )
        self.step2_special_target_label = QLabel("目标磨损")
        self.step2_special_target_label.setObjectName("alchemyStep2NormLabel")
        self.step2_special_target_edit = WearFloatLineEditWithIeee(
            0.0, 1.0, self.step2_special_card
        )
        self.step2_special_target_edit.line_edit().setObjectName("alchemyStep2NormEdit")
        self.step2_special_target_edit.line_edit().setPlaceholderText("回车或失焦生效")
        self.step2_special_target_edit.setFixedWidth(140)
        self.step2_special_target_edit.line_edit().textEdited.connect(
            self._on_step2_special_target_text_edited
        )
        self.step2_special_target_edit.line_edit().editingFinished.connect(
            self._on_step2_special_target_editing_finished
        )
        special_layout.addWidget(self.step2_special_hint_label, 1, Qt.AlignVCenter)
        special_layout.addWidget(self.step2_special_rounds_label, 0, Qt.AlignVCenter)
        special_layout.addWidget(self.step2_special_rounds_spin, 0, Qt.AlignVCenter)
        special_layout.addWidget(self.step2_special_target_label, 0, Qt.AlignVCenter)
        special_layout.addWidget(self.step2_special_target_edit, 0, Qt.AlignVCenter)
        special_layout.addStretch(1)
        step2_layout.addWidget(self.step2_special_card, 0, Qt.AlignLeft)

        self.step2_scroll = QScrollArea()
        self.step2_scroll.setObjectName("alchemyScrollArea")
        self.step2_scroll.setWidgetResizable(True)
        self.step2_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.step2_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.step2_scroll.setFrameShape(QFrame.NoFrame)
        self.step2_scroll.setAttribute(Qt.WA_StyledBackground)

        self.step2_groups_container = QWidget()
        self.step2_groups_container.setObjectName("alchemyGroupsContainer")
        self.step2_groups_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.step2_groups_layout = QVBoxLayout(self.step2_groups_container)
        self.step2_groups_layout.setContentsMargins(0, 0, 0, 0)
        self.step2_groups_layout.setSpacing(8)
        self.step2_groups_layout.setSizeConstraint(QLayout.SetMinimumSize)

        self.step2_scroll.setWidget(self.step2_groups_container)
        self.step2_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        step2_layout.addWidget(self.step2_scroll, 1)
        self._step2_update_norm_row_visibility()
        self.step_stack.addWidget(self.step2_widget)

        # 步骤3：计算设置
        self.step3_widget = QWidget()
        self.step3_widget.setObjectName("alchemyPage")
        step3_layout = QVBoxLayout(self.step3_widget)
        step3_layout.setContentsMargins(0, 0, 0, 0)
        step3_layout.setSpacing(16)
        step3_top = QHBoxLayout()
        step3_top.setSpacing(12)
        self._step3_title_icon = QLabel(self)
        self._step3_title_icon.setObjectName("contentPageTitleIcon")
        self._step3_title_icon.setFixedSize(28, 28)
        self._step3_title_icon.setAlignment(Qt.AlignCenter)
        step3_top.addWidget(self._step3_title_icon, 0, Qt.AlignVCenter)
        step3_title = QLabel("配方计算")
        step3_title.setObjectName("alchemyPageTitle")
        step3_title.setFont(title_font)
        step3_top.addWidget(step3_title, 0, Qt.AlignVCenter)
        self._step3_min_be_row = QWidget()
        self._step3_min_be_row.setObjectName("alchemyStep3MinBeRow")
        _min_be_layout = QHBoxLayout(self._step3_min_be_row)
        _min_be_layout.setContentsMargins(0, 0, 0, 0)
        _min_be_layout.setSpacing(6)
        self.step3_min_be_label = QLabel("最低保本率")
        self.step3_min_be_label.setObjectName("alchemyStep2NormLabel")
        self.step3_min_be_spin = QSpinBox(self._step3_min_be_row)
        self.step3_min_be_spin.setObjectName("alchemyStep2NormSpin")
        self.step3_min_be_spin.setRange(0, 100)
        self.step3_min_be_spin.setValue(0)
        self.step3_min_be_spin.setSuffix("%")
        _min_be_fm = QFontMetrics(self.step3_min_be_spin.font())
        self.step3_min_be_spin.setFixedWidth(
            12 + 12 + 34 + _min_be_fm.horizontalAdvance("100%") + 10
        )
        self.step3_min_be_spin.setToolTip(
            "仅搜索保本率不低于该值的配方"
        )
        self.step3_max_be_label = QLabel("最高保本率")
        self.step3_max_be_label.setObjectName("alchemyStep2NormLabel")
        self.step3_max_be_spin = QSpinBox(self._step3_min_be_row)
        self.step3_max_be_spin.setObjectName("alchemyStep2NormSpin")
        self.step3_max_be_spin.setRange(0, 100)
        self.step3_max_be_spin.setValue(100)
        self.step3_max_be_spin.setSuffix("%")
        self.step3_max_be_spin.setFixedWidth(
            12 + 12 + 34 + _min_be_fm.horizontalAdvance("100%") + 10
        )
        self.step3_max_be_spin.setToolTip(
            "仅搜索保本率不高于该值的配方"
        )
        _min_be_layout.addWidget(self.step3_min_be_label, 0, Qt.AlignVCenter)
        _min_be_layout.addWidget(self.step3_min_be_spin, 0, Qt.AlignVCenter)
        _min_be_layout.addWidget(self.step3_max_be_label, 0, Qt.AlignVCenter)
        _min_be_layout.addWidget(self.step3_max_be_spin, 0, Qt.AlignVCenter)
        step3_top.addWidget(self._step3_min_be_row, 0, Qt.AlignVCenter)
        self._step3_recipe_overlap_row = QWidget()
        self._step3_recipe_overlap_row.setObjectName("alchemyStep3RecipeOverlapRow")
        _overlap_layout = QHBoxLayout(self._step3_recipe_overlap_row)
        _overlap_layout.setContentsMargins(0, 0, 0, 0)
        _overlap_layout.setSpacing(0)
        self.step3_no_overlap_check = QCheckBox("配方材料不重复")
        self.step3_no_overlap_check.setToolTip(
            "勾选后每件材料最多进入一个配方；程序选出当前最高期望配方后扣除其材料，"
            "再用剩余材料继续计算。例如100件军规级材料最多生成10个十合一配方。"
            "计算时长较长，请谨慎勾选。"
        )
        self.step3_no_overlap_check.setObjectName("alchemyNoOverlapCheck")
        self.step3_no_overlap_check.setChecked(False)
        self.step3_no_overlap_check.setCursor(Qt.PointingHandCursor)
        self.step3_no_overlap_check.toggled.connect(self._on_no_overlap_toggled)
        _overlap_layout.addWidget(
            self.step3_no_overlap_check,
            0,
            Qt.AlignVCenter,
        )
        step3_top.addWidget(self._step3_recipe_overlap_row, 0, Qt.AlignVCenter)
        step3_top.addStretch(1)
        self._step3_save_location_row = QWidget()
        self._step3_save_location_row.setObjectName("alchemyStep3SaveLocationRow")
        self._step3_save_location_row.setVisible(False)
        _sl_top = QHBoxLayout(self._step3_save_location_row)
        _sl_top.setContentsMargins(0, 0, 0, 0)
        _sl_top.setSpacing(12)
        self._step3_save_mode_label = QLabel("配方保存位置")
        self._step3_save_mode_label.setObjectName("alchemyStep1Hint")
        _sl_top.addWidget(self._step3_save_mode_label, 0, Qt.AlignVCenter)
        self._step3_save_tooltip_row = QWidget()
        self._step3_save_tooltip_row.setObjectName("alchemyStep2ModeRow")
        _tt_layout = QHBoxLayout(self._step3_save_tooltip_row)
        _tt_layout.setContentsMargins(0, 0, 0, 0)
        _tt_layout.setSpacing(6)
        self._step3_save_hint_icon = QLabel("ⓘ")
        self._step3_save_hint_icon.setObjectName("alchemyStep2ModeHint")
        self._step3_save_tooltip_text = (
            "默认：保存配方时写入「未分类」文件夹。\n"
            "自定义：保存配方时在弹窗中选择目标文件夹。"
        )
        self._step3_save_hint_icon.setCursor(Qt.WhatsThisCursor)
        _tt_layout.addWidget(self._step3_save_hint_icon, 0, Qt.AlignVCenter)
        self._step3_save_tooltip_row.installEventFilter(self)
        _sl_top.addWidget(self._step3_save_tooltip_row, 0, Qt.AlignVCenter)
        self._step3_save_location_switch = SegmentedCheckSwitch(
            container_object_name="alchemyStep2ModeSegmented",
            slider_object_name="alchemyStep2ModeSlider",
            segments=(
                ("alchemySegmentLeft", "默认"),
                ("alchemySegmentRight", "自定义"),
            ),
        )
        self._step3_save_default_btn = self._step3_save_location_switch.buttons[0]
        self._step3_save_custom_btn = self._step3_save_location_switch.buttons[1]
        self._step3_save_location_group = QButtonGroup(self)
        self._step3_save_location_group.addButton(self._step3_save_default_btn)
        self._step3_save_location_group.addButton(self._step3_save_custom_btn)
        self._step3_save_location_group.setExclusive(True)
        self._step3_save_default_btn.clicked.connect(self._on_step3_save_location_changed)
        self._step3_save_custom_btn.clicked.connect(self._on_step3_save_location_changed)
        _sl_top.addWidget(self._step3_save_location_switch, 0, Qt.AlignVCenter)
        step3_top.addWidget(self._step3_save_location_row, 0, Qt.AlignVCenter)
        self.step3_prev_btn = QPushButton("上一步")
        self.step3_prev_btn.setObjectName("alchemyClearFileBtn")
        self.step3_prev_btn.setCursor(Qt.PointingHandCursor)
        self.step3_prev_btn.clicked.connect(self._on_step3_prev)
        step3_top.addWidget(self.step3_prev_btn, 0, Qt.AlignVCenter)
        self.step3_calc_btn = QPushButton("开始计算")
        self.step3_calc_btn.setObjectName("alchemyCalcBtn")
        self.step3_calc_btn.setCursor(Qt.PointingHandCursor)
        self.step3_calc_btn.clicked.connect(self._on_step3_calc_action_clicked)
        step3_top.addWidget(self.step3_calc_btn, 0, Qt.AlignVCenter)
        step3_layout.addLayout(step3_top)

        self.step3_progress_container = QWidget()
        self.step3_progress_container.setVisible(False)
        progress_outer = QVBoxLayout(self.step3_progress_container)
        progress_outer.setContentsMargins(0, 0, 0, 0)
        progress_outer.setSpacing(8)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(12)
        self.step3_progress_bar = QProgressBar()
        self.step3_progress_bar.setObjectName("alchemyCalcProgressBar")
        self.step3_progress_bar.setRange(0, 100)
        self.step3_progress_bar.setValue(0)
        self.step3_progress_bar.setTextVisible(False)
        progress_row.addWidget(self.step3_progress_bar, 1)
        self.step3_progress_label = QLabel("0%")
        self.step3_progress_label.setObjectName("alchemyCalcProgressLabel")
        self.step3_progress_label.setFixedWidth(44)
        self.step3_progress_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        progress_row.addWidget(self.step3_progress_label, 0, Qt.AlignVCenter)
        progress_outer.addLayout(progress_row)
        self.step3_progress_detail_label = QLabel("")
        self.step3_progress_detail_label.setObjectName("alchemyCalcProgressDetailLabel")
        self.step3_progress_detail_label.setVisible(False)
        self.step3_progress_detail_label.setWordWrap(True)
        self.step3_progress_detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.step3_progress_detail_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum,
        )
        progress_outer.addWidget(self.step3_progress_detail_label)
        step3_layout.addWidget(self.step3_progress_container, 0, Qt.AlignTop)

        self.step3_results_scroll = QScrollArea()
        self.step3_results_scroll.setObjectName("alchemyScrollArea")
        self.step3_results_scroll.setWidgetResizable(True)
        self.step3_results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.step3_results_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.step3_results_scroll.setFrameShape(QFrame.NoFrame)
        self.step3_results_scroll.setAttribute(Qt.WA_StyledBackground)
        self.step3_results_container = QWidget()
        self.step3_results_container.setObjectName("alchemyGroupsContainer")
        self.step3_results_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.step3_results_layout = QVBoxLayout(self.step3_results_container)
        self.step3_results_layout.setContentsMargins(0, 0, 0, 0)
        self.step3_results_layout.setSpacing(8)
        self.step3_results_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.step3_results_scroll.setWidget(self.step3_results_container)
        self.step3_results_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        step3_layout.addWidget(self.step3_results_scroll, 1)
        self.step_stack.addWidget(self.step3_widget)
        self._step3_update_calc_filter_visibility()

        main_layout.addWidget(self.step_stack, 1)
        self.step_stack.setCurrentIndex(0)
        self.step_stack.currentChanged.connect(self._on_navigation_step_changed)

        self._page_title_icon_labels = (
            self._step1_title_icon,
            self._step2_title_icon,
            self._step3_title_icon,
        )
        self._apply_page_title_icons()

        self._groups: list[CollapsibleGroup] = []
        self._selected_data: list = []
        # (规范化皮肤键, float32 磨损, 平台) -> item；同平台同皮肤同磨损不区分 goods_id，避免 Buff 多条上架重复。
        self._all_data: dict[tuple, dict] = {}
        self._substrate_group_by_slot_key: dict[str, CollapsibleGroup] = {}
        self._step2_product_widgets: dict = {}  # scan: pid->[(tpl,min,max)]; target: pid->[(tpl,target_edit)]
        self._step2_box_groups: list[CalcSettingProductGroup] = []
        # 各计算模式独立缓存，切换模式时不互相覆盖顶部输入与产物行
        self._step2_wear_scan_min: float = 0.0
        self._step2_wear_scan_max: float = 1.0
        self._step2_wear_target_norm: float = 1.0
        self._step2_wear_special_min: float = 0.0
        self._step2_wear_special_max: float = 1.0
        self._step2_norm_signal_conns: list = []
        self._step3_recipe_groups: list[RecipeResultGroup] = []
        self._step3_batch_source_id: str = ""
        self._calc_worker: Optional["CalcProcessRunner"] = None
        self._special_wear_runner: Optional["SpecialWearCalcRunner"] = None
        self._fetch_worker: Optional["FetchPriceWorker"] = None
        self._step3_calc_running: bool = False
        self._calc_finished_conn = None
        self._calc_progress_conn = None
        self._special_wear_finished_conn = None
        self._special_wear_progress_conn = None
        self._special_wear_stats_conn = None
        self._sw_eta_high = 30.0
        self._special_wear_elapsed_timer = QTimer(self)
        self._special_wear_elapsed_timer.setInterval(250)
        self._special_wear_elapsed_timer.timeout.connect(self._on_special_wear_elapsed_tick)
        self._special_wear_last_pct = 0
        self._special_wear_timing_active = False
        self._step2_special_target_dirty = False
        self._step2_special_target_apply_timer = QTimer(self)
        self._step2_special_target_apply_timer.setSingleShot(True)
        self._step2_special_target_apply_timer.timeout.connect(
            self._run_step2_special_target_apply
        )
        self._step2_special_radio_group = QButtonGroup(self)
        self._step2_special_radio_conn = None
        self._refresh_count_timer: QTimer | None = None
        self._alchemy_theme_icon_refresh_pending = False
        self._load_step2_wear_prefs_from_disk()

    def navigation_subroute(self) -> str:
        return ("materials", "wear", "results")[
            max(0, min(2, self.step_stack.currentIndex()))
        ]

    def navigation_route_label(self) -> str:
        labels = {
            "materials": "炼金计算 · 底物数据",
            "wear": "炼金计算 · 产物磨损",
            "results": "炼金计算 · 配方结果",
        }
        return labels[self.navigation_subroute()]

    def restore_navigation_subroute(self, route: str) -> None:
        index = {"materials": 0, "wear": 1, "results": 2}.get(route, 0)
        self.step_stack.setCurrentIndex(index)

    def _on_navigation_step_changed(self, _index: int) -> None:
        self.navigation_route_changed.emit(self.navigation_subroute())

    def save_step2_wear_prefs_for_exit(self) -> None:
        """关闭时保存磨损模式、特殊磨损轮数与配方材料去重选择。"""
        try:
            nt = float(self.step2_norm_target_edit.value())
        except (TypeError, ValueError):
            nt = 1.0
        nt = max(0.0, min(1.0, nt))
        sr = int(self.step2_special_rounds_spin.value())
        sr = max(1, min(50, sr))
        save_alchemy_step2_wear_ui(
            {
                "mode": self.get_step2_mode(),
                "norm_target": nt,
                "special_rounds": sr,
                "non_overlapping_recipes": self.step3_no_overlap_check.isChecked(),
            }
        )

    def _load_step2_wear_prefs_from_disk(self) -> None:
        d = load_alchemy_step2_wear_ui()
        if not isinstance(d, dict):
            return
        mode = str(d.get("mode") or "").strip()
        if mode not in ("scan", "target", "special_wear"):
            mode = "scan"
        self.step2_mode_scan_btn.setChecked(mode == "scan")
        self.step2_mode_target_btn.setChecked(mode == "target")
        self.step2_mode_special_btn.setChecked(mode == "special_wear")
        self.step2_mode_container.sync_mode_slider(animate=False)
        non_overlapping = bool(d.get("non_overlapping_recipes", False))
        self.step3_no_overlap_check.blockSignals(True)
        self.step3_no_overlap_check.setChecked(non_overlapping)
        self.step3_no_overlap_check.blockSignals(False)
        self._step2_update_norm_row_visibility()

        def _norm_f(key: str, default: float) -> float:
            try:
                v = float(d.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(0.0, min(1.0, v))

        # 最小/最大归一化不持久化，每次启动用全范围
        nmi, nma = 0.0, 1.0
        ntt = _norm_f("norm_target", 1.0)
        self._step2_wear_scan_min = nmi
        self._step2_wear_scan_max = nma
        self._step2_wear_target_norm = ntt
        self._step2_wear_special_min = 0.0
        self._step2_wear_special_max = 1.0
        self.step2_norm_min_edit.blockSignals(True)
        self.step2_norm_max_edit.blockSignals(True)
        self.step2_norm_target_edit.blockSignals(True)
        self.step2_norm_min_edit.setValue(nmi)
        self.step2_norm_max_edit.setValue(nma)
        self.step2_norm_target_edit.setValue(ntt)
        self.step2_norm_min_edit.blockSignals(False)
        self.step2_norm_max_edit.blockSignals(False)
        self.step2_norm_target_edit.blockSignals(False)

        try:
            r = int(d.get("special_rounds", ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS))
        except (TypeError, ValueError):
            r = ALCHEMY_SPECIAL_WEAR_DEFAULT_ROUNDS
        self.step2_special_rounds_spin.setValue(max(1, min(50, r)))

    def _on_no_overlap_toggled(self, checked: bool) -> None:
        if checked:
            show_alert(self, "提示", "计算时长较长, 谨慎勾选")
        self.save_step2_wear_prefs_for_exit()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        self._apply_page_title_icons()

    def changeEvent(self, event: QEvent):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if not self.isVisible():
                return
            # 单次深浅切换会连续收到两种事件，合并到下一拍只刷一次图标（与数据采集页一致）
            if self._alchemy_theme_icon_refresh_pending:
                return
            self._alchemy_theme_icon_refresh_pending = True
            QTimer.singleShot(0, self._run_alchemy_theme_icon_refresh)

    def _run_alchemy_theme_icon_refresh(self) -> None:
        self._alchemy_theme_icon_refresh_pending = False
        if not self.isVisible():
            return
        self._apply_page_title_icons()

    def _apply_page_title_icons(self) -> None:
        labels = getattr(self, "_page_title_icon_labels", ())
        if not labels or not ALCHEMY_ICON_PATH.is_file():
            return
        color = self.palette().color(QPalette.ColorRole.WindowText).name()
        px = 28
        ico = load_svg_icon(ALCHEMY_ICON_PATH, color, size=px)
        pm = ico.pixmap(px, px)
        if pm is None or pm.isNull():
            return
        for lb in labels:
            lb.setPixmap(pm)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "step2_mode_row", None):
            if event.type() == QEvent.Type.Enter:
                pos = QCursor.pos()
                QToolTip.showText(pos, self._step2_mode_tooltip_text, obj)
            elif event.type() == QEvent.Type.Leave:
                QToolTip.hideText()
        elif obj is getattr(self, "_step3_save_tooltip_row", None):
            if event.type() == QEvent.Type.Enter:
                pos = QCursor.pos()
                QToolTip.showText(pos, self._step3_save_tooltip_text, obj)
            elif event.type() == QEvent.Type.Leave:
                QToolTip.hideText()
        return super().eventFilter(obj, event)

    def _refresh_step1_count(self):
        """防抖刷新（连续勾选时合并为一次更新）"""
        if self._refresh_count_timer is not None:
            self._refresh_count_timer.stop()
        self._refresh_count_timer = QTimer(self)
        self._refresh_count_timer.setSingleShot(True)
        self._refresh_count_timer.timeout.connect(self._do_refresh_step1_count)
        self._refresh_count_timer.start(50)

    def _do_refresh_step1_count(self):
        """实际刷新第一页的已选皮肤/数据数量"""
        skin_names = set()
        data_count = 0
        required_count = 0
        for g in self._groups:
            for data in g.get_selected_items():
                skin_names.add(
                    canonical_goods_name_for_lookup(
                        str(data.get("goods_name", "") or "")
                    )
                )
                data_count += 1
                if data.get("must_select"):
                    required_count += 1
        self.step1_count_label.setText(
            f"已选择 {len(skin_names)} 种皮肤，{data_count} 条数据，其中包含 {required_count} 个必选底物"
        )

    def _collect_selected_data_from_groups(self) -> list[dict]:
        selected: list[dict] = []
        for g in self._groups:
            selected.extend(g.get_selected_items())
        return selected

    def _refresh_selected_data_from_groups(self) -> None:
        self._selected_data = self._collect_selected_data_from_groups()

    def _snapshot_group_ui_state(
        self,
    ) -> tuple[dict[str, tuple[bool, bool]], set[str]]:
        row_states: dict[str, tuple[bool, bool]] = {}
        expanded_goods_names: set[str] = set()
        for g in self._groups:
            goods_name = getattr(g, "_goods_name", "")
            if goods_name and getattr(g, "_expanded", False):
                expanded_goods_names.add(goods_name)
            for slot_key in g.get_all_slot_keys():
                state = g.get_row_state_by_slot_key(slot_key)
                if state is not None:
                    row_states[slot_key] = state
        return row_states, expanded_goods_names

    def _restore_group_ui_state(
        self,
        row_states: dict[str, tuple[bool, bool]],
        expanded_goods_names: set[str],
    ) -> None:
        for g in self._groups:
            goods_name = getattr(g, "_goods_name", "")
            if goods_name in expanded_goods_names and not getattr(g, "_expanded", False):
                g.toggle()
            for slot_key in g.get_all_slot_keys():
                state = row_states.get(slot_key)
                if state is None:
                    continue
                calc_checked, must_checked = state
                g.set_row_state_by_slot_key(
                    slot_key,
                    calc_checked=calc_checked,
                    must_checked=must_checked,
                )

    def _rebuild_substrate_lookup_index(self) -> None:
        slot_mapping: dict[str, CollapsibleGroup] = {}
        for g in self._groups:
            for slot_key in g.get_all_slot_keys():
                if slot_key not in slot_mapping:
                    slot_mapping[slot_key] = g
        self._substrate_group_by_slot_key = slot_mapping
        logger.debug(
            "重建炼金底物索引: groups=%d slot_key=%d",
            len(self._groups),
            len(slot_mapping),
        )

    def _get_recipe_substrate_action_state(self, slot: QrSlot) -> str:
        state = None
        slot_key = substrate_slot_lookup_key(
            name=slot.name,
            float_value=slot.float_value,
            platform=slot.platform,
        )
        if slot_key:
            group = self._substrate_group_by_slot_key.get(slot_key)
            if group is not None:
                state = group.get_row_state_by_slot_key(slot_key)
                logger.debug("配方操作槽位状态按 key 命中: key=%s state=%s", slot_key, state)
            else:
                logger.debug("配方操作槽位状态未命中 key 索引: key=%s", slot_key)
        if state is None:
            logger.warning("配方操作槽位状态未命中 slot_key: key=%s", slot_key)
            return "neutral"
        calc_checked, must_checked = state
        if not calc_checked:
            return "excluded"
        if must_checked:
            return "locked"
        return "neutral"

    def _set_recipe_substrate_action_state(
        self, slot: QrSlot, target_state: str, *, notify: bool = True
    ) -> None:
        calc_checked = target_state != "excluded"
        must_checked = target_state == "locked"
        updated = False
        slot_key = substrate_slot_lookup_key(
            name=slot.name,
            float_value=slot.float_value,
            platform=slot.platform,
        )
        if slot_key:
            group = self._substrate_group_by_slot_key.get(slot_key)
            if group is not None:
                updated = group.set_row_state_by_slot_key(
                    slot_key,
                    calc_checked=calc_checked,
                    must_checked=must_checked,
                )
                logger.debug("配方操作槽位写回按 key 命中: key=%s updated=%s", slot_key, updated)
            else:
                logger.debug("配方操作槽位写回未命中 key 索引: key=%s", slot_key)
        if not updated:
            logger.warning("配方操作槽位写回未命中 slot_key: key=%s", slot_key)
            if notify:
                show_toast(self, "未找到对应底物，无法更新状态", style="warning")
            return
        self._refresh_selected_data_from_groups()
        if not notify:
            return
        if target_state == "excluded":
            show_toast(self, "已排除当前底物", style="error")
        elif target_state == "locked":
            show_toast(self, "已锁定当前底物", style="success")
        else:
            show_toast(self, "已恢复当前底物", style="info")

    def _on_next(self):
        """下一步 - 收集勾选数据并显示汇总页，所有校验在点击时统一处理"""
        if not self.next_btn._file_loaded:
            show_toast(self, "请先选择文件", style="warning")
            return

        self._refresh_selected_data_from_groups()

        if not self._selected_data:
            show_toast(self, "请至少选择一条底物数据", style="warning")
            return

        for quality, _stat_trak, k, rows in partition_selected_data_by_tradeup_group(
            self._selected_data,
            eligible_only=True,
        ):
            required_count = sum(1 for row in rows if row.get("must_select"))
            if required_count >= k:
                limit_hint = (
                    "隐秘品质的必选底物至多 4 个"
                    if quality == "隐秘"
                    else f"{quality}品质的必选底物至多 9 个"
                )
                show_toast(self, limit_hint, style="warning")
                return
        self._display_step2()
        self.step_stack.setCurrentIndex(1)
        if not load_alchemy_wear_step2_notice_dismissed():
            win = self.window()

            def _show_wear_notice() -> None:
                dlg = WearInputNoticeDialog(win)
                dlg.exec()

            QTimer.singleShot(0, _show_wear_notice)

    def _on_step3_prev(self):
        """计算设置 - 上一步，返回磨损设置"""
        self.step_stack.setCurrentIndex(1)

    def _step3_set_calc_button_idle(self) -> None:
        self._step3_calc_running = False
        self.step3_calc_btn.setText("开始计算")
        self.step3_calc_btn.setEnabled(True)
        self.step3_no_overlap_check.setEnabled(True)
        self._step3_set_calc_btn_stopping(False)

    def _step3_set_calc_btn_stopping(self, stopping: bool) -> None:
        self.step3_calc_btn.setProperty("calcStopping", stopping)
        self.step3_calc_btn.style().unpolish(self.step3_calc_btn)
        self.step3_calc_btn.style().polish(self.step3_calc_btn)

    def _step3_reset_after_calc_interrupted(self) -> None:
        self._stop_special_wear_elapsed_timer()
        self._disconnect_calc_runner_signals()
        self._calc_worker = None
        self._special_wear_runner = None
        self._fetch_worker = None
        self._step3_calc_running = False
        self.step3_progress_container.setVisible(False)
        self.step3_progress_bar.setVisible(True)
        self.step3_progress_bar.setRange(0, 100)
        self.step3_progress_detail_label.clear()
        self.step3_progress_detail_label.setVisible(False)
        self.step3_progress_label.setMinimumWidth(0)
        self.step3_progress_label.setFixedWidth(48)
        self.step3_progress_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self.step3_calc_btn.setText("开始计算")
        self.step3_calc_btn.setEnabled(True)
        self.step3_no_overlap_check.setEnabled(True)
        self._step3_set_calc_btn_stopping(False)
        show_toast(self, "计算已中断", style="info")

    def _on_step3_calc_action_clicked(self) -> None:
        if self._step3_calc_running:
            self._on_step3_stop_calc_requested()
            return
        self._on_step3_start_calc()

    def _on_step3_stop_calc_requested(self) -> None:
        if worker_is_running(self._fetch_worker):
            self._fetch_worker.requestInterruption()
        if self._calc_worker is not None:
            self._calc_worker.cancel()
        if self._special_wear_runner is not None:
            self._special_wear_runner.cancel()
        if self._step3_calc_running:
            self.step3_calc_btn.setText("正在停止…")
            show_toast(self, "正在停止计算…", style="info")

    def _on_step3_start_calc(self):
        """开始计算 - 加载本地产物价格后启动后台计算"""
        if not self._selected_data:
            show_toast(self, "请先选择底物数据", style="warning")
            return

        self._step3_calc_running = True
        self.step3_calc_btn.setText("停止计算")
        self.step3_no_overlap_check.setEnabled(False)
        self._step3_set_calc_btn_stopping(True)
        show_toast(self, "正在加载产物价格...", style="info")

        self._fetch_worker = FetchPriceWorker(self)
        self._fetch_worker.finished.connect(self._on_fetch_price_finished)
        self._fetch_worker.start()

    def _show_step3_input_error(self, message: str) -> None:
        """步骤三开始计算前的参数错误统一走 Toast。"""
        show_toast(self, message, style="warning")

    def _clear_step3_results_for_new_calc(self) -> None:
        """在即将用新结果替换界面时清空步骤三列表（校验类错误在调用前应已 return）。"""
        for g in self._step3_recipe_groups:
            self.step3_results_layout.removeWidget(g)
            g.deleteLater()
        self._step3_recipe_groups.clear()
        if self.step3_results_layout.count() > 0:
            item = self.step3_results_layout.takeAt(self.step3_results_layout.count() - 1)
            if item and item.spacerItem():
                del item
        self._step3_save_location_row.setVisible(False)
        self._step3_batch_source_id = ""

    @staticmethod
    def _is_step3_validation_error(message: str) -> bool:
        msg = str(message or "").strip()
        if not msg:
            return False
        prefixes = (
            "请先选择底物数据",
            "请选择单一品质的底物",
            "无法解析底物数据",
            "有效底物数量不足",
            "必选底物最多只能选择",
            "非必选底物数量不足",
            "底物数量应为",
            "底物品质数据不一致",
            "无底物",
        )
        return msg.startswith(prefixes)

    def _disconnect_calc_runner_signals(self):
        """按 Connection 句柄断开，避免目标模式未连接 progress 时 disconnect(slot) 触发 RuntimeWarning。"""
        if self._calc_progress_conn is not None:
            QObject.disconnect(self._calc_progress_conn)
            self._calc_progress_conn = None
        if self._calc_finished_conn is not None:
            QObject.disconnect(self._calc_finished_conn)
            self._calc_finished_conn = None
        if self._special_wear_finished_conn is not None:
            QObject.disconnect(self._special_wear_finished_conn)
            self._special_wear_finished_conn = None
        if self._special_wear_progress_conn is not None:
            QObject.disconnect(self._special_wear_progress_conn)
            self._special_wear_progress_conn = None
        if self._special_wear_stats_conn is not None:
            QObject.disconnect(self._special_wear_stats_conn)
            self._special_wear_stats_conn = None

    def _on_calc_finished(self, recipes: list, error_msg: str | None):
        """计算完成 - 展示 top10 配方"""
        if error_msg == "__cancelled__":
            self._step3_reset_after_calc_interrupted()
            return
        self._stop_special_wear_elapsed_timer()
        if self._special_wear_progress_conn is not None:
            QObject.disconnect(self._special_wear_progress_conn)
            self._special_wear_progress_conn = None
        if self._special_wear_stats_conn is not None:
            QObject.disconnect(self._special_wear_stats_conn)
            self._special_wear_stats_conn = None
        self.step3_progress_container.setVisible(False)
        self.step3_progress_bar.setVisible(True)
        self.step3_progress_bar.setRange(0, 100)
        self.step3_progress_detail_label.clear()
        self.step3_progress_detail_label.setVisible(False)
        self.step3_progress_label.setMinimumWidth(0)
        self.step3_progress_label.setFixedWidth(48)
        self.step3_progress_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self._step3_set_calc_button_idle()

        elapsed = time.time() - getattr(self, "_calc_start_time", 0)
        elapsed_str = f"耗时 {elapsed:.1f} 秒"

        if error_msg and self._is_step3_validation_error(error_msg):
            if not self._step3_recipe_groups:
                self._step3_save_location_row.setVisible(False)
            self._show_step3_input_error(error_msg)
            return

        self._clear_step3_results_for_new_calc()

        if error_msg:
            self._step3_save_location_row.setVisible(False)
            show_alert(self, "计算失败", f"{error_msg}\n\n{elapsed_str}")
            return

        if not recipes:
            self._step3_save_location_row.setVisible(False)
            min_be_pct = int(self.step3_min_be_spin.value())
            max_be_pct = int(self.step3_max_be_spin.value())
            if (
                (min_be_pct > 0 or max_be_pct < 100)
                and self.get_step2_mode() in ("scan", "target")
            ):
                msg = (
                    f"未找到保本率在 {min_be_pct}%～{max_be_pct}% "
                    "范围内的配方"
                )
            else:
                msg = "未找到符合条件的配方"
            show_alert(self, "计算完成", f"{msg}\n\n{elapsed_str}")
            return

        for rank, recipe in enumerate(recipes, 1):
            group = RecipeResultGroup(
                rank,
                recipe,
                self.step3_results_container,
                enable_save=True,
                get_substrate_action_state=self._get_recipe_substrate_action_state,
                set_substrate_action_state=self._set_recipe_substrate_action_state,
            )
            group.save_requested.connect(self._on_recipe_save_requested)
            group.add_to_purchase_batch_requested.connect(
                self._on_recipe_add_to_purchase_batch_requested
            )
            self.step3_results_layout.addWidget(group)
            self._step3_recipe_groups.append(group)
        self.step3_results_layout.addStretch(1)

        self._step3_save_default_btn.blockSignals(True)
        self._step3_save_custom_btn.blockSignals(True)

        custom = load_alchemy_recipe_save_mode() == "custom"
        self._step3_save_default_btn.setChecked(not custom)
        self._step3_save_custom_btn.setChecked(custom)
        self._step3_save_default_btn.blockSignals(False)
        self._step3_save_custom_btn.blockSignals(False)
        self._step3_save_location_row.setVisible(True)
        self._step3_batch_source_id = f"alchemy-{time.time_ns()}"
        self._step3_save_location_switch.sync_mode_slider(animate=False)
        self._sync_step3_save_button_labels()

        show_toast(self, f"计算完成，共 {len(recipes)} 个配方，{elapsed_str}", style="success")

    def _on_step3_save_location_changed(self) -> None:
        self._step3_save_location_switch.sync_mode_slider(animate=True)
        self._sync_step3_save_button_labels()
        save_alchemy_recipe_save_mode(
            "custom" if self._step3_save_custom_btn.isChecked() else "default",
        )

    def _sync_step3_save_button_labels(self) -> None:
        if not self._step3_recipe_groups:
            return
        custom = self._step3_save_custom_btn.isChecked()
        text = "保存配方到..." if custom else "保存配方"
        for g in self._step3_recipe_groups:
            g.set_save_button_text(text)

    def _on_recipe_save_requested(self, rank: int, recipe: dict):
        try:
            mode = self.get_step2_mode()
            if mode == "scan":
                norm_min = self.step2_norm_min_edit.value()
                norm_max = self.step2_norm_max_edit.value()
            elif mode == "special_wear":
                norm_min = float(self._step2_wear_special_min)
                norm_max = float(self._step2_wear_special_max)
            else:
                t = self.step2_norm_target_edit.value()
                norm_min = norm_max = t

            if self._step3_save_custom_btn.isChecked():
                targets = build_all_recipe_folder_pick_targets()
                dlg = MoveRecipeFolderDialog(
                    self.window(),
                    targets=targets,
                    dialog_title="保存配方到",
                    hint_text="请勾选一个目标文件夹，然后点击「确定」。",
                    allow_create_folder=True,
                    recipe_name_default=default_save_recipe_dialog_title(recipe),
                )
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    return
                fid = dlg.chosen_folder_id()
                save_recipe_file(
                    recipe,
                    rank=rank,
                    mode=mode,
                    norm_min=float(norm_min),
                    norm_max=float(norm_max),
                    folder_id=fid,
                    title=dlg.chosen_recipe_title(),
                )
                save_last_recipe_save_folder_id(fid)
            else:
                save_recipe_file(
                    recipe,
                    rank=rank,
                    mode=mode,
                    norm_min=float(norm_min),
                    norm_max=float(norm_max),
                    folder_id=None,
                )
            show_toast(self, "配方已保存到「配方管理」", style="success")
        except OSError as e:
            show_toast(self, f"保存失败：{e}", style="error")

    def _create_purchase_batch_path(self) -> Path | None:
        accounts = [
            entry for entry in list_profile_entries() if str(entry.get("id") or "")
        ]
        if not accounts:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return None
        if not ask_confirmation(
            self,
            "创建采购批次",
            "创建时会把该账号当前本地库存记为基线。请确认已经在 Steam 库存页刷新过该账号库存。",
        ):
            return None
        default_name = datetime.now().strftime("采购批次 %Y-%m-%d %H:%M")
        name, accepted = get_wide_text_input(
            self,
            title="新建采购批次",
            label="批次名称：",
            value=default_name,
        )
        if not accepted or not name.strip():
            return None
        account_labels = [combo_display_name_for_profile(entry) for entry in accounts]
        active_id = get_active_profile_id()
        current = next(
            (
                index
                for index, entry in enumerate(accounts)
                if str(entry.get("id") or "") == active_id
            ),
            0,
        )
        account_label, accepted = QInputDialog.getItem(
            self,
            "选择收货账号",
            "Steam 收货账号：",
            account_labels,
            current,
            False,
        )
        if not accepted:
            return None
        entry = accounts[account_labels.index(account_label)]
        profile_id = str(entry.get("id") or "")
        cfg = load_steam_account_config_dict(profile_id)
        try:
            return create_purchase_batch(
                name,
                profile_id=profile_id,
                steam_id=str(cfg.get("steam_id") or ""),
                account_name=account_label,
                inventory_items=load_profile_inventory_items(profile_id),
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"采购批次创建失败：{exc}", style="warning")
            return None

    def _choose_purchase_batch_path(self):
        batches = list_purchase_batches()
        labels: list[str] = []
        for _path, batch in batches:
            summary = purchase_batch_summary(batch)
            labels.append(
                f"{batch.get('name') or '未命名批次'} · "
                f"{batch.get('account_name') or 'Steam'} · "
                f"{summary['recipes']}配方/{summary['total']}件"
            )
        labels.append(_NEW_PURCHASE_BATCH_LABEL)
        selected, accepted = QInputDialog.getItem(
            self,
            "加入采购批次",
            "选择目标批次：",
            labels,
            0,
            False,
        )
        if not accepted:
            return None
        if selected == _NEW_PURCHASE_BATCH_LABEL:
            return self._create_purchase_batch_path()
        return batches[labels.index(selected)][0]

    def _alchemy_purchase_title(self, rank: int, recipe: dict) -> str:
        wear = float(recipe.get("avg_nfv") or 0)
        return f"炼金计算方案 {rank:02d} · 归一化磨损 {wear:.9f}"

    def _add_alchemy_recipe_to_batch(
        self,
        path: Path,
        rank: int,
        recipe: dict,
    ) -> bool:
        source_id = self._step3_batch_source_id or "alchemy-current"
        add_recipe_to_purchase_batch(
            path,
            recipe,
            title=self._alchemy_purchase_title(rank, recipe),
            source_ref=f"{source_id}:{rank}",
        )
        return True

    def _on_recipe_add_to_purchase_batch_requested(
        self,
        rank: int,
        recipe: dict,
    ) -> None:
        path = self._choose_purchase_batch_path()
        if path is None:
            return
        try:
            self._add_alchemy_recipe_to_batch(path, rank, recipe)
        except (OSError, ValueError) as exc:
            show_toast(self, f"加入采购批次失败：{exc}", style="warning")
            return
        show_toast(self, "配方已加入采购批次", style="success")

    def _on_clear_file(self):
        """清除所有已加载的数据"""
        if not self.next_btn._file_loaded:
            return
        self._all_data.clear()
        self._display_groups({})
        self.next_btn.set_file_loaded(False)
        self.step1_count_label.setText("已选择 0 种皮肤，0 条数据，其中包含 0 个必选底物")
        show_toast(self, "已清除", style="success")

    def _on_recipe_data_clicked(self) -> None:
        entries = list_saved_recipes()
        if not entries:
            show_toast(self, "暂无已保存配方", style="warning")
            return
        dlg = ExcludeSavedRecipesDialog(self.window(), entries)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        action = dlg.chosen_action()
        selected = dlg.selected_payloads()
        if not selected:
            show_toast(self, "未选择配方", style="warning")
            return
        if action == "exclude":
            unchecked, skipped, had_markers = self._exclude_substrates_from_saved_recipes(
                selected
            )
            if not had_markers:
                if skipped > 0:
                    show_toast(
                        self,
                        f"所选配方中有 {skipped} 条底物无法识别（名称），无法排除",
                        style="warning",
                    )
                else:
                    show_toast(self, "所选配方中没有底物数据", style="warning")
                return
            if unchecked > 0:
                msg = f"已取消勾选 {unchecked} 条底物"
                if skipped > 0:
                    msg += f"（另有 {skipped} 条配方底物无法识别已跳过）"
                show_toast(self, msg, style="success")
            else:
                msg = "当前列表中没有需要排除的底物"
                if skipped > 0:
                    msg += f"（所选配方中有 {skipped} 条底物无法识别）"
                show_toast(self, msg, style="info")
        elif action == "import":
            added_from_recipe = 0
            mat_skip = mat_dup = 0
            had_pre_existing = len(self._all_data) > 0
            added_from_recipe, mat_skip, mat_dup = (
                self._import_materialize_substrates_from_recipe_payloads(selected)
            )
            if added_from_recipe > 0:
                self._finalize_ingest(
                    preserve_existing_states=had_pre_existing
                )
                for g in self._step3_recipe_groups:
                    self.step3_results_layout.removeWidget(g)
                    g.deleteLater()
                self._step3_recipe_groups.clear()
                if self.step3_results_layout.count() > 0:
                    item = self.step3_results_layout.takeAt(
                        self.step3_results_layout.count() - 1
                    )
                    if item and item.spacerItem():
                        del item
                self._step3_save_location_row.setVisible(False)
                self.step_stack.setCurrentIndex(0)
            applied, skipped, had_table_match = (
                self._import_substrate_states_from_saved_recipes(selected)
            )
            if added_from_recipe > 0 or applied > 0:
                parts: list[str] = []
                if added_from_recipe > 0:
                    parts.append(f"已从配方合并 {added_from_recipe} 条新底物")
                if applied > 0:
                    parts.append(f"已同步 {applied} 行「参与计算 / 必选」")
                msg = "；".join(parts)
                if skipped > 0:
                    msg += f"（跳过 {skipped} 条无法解析的配方底物）"
                if mat_skip or mat_dup:
                    extra = []
                    if mat_skip:
                        extra.append(f"{mat_skip} 条配方底物未写入（无法识别）")
                    if mat_dup:
                        extra.append(f"{mat_dup} 条与列表已有底物重复已跳过")
                    msg += "（" + "；".join(extra) + "）"
                show_toast(self, msg, style="success")
            else:
                if skipped > 0:
                    show_toast(
                        self,
                        f"未更新任何底物（所选配方中有 {skipped} 条底物无法识别）",
                        style="warning",
                    )
                elif had_table_match:
                    show_toast(
                        self,
                        "已比对配方：当前列表中匹配行的「参与计算 / 必选」与配方一致，无需修改。",
                        style="info",
                    )
                elif mat_skip > 0 or mat_dup > 0:
                    parts = []
                    if mat_skip:
                        parts.append(f"{mat_skip} 条配方底物无法识别")
                    if mat_dup:
                        parts.append(f"{mat_dup} 条与列表已有底物重复")
                    show_toast(
                        self,
                        "未能合并新底物（" + "；".join(parts) + "）",
                        style="warning",
                    )
                else:
                    show_toast(
                        self,
                        "所选配方中没有底物数据，或与当前列表无任何可合并项。",
                        style="info",
                    )
        else:
            show_toast(self, "未选择操作", style="warning")

    def _exclude_substrates_from_saved_recipes(
        self, payloads: list[dict]
    ) -> tuple[int, int, bool]:
        """
        将当前列表中与所选已保存配方底物（模板 + 磨损）一致的行取消「是否参与计算」勾选。
        不删除底物数据。返回 (取消勾选条数, 无法解析的配方底物条数, 是否生成了至少一条有效排除规则)。
        """
        markers: list[tuple[tuple[str, bool], float]] = []
        skipped = 0
        for payload in payloads:
            recipe = payload.get("recipe") or {}
            subs = recipe.get("substrates_display")
            if not isinstance(subs, list):
                continue
            for s in subs:
                if not isinstance(s, dict):
                    continue
                name = s.get("name")
                fv = s.get("float_value")
                if name is None or fv is None:
                    skipped += 1
                    continue
                tpl = get_template_from_goods_name(str(name))
                if tpl is None:
                    skipped += 1
                    continue
                try:
                    fv_f = float(fv)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                markers.append((_recipe_exclude_template_key(tpl), fv_f))

        if not markers:
            return 0, skipped, False

        def _should_uncheck(data: dict) -> bool:
            goods_name = data.get("goods_name", "")
            fv = data.get("float_value")
            tpl = get_template_from_goods_name(str(goods_name))
            if tpl is None:
                return False
            try:
                fv_f = float(fv)
            except (TypeError, ValueError):
                return False
            tk = _recipe_exclude_template_key(tpl)
            for mk, mfv in markers:
                if mk == tk and _recipe_exclude_float_close(fv_f, mfv):
                    return True
            return False

        total = 0
        for g in self._groups:
            total += g.uncheck_rows_where(_should_uncheck)
        self._refresh_step1_count()
        return total, skipped, True

    @staticmethod
    def _import_calc_row_dict(calc_it) -> dict | None:
        if calc_it is None:
            return None
        data = calc_it.data(Qt.ItemDataRole.UserRole)
        if data is None:
            return None
        # PySide 可能把 Python dict 包成带 .value() 的类型
        for _ in range(4):
            val = getattr(data, "value", None)
            if callable(val):
                try:
                    nxt = val()
                except Exception:
                    break
                if nxt is None or nxt is data:
                    break
                data = nxt
            else:
                break
        if isinstance(data, dict):
            return data
        if isinstance(data, Mapping):
            try:
                return dict(data)
            except Exception:
                return None
        return None

    @staticmethod
    def _import_cell_slot_key_str(calc_it, data: dict) -> str | None:
        raw = calc_it.data(Qt.ItemDataRole.UserRole + 2)
        if raw is not None:
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            try:
                s = str(raw).strip()
                if s:
                    return s
            except Exception:
                pass
        sk = substrate_row_lookup_key(data)
        if isinstance(sk, str) and sk.strip():
            return sk.strip()
        return None

    def _import_find_matching_rows_for_substrate(self, s: dict) -> list[tuple[CollapsibleGroup, int]]:
        """扫描步骤 1 全部表格行：精确槽位键、名称+磨损前缀、或模板键+磨损近似。"""
        name = s.get("name")
        fv = s.get("float_value")
        plat = s.get("platform")
        if name is None or fv is None:
            return []
        try:
            fv_rec = float(fv)
        except (TypeError, ValueError):
            return []
        sk_full = substrate_slot_lookup_key(name=name, float_value=fv, platform=plat)
        id_key = substrate_identity_key(name=name, float_value=fv)
        tpl_rec = get_template_from_goods_name(str(name).strip())
        mk_rec = _recipe_exclude_template_key(tpl_rec) if tpl_rec is not None else None

        out: list[tuple[CollapsibleGroup, int]] = []
        seen: set[tuple[int, int]] = set()
        for g in self._groups:
            # Saved-recipe matching is explicitly row-oriented.  Ordinary large
            # imports keep collapsed tables lazy; only this less frequent action
            # needs the concrete table rows.
            g.ensure_table_populated()
            tw = g.table_widget
            for row in range(tw.rowCount()):
                calc_it = tw.item(row, tw._CHECK_COL)
                if calc_it is None:
                    continue
                data = self._import_calc_row_dict(calc_it)
                if data is None:
                    continue
                sk_live = self._import_cell_slot_key_str(calc_it, data)
                id_live = substrate_identity_key(
                    name=data.get("goods_name"),
                    float_value=data.get("float_value"),
                )
                matched = False
                if sk_full and sk_live and sk_full == sk_live:
                    matched = True
                elif id_key and id_live and id_key == id_live:
                    # 与「名称+磨损+平台」整键不同：不依赖 sk_live（platform 为空时槽位键为 None）
                    matched = True
                elif id_key and sk_live and sk_live.startswith(id_key + "||"):
                    matched = True
                elif mk_rec is not None:
                    tpl_d = get_template_from_goods_name(str(data.get("goods_name", "")))
                    if tpl_d is not None and _recipe_exclude_template_key(tpl_d) == mk_rec:
                        try:
                            fv_d = float(data.get("float_value"))
                        except (TypeError, ValueError):
                            fv_d = None
                        if fv_d is not None and _recipe_exclude_float_close(fv_d, fv_rec):
                            matched = True
                if not matched:
                    continue
                k = (id(g), row)
                if k in seen:
                    continue
                seen.add(k)
                out.append((g, row))
        return out

    @staticmethod
    def _row_dict_from_recipe_substrate_display(s: dict) -> dict | None:
        """将已保存配方 ``substrates_display`` 单项转为步骤 1 行字典；无效则 None。"""
        name = s.get("name")
        fv = s.get("float_value")
        if name is None or fv is None:
            return None
        try:
            fv_f = float(fv)
        except (TypeError, ValueError):
            return None
        goods_name = str(name).strip()
        if not goods_name or not get_quality_from_goods_name(goods_name):
            return None
        plat = str(s.get("platform") or "").strip().lower()
        if not plat:
            plat = "buff"
        uid = s.get("uuid")
        if uid is not None and str(uid).strip():
            gid = str(uid).strip()
        else:
            blob = json.dumps(
                {
                    "goods_name": goods_name,
                    "float_value": fv_f,
                    "platform": plat,
                    "purchase_link": str(s.get("purchase_link") or ""),
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            gid = "recipe_sub:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:22]
        try:
            price_f = float(s.get("price"))
        except (TypeError, ValueError):
            price_f = 0.0
        out: dict = {
            "goods_name": goods_name,
            "float_value": fv_f,
            "platform": plat,
            "goods_id": gid,
            "price": price_f,
        }
        wb = s.get("weapon_box")
        if wb is not None and str(wb).strip():
            out["weapon_box"] = str(wb).strip()
        pl = s.get("purchase_link")
        if pl is not None and str(pl).strip():
            out["purchase_link"] = str(pl).strip()
        return out

    def _import_materialize_substrates_from_recipe_payloads(
        self, payloads: list[dict]
    ) -> tuple[int, int, int]:
        """从所选配方的 ``substrates_display`` 合并生成步骤 1 行：仅追加 ``_all_data`` 中尚不存在的去重键。
        在列表已有其它底物时同样会合并配方中「列表里还没有」的条目。
        返回 (新增条数, 无法识别/校验失败条数, 与已有数据重复键未写入条数)。
        """
        added = skip_bad = skip_dup = 0
        for payload in payloads:
            recipe = payload.get("recipe") or {}
            subs = recipe.get("substrates_display")
            if not isinstance(subs, list):
                continue
            for s in subs:
                if not isinstance(s, dict):
                    continue
                row = self._row_dict_from_recipe_substrate_display(s)
                if row is None:
                    skip_bad += 1
                    continue
                key = self._row_dedupe_key(row)
                if key in self._all_data:
                    skip_dup += 1
                    continue
                if self._try_add_one_row(row):
                    added += 1
                else:
                    skip_bad += 1
        return added, skip_bad, skip_dup

    def _import_substrate_states_from_saved_recipes(
        self, payloads: list[dict]
    ) -> tuple[int, int, bool]:
        """
        按已保存配方 ``substrates_display`` 对当前步骤 1 中匹配行写回「参与计算 / 必选」。

        匹配顺序：先按「名称 + 磨损 + 平台」精确命中；若无命中再按「名称 + 磨损」
        命中当前列表中任意平台来源的同一底物；若仍无命中，再按与「排除配方」相同的
        ``paint_index + StatTrak`` 与磨损近似匹配扫描步骤 1 表格行。

        写回时按 **表格行号** 直接更新复选框，不依赖 ``_slot_key_to_row``（避免槽位索引缺失、
        排序后索引与 ``set_row_state_by_slot_key`` 不一致等问题）。

        - 若条目含 ``alchemy_meta_excluded`` / ``alchemy_meta_locked``（在配方管理中设置），
          分别对应不参与计算、必选。
        - 无上述标记时仍参与导入：视为「参与计算且非必选」，用于与配方对齐或从排除状态恢复。

        返回 (实际改写了勾选状态的底物行数, 配方底物条目中缺少名称/磨损或无法解析浮点而跳过数,
        是否至少命中过一条当前列表中的底物行——即使勾选状态与配方一致未改写也算 True)。
        """
        touched_rows: set[tuple[int, int]] = set()
        skipped = 0
        had_table_match = False
        for payload in payloads:
            recipe = payload.get("recipe") or {}
            subs = recipe.get("substrates_display")
            if not isinstance(subs, list):
                continue
            for s in subs:
                if not isinstance(s, dict):
                    continue
                name = s.get("name")
                fv = s.get("float_value")
                if name is None or fv is None:
                    skipped += 1
                    continue
                try:
                    float(fv)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                rows = self._import_find_matching_rows_for_substrate(s)
                if not rows:
                    continue
                had_table_match = True
                ex = bool(s.get(SUBSTRATE_ALCHEMY_META_EXCLUDED_KEY))
                lk = bool(s.get(SUBSTRATE_ALCHEMY_META_LOCKED_KEY)) and not ex
                if ex:
                    calc_checked, must_checked = False, False
                elif lk:
                    calc_checked, must_checked = True, True
                else:
                    calc_checked, must_checked = True, False
                for grp, row_i in rows:
                    if grp.set_row_calc_must_at_row(
                        row_i,
                        calc_checked=calc_checked,
                        must_checked=must_checked,
                    ):
                        touched_rows.add((id(grp), row_i))
        self._rebuild_substrate_lookup_index()
        self._refresh_selected_data_from_groups()
        self._refresh_step1_count()
        return len(touched_rows), skipped, had_table_match

    @staticmethod
    def _dedupe_float_key_part(v: object) -> object:
        """与 ``substrate_slot_lookup_key`` / ``SkinInstance`` 一致：磨损按 float32 参与去重键。"""
        if v is None:
            return None
        try:
            return float(np.float32(float(v)))
        except (TypeError, ValueError):
            return v

    @staticmethod
    def _row_dedupe_key(data: dict) -> tuple:
        gn = data.get("goods_name", "未知")
        raw = str(gn).strip() if gn is not None else ""
        canon = canonical_goods_name_for_lookup(raw) if raw else ""
        goods_key = (canon or raw or "未知").strip()
        if not goods_key:
            goods_key = "未知"
        float_value = AlchemyPage._dedupe_float_key_part(data.get("float_value"))
        plat = str(data.get("platform", "") or "").strip().lower() or "buff"
        return (goods_key, float_value, plat)

    def _try_add_one_row(self, data: dict) -> bool:
        """校验并写入 _all_data，重复键不覆盖。返回是否新增。"""
        if not isinstance(data, dict) or REQUIRED_KEYS - set(data.keys()):
            return False
        goods_name = data.get("goods_name", "未知")
        if not get_quality_from_goods_name(goods_name):
            return False
        key = self._row_dedupe_key(data)
        if key in self._all_data:
            return False
        self._all_data[key] = data
        return True

    def _finalize_ingest(self, *, preserve_existing_states: bool = True) -> None:
        row_states: dict[str, tuple[bool, bool]] = {}
        expanded_goods_names: set[str] = set()
        if preserve_existing_states and self._groups:
            row_states, expanded_goods_names = self._snapshot_group_ui_state()
        to_remove = [
            k
            for k, row in self._all_data.items()
            if not get_quality_from_goods_name(str(row.get("goods_name", "") or ""))
        ]
        for k in to_remove:
            del self._all_data[k]
        grouped = self._build_grouped_from_all_data()
        self._display_groups(
            grouped,
            preserved_row_states=row_states,
            expanded_goods_names=expanded_goods_names,
        )
        self.next_btn.set_file_loaded(len(self._all_data) > 0)

    def _ingest_scraped_dicts(self, raw_items: list[dict], *, replace_all: bool) -> tuple[int, int]:
        """
        replace_all=True：清空后仅保留本批数据。
        返回 (成功写入条数, 未写入条数)（无效行、重复键、无法解析品质等均计入失败）。
        """
        if replace_all:
            self._all_data.clear()
        added = 0
        failed = 0
        for data in raw_items:
            if not isinstance(data, dict) or REQUIRED_KEYS - set(data.keys()):
                failed += 1
                continue
            if self._try_add_one_row(data):
                added += 1
            else:
                failed += 1
        self._finalize_ingest(preserve_existing_states=not replace_all)
        return added, failed

    def _markers_template_float_from_import_items(
        self, items: list[dict]
    ) -> list[tuple[tuple[str, bool], float]]:
        """库存导入条目的 (模板键, 磨损)，用于与非库存底物比对（同 saved recipe 排除逻辑）。"""
        markers: list[tuple[tuple[str, bool], float]] = []
        for data in items:
            if not isinstance(data, dict):
                continue
            name = data.get("goods_name")
            fv = data.get("float_value")
            if name is None or fv is None:
                continue
            tpl = get_template_from_goods_name(str(name))
            if tpl is None:
                continue
            try:
                fv_f = float(fv)
            except (TypeError, ValueError):
                continue
            markers.append((_recipe_exclude_template_key(tpl), fv_f))
        return markers

    def _should_uncheck_non_inventory_conflicting_with_markers(
        self,
        data: dict,
        markers: list[tuple[tuple[str, bool], float]],
    ) -> bool:
        if not markers:
            return False
        plat = str(data.get("platform", "")).strip().lower()
        if plat in _INVENTORY_PLATFORMS:
            return False
        goods_name = data.get("goods_name", "")
        fv = data.get("float_value")
        tpl = get_template_from_goods_name(str(goods_name))
        if tpl is None:
            return False
        try:
            fv_f = float(fv)
        except (TypeError, ValueError):
            return False
        tk = _recipe_exclude_template_key(tpl)
        for mk, mfv in markers:
            if mk == tk and _recipe_exclude_float_close(fv_f, mfv):
                return True
        return False

    def apply_inventory_import_merge(self, items: list[dict]) -> None:
        """保留现有底物并追加库存导入，回到步骤一。对与库存同模板同磨损的非库存行取消「是否参与计算」。"""
        markers = self._markers_template_float_from_import_items(items)
        added, ingest_failed = self._ingest_scraped_dicts(items, replace_all=False)

        for g in self._step3_recipe_groups:
            self.step3_results_layout.removeWidget(g)
            g.deleteLater()
        self._step3_recipe_groups.clear()
        if self.step3_results_layout.count() > 0:
            item = self.step3_results_layout.takeAt(self.step3_results_layout.count() - 1)
            if item and item.spacerItem():
                del item
        self._step3_save_location_row.setVisible(False)
        self.step_stack.setCurrentIndex(0)

        unchecked = 0
        if markers:

            def _should_uncheck(data: dict) -> bool:
                return self._should_uncheck_non_inventory_conflicting_with_markers(
                    data, markers
                )

            for g in self._groups:
                unchecked += g.uncheck_rows_where(_should_uncheck)

        msg = f"已合并库存底物 {added} 条"
        if ingest_failed:
            msg += f"，未写入 {ingest_failed} 条"
        if unchecked:
            msg += f"；已对 {unchecked} 条同皮肤同磨损的非库存底物取消参与计算"
        show_toast(self, msg, style="success" if (added or unchecked) else "info")

    def apply_inventory_import_replace(
        self,
        items: list[dict],
        *,
        source_label: str = "库存",
    ) -> None:
        """清空后以本批导入条目作为底物列表，回到步骤一并默认全选。"""
        added, ingest_failed = self._ingest_scraped_dicts(items, replace_all=True)
        for g in self._step3_recipe_groups:
            self.step3_results_layout.removeWidget(g)
            g.deleteLater()
        self._step3_recipe_groups.clear()
        if self.step3_results_layout.count() > 0:
            item = self.step3_results_layout.takeAt(self.step3_results_layout.count() - 1)
            if item and item.spacerItem():
                del item
        self._step3_save_location_row.setVisible(False)
        self.step_stack.setCurrentIndex(0)
        msg = f"已载入{source_label}底物 {added} 条（已替换原底物列表）"
        if ingest_failed:
            msg += f"，未写入 {ingest_failed} 条"
        show_toast(self, msg, style="success" if added else "info")

    def _load_inventory_json_list(self, profile_id: str) -> list[dict]:
        path = profile_inventory_data_path(profile_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        return [row for row in raw if isinstance(row, dict)]

    def _inventory_item_to_alchemy_row(
        self, item: dict, price_map: dict | None
    ) -> dict | None:
        from core.alchemy_calc import lookup_inventory_item_price_value

        template = resolve_inventory_skin_template(item)
        if template is None:
            return None
        weapon = str(getattr(template, "weapon_name", "") or "").strip()
        skin = str(getattr(template, "skin_name", "") or "").strip()
        if not weapon:
            return None
        goods_name = f"{weapon} | {skin}" if skin else weapon
        wear = inventory_wear_chinese(item)
        if wear:
            goods_name = f"{goods_name}（{wear}）"
        raw_float = item.get("float", item.get("float_value"))
        try:
            float_value = float(raw_float)
        except (TypeError, ValueError):
            return None
        price = lookup_inventory_item_price_value(item, price_map)
        return {
            "float_value": float_value,
            "goods_id": str(item.get("assetid") or goods_name),
            "goods_name": goods_name,
            "platform": "inventory",
            "price": float(price or 0.0),
        }

    def _on_import_steam_inventory(self) -> None:
        from core.inventory_steam_accounts import list_profile_entries

        if not list_profile_entries():
            show_toast(
                self,
                "请先在「Steam 库存」登录账号并获取库存",
                style="warning",
            )
            return
        dialog = ImportSteamInventoryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_import()
        if not selected:
            return
        profile_id, mode = selected
        items = self._load_inventory_json_list(profile_id)
        if not items:
            show_toast(
                self,
                "该账号暂无本地库存，请先到「Steam 库存」点击获取库存",
                style="warning",
            )
            return
        price_map = try_build_product_price_map_from_disk()
        mapped: list[dict] = []
        skipped = 0
        for item in items:
            row = self._inventory_item_to_alchemy_row(item, price_map)
            if row is None:
                skipped += 1
                continue
            mapped.append(row)
        if not mapped:
            show_toast(
                self,
                f"未能导入：{skipped} 件缺少可识别皮肤或磨损",
                style="warning",
            )
            return
        if mode == "merge":
            self.apply_inventory_import_merge(mapped)
        else:
            self.apply_inventory_import_replace(mapped)
        if skipped:
            show_toast(
                self,
                f"另有 {skipped} 件因缺少模板或磨损未导入",
                style="info",
            )

    def _on_select_file(self):
        # 小助手自己保存的采集 JSON 是最常用的数据源，因此原生文件
        # 选择器优先从该目录打开；用户仍可在选择器中切换到任意目录。
        initial_directory = ""
        try:
            COLLECTED_JSON_DIR.mkdir(parents=True, exist_ok=True)
            initial_directory = str(COLLECTED_JSON_DIR)
        except OSError:
            # 数据目录暂时不可用时仍允许选择外部 JSON，不阻断导入流程。
            pass
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 JSON / JSONL 文件（可多选）",
            initial_directory,
            "数据文件 (*.jsonl *.json);;JSONL 文件 (*.jsonl);;JSON 文件 (*.json);;所有文件 (*.*)"
        )
        if paths:
            self._load_jsonl([Path(p) for p in paths])

    def _on_add_custom_items(self) -> None:
        dialog = CustomAlchemyItemDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selection = dialog.selection()
        if selection is None:
            show_toast(self, "请选择有效饰品", style="warning")
            return
        template, weapon_box_id, low, high, quantity, manual_price = selection
        span = max(0.0, high - low)
        price_map = None if manual_price > 0 else try_build_product_price_map_from_disk()
        added = 0
        unresolved_price = 0
        batch_id = time.time_ns()
        for index in range(quantity):
            ratio = (index + 0.5) / quantity
            wear = low + span * ratio if span > 0 else low
            wear = float(np.float32(max(template.min_float, min(template.max_float, wear))))
            instance = SkinInstance(
                skin_template=template,
                float_value=wear,
                price=manual_price,
                platform="custom",
            )
            price = float(manual_price)
            if price <= 0:
                matched = lookup_template_price_value(
                    template,
                    wear,
                    price_map,
                    weapon_box_id=weapon_box_id,
                )
                price = float(matched) if matched is not None else 0.0
                if price <= 0:
                    unresolved_price += 1
            row = {
                "float_value": wear,
                "goods_id": f"custom:{template.paint_index}:{batch_id}:{index}",
                "goods_name": instance.name,
                "platform": "custom",
                "price": price,
                "weapon_box_id": weapon_box_id,
            }
            if self._try_add_one_row(row):
                added += 1
        self._finalize_ingest(preserve_existing_states=True)
        if not added:
            show_toast(self, "自定义饰品与当前数据重复，未新增", style="warning")
            return
        message = f"已添加 {added} 件自定义饰品"
        if unresolved_price:
            message += f"；{unresolved_price} 件将在计算前再次匹配价格"
        show_toast(self, message, style="success")

    def _load_jsonl(self, paths: list[Path]):
        """读取 JSONL / JSON 文件，合并到现有数据。

        支持：
        - JSONL：每行一个对象
        - JSON：对象数组，或单个对象
        按规范化皮肤键+磨损(float32)+platform 去重（不区分 goods_id）。
        每条必须包含 float_value、goods_id、goods_name、platform、price。
        """
        new_count = 0
        fail_count = 0
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except Exception as e:
                show_toast(self, f"读取失败 {path.name}: {e}")
                continue
            if not text:
                continue
            rows: list = []
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = [payload]
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        fail_count += 1
            for data in rows:
                if self._try_add_one_row(data):
                    new_count += 1
                else:
                    fail_count += 1

        self._finalize_ingest(preserve_existing_states=True)
        if not paths:
            return
        if new_count > 0 or fail_count > 0:
            msg = f"新增 {new_count} 条数据"
            if fail_count:
                msg += f"，失败 {fail_count} 条"
            show_toast(self, msg, style="success" if new_count else "warning")
        else:
            show_toast(self, "无新增数据", style="success")

    def _build_grouped_from_all_data(self) -> dict[str, list]:
        """Group the same skin together, regardless of its wear suffix."""
        grouped: dict[str, list] = defaultdict(list)

        def _sort_key(item):
            key_t, it = item
            gn = self._group_display_name(it)
            fv = it.get("float_value", key_t[1])
            return (
                gn,
                float("-inf") if fv is None else (fv if isinstance(fv, (int, float)) else 0),
            )

        for key_tuple, item in sorted(self._all_data.items(), key=_sort_key):
            gkey = self._group_display_name(item)
            grouped[gkey].append(item)
        for items in grouped.values():
            items.sort(
                key=lambda row: (
                    float(row.get("float_value"))
                    if isinstance(row.get("float_value"), (int, float))
                    else float("inf")
                )
            )
        return grouped

    @staticmethod
    def _group_display_name(item: dict) -> str:
        raw = str(item.get("goods_name") or "").strip()
        template = get_template_from_goods_name(raw)
        if template is not None:
            weapon = str(template.weapon_name or "").strip()
            skin = str(template.skin_name or "").strip()
            return f"{weapon} | {skin}" if skin else weapon
        return strip_appearance_suffix_from_goods_name(raw) or raw or "未知饰品"

    def _display_groups(
        self,
        grouped: dict[str, list],
        *,
        preserved_row_states: dict[str, tuple[bool, bool]] | None = None,
        expanded_goods_names: set[str] | None = None,
    ):
        """清空并重新渲染聚合组"""
        for g in self._groups:
            self.groups_layout.removeWidget(g)
            g.deleteLater()
        self._groups.clear()
        self._substrate_group_by_slot_key.clear()
        # 移除末尾的 stretch
        if self.groups_layout.count() > 0:
            item = self.groups_layout.takeAt(self.groups_layout.count() - 1)
            if item and item.spacerItem():
                del item

        for goods_name in sorted(grouped.keys(), key=_substrate_group_sort_key):
            items = grouped[goods_name]
            group = CollapsibleGroup(
                goods_name, items, self.groups_container,
                on_selection_changed=self._refresh_step1_count
            )
            self.groups_layout.addWidget(group)
            self._groups.append(group)
        self._rebuild_substrate_lookup_index()
        if preserved_row_states or expanded_goods_names:
            self._restore_group_ui_state(
                preserved_row_states or {},
                expanded_goods_names or set(),
            )
            self._refresh_selected_data_from_groups()
        self.groups_layout.addStretch(1)
        self._refresh_step1_count()

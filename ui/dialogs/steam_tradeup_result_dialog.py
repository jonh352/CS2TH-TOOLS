"""Successful Steam trade-up result dialog."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.alchemy_quality import get_pid_map, get_template_from_goods_name
from core.inventory_icons import weapon_image_path_from_skin_template
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.modal_shell import (
    MODAL_WIDTH_LG,
    add_modal_footer_buttons,
    build_frameless_modal_content,
)


def _product_image_path(product: dict[str, Any]) -> str | None:
    template = None
    paint_index = str(product.get("paint_index") or "").strip()
    if paint_index:
        template = get_pid_map().get(paint_index)
    if template is None:
        template = get_template_from_goods_name(str(product.get("name") or ""))
    return weapon_image_path_from_skin_template(template) if template is not None else None


class SteamTradeupResultDialog(QDialog):
    """Show the product returned directly by the CS2 GC craft response."""

    def __init__(
        self,
        parent: QWidget | None,
        products: list[dict[str, Any]],
        *,
        output_asset_ids: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("汰换成功")
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, _box, layout, close_btn = build_frameless_modal_content(
            self,
            "汰换成功",
            "Steam 已返回本次真实产物，结果已保存到汰换记录。",
            box_width=MODAL_WIDTH_LG,
        )
        root.addWidget(overlay)
        close_btn.clicked.connect(self.accept)

        shown_products = [dict(row) for row in products if isinstance(row, dict)]
        if shown_products:
            for product in shown_products:
                layout.addWidget(self._build_product_card(product))
        else:
            asset_ids = [str(value) for value in output_asset_ids or [] if str(value)]
            suffix = f"（资产 ID：{', '.join(asset_ids)}）" if asset_ids else ""
            fallback = QLabel(f"产物已生成{suffix}，请刷新 Steam 库存查看详情。")
            fallback.setObjectName("steamTradeupResultFallback")
            fallback.setWordWrap(True)
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(fallback)

        cancel_btn, ok_btn = add_modal_footer_buttons(
            layout,
            cancel_text="",
            ok_text="确定",
            on_ok=self.accept,
        )
        cancel_btn.hide()
        ok_btn.setDefault(True)
        install_dialog_topmost_follow_parent(self)

    def _build_product_card(self, product: dict[str, Any]) -> QFrame:
        card = QFrame(self)
        card.setObjectName("steamTradeupResultCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(8)

        image_label = QLabel(card)
        image_label.setObjectName("steamTradeupResultImage")
        image_label.setFixedSize(360, 210)
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_path = _product_image_path(product)
        pixmap = QPixmap(image_path) if image_path else QPixmap()
        if not pixmap.isNull():
            image_label.setPixmap(
                pixmap.scaled(
                    340,
                    190,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            image_label.setText("暂无本地产物图片")
        card_layout.addWidget(image_label, 0, Qt.AlignmentFlag.AlignHCenter)

        name_label = QLabel(str(product.get("name") or "未知产物"), card)
        name_label.setObjectName("steamTradeupResultName")
        name_label.setWordWrap(True)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(name_label)

        details: list[str] = []
        float_value = product.get("float_value")
        try:
            details.append(f"磨损：{float(float_value):.10f}")
        except (TypeError, ValueError):
            details.append("磨损：待刷新")
        price = product.get("price")
        try:
            details.append(f"参考价：¥{float(price):.2f}")
        except (TypeError, ValueError):
            details.append("参考价：待刷新")
        asset_id = str(product.get("asset_id") or "")
        if asset_id:
            details.append(f"资产 ID：{asset_id}")
        meta_label = QLabel("　·　".join(details), card)
        meta_label.setObjectName("steamTradeupResultMeta")
        meta_label.setWordWrap(True)
        meta_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(meta_label)
        return card

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())

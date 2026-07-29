"""炼金页：多选已保存配方，用于「排除配方」或按配方元数据「导入数据」到底物列表。

按文件夹分组展示（无配方文件夹不出现）；文件夹可展开/收起，勾选文件夹即全选/全不选其下配方。
样式：与登录 / 确认弹窗相同的遮罩 + loginBox，随主窗口主题 QSS。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.saved_recipes import load_recipe_folders
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.icons import expand_section_triangle_icon
from ui.pages.recipe_manage import _display_row_title, _format_saved_at_local
from ui.widgets.toast import show_toast

_ROLE = Qt.ItemDataRole.UserRole
_KIND_FOLDER = "exclude_folder"
_KIND_RECIPE = "exclude_recipe"

_FOLDER_TRIANGLE_ICON_PX = 14
# 与 theme/dialogs.qss 中 QTreeWidget#excludeRecipeListWidget::indicator 宽高一致
_EXCLUDE_TREE_CHECK_INDICATOR_PX = 18
# 在指示器外再扩一圈命中区（文件夹行右边界不超过三角图标左侧，避免与展开区域抢判）
_EXCLUDE_TREE_CHECK_HIT_MARGIN_PX = 12


def _list_row_label(payload: dict[str, Any]) -> str:
    """配方展示标题 + 保存时间（与配方管理页时间格式一致）。"""
    title = _display_row_title(payload)
    saved = _format_saved_at_local(str(payload.get("saved_at") or ""))
    if saved:
        return f"{title}　·　保存时间：{saved}"
    return title


def _apply_folder_disclosure_icon(
    item: QTreeWidgetItem, expanded: bool, tree: QTreeWidget
) -> None:
    """文件夹行左侧：triangle-fill.svg 旋转后的图标（与 setRootIsDecorated(False) 配合）。"""
    data = item.data(0, _ROLE)
    if not isinstance(data, dict) or data.get("kind") != _KIND_FOLDER:
        return
    fill = tree.palette().color(QPalette.ColorRole.Shadow).name()
    icon = expand_section_triangle_icon(
        expanded, size_px=_FOLDER_TRIANGLE_ICON_PX, fill_color=fill
    )
    tree.blockSignals(True)
    try:
        item.setIcon(0, icon)
    finally:
        tree.blockSignals(False)


def _group_entries_by_folder(
    entries: list[tuple[Path, dict[str, Any]]],
) -> list[tuple[str | None, str, list[tuple[Path, dict[str, Any]]]]]:
    """按文件夹分组；仅返回至少含一条配方的组。顺序：未分类 → 索引中的文件夹 → 孤儿 folder_id。"""
    uncat: list[tuple[Path, dict[str, Any]]] = []
    by_fid: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path, payload in entries:
        fid = str(payload.get("folder_id") or "").strip()
        if not fid:
            uncat.append((path, payload))
        else:
            by_fid.setdefault(fid, []).append((path, payload))
    groups: list[tuple[str | None, str, list[tuple[Path, dict[str, Any]]]]] = []
    if uncat:
        groups.append((None, f"未分类 ({len(uncat)})", uncat))
    known: set[str] = set()
    for f in load_recipe_folders():
        fid = str(f.get("id") or "")
        if not fid:
            continue
        lst = by_fid.get(fid)
        if not lst:
            continue
        known.add(fid)
        name = str(f.get("name") or "").strip() or "文件夹"
        groups.append((fid, f"{name} ({len(lst)})", lst))
    for fid in sorted(by_fid.keys() - known):
        lst = by_fid[fid]
        groups.append((fid, f"其他 ({len(lst)})", lst))
    return groups


class _ExcludeRecipeTree(QTreeWidget):
    """文件夹：仅三角图标与标题文字区域展开/收起，复选框区域走 Qt 勾选；配方行：点击整行切换勾选（复选框仍走默认）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(18)
        self.setAnimated(True)
        # 与 Qt 默认 branch 二选一：文件夹行用 triangle-fill.svg 旋转图标；展开命中由本类 mouseReleaseEvent 处理
        self.setRootIsDecorated(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 勿在选中行上把左侧复选框/图标区整块刷成 palette(highlight)（常见为蓝条）
        if hasattr(self, "setShowDecorationSelected"):
            self.setShowDecorationSelected(False)
        # 勾选状态只靠复选框；若允许行选中，QSS 的 ::item:selected 会在点击后出现「加深」高亮
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

    def _style_option_for_index(self, idx) -> QStyleOptionViewItem:
        """indexAt / subElementRect 均基于 viewport 坐标。部分 PySide6 未导出 viewOptions()，故手动填充选项。"""
        opt = QStyleOptionViewItem()
        opt.initFrom(self)
        opt.rect = self.visualRect(idx)
        opt.widget = self
        opt.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        raw_sds = getattr(self, "showDecorationSelected", True)
        opt.showDecorationSelected = (
            bool(raw_sds()) if callable(raw_sds) else bool(raw_sds)
        )
        item = self.itemFromIndex(idx)
        if item is None:
            return opt
        opt.text = item.text(0)
        ico = item.icon(0)
        if not ico.isNull():
            opt.icon = ico
            opt.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        opt.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        cs = item.checkState(0)
        if cs == Qt.CheckState.Checked:
            opt.state |= QStyle.StateFlag.State_On
        elif cs == Qt.CheckState.PartiallyChecked:
            opt.state |= QStyle.StateFlag.State_NoChange
        else:
            opt.state |= QStyle.StateFlag.State_Off
        if item.childCount() > 0:
            opt.state |= QStyle.StateFlag.State_Children
            if item.isExpanded():
                opt.state |= QStyle.StateFlag.State_Open
        return opt

    def _check_indicator_hit_rect(self, opt: QStyleOptionViewItem) -> QRect:
        """在 QStyle 指示器基础上放大命中区（与 QSS 18px 对齐），不压到三角装饰左侧。"""
        sty = self.style()
        vp = self.viewport()
        r = sty.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator,
            opt,
            vp,
        )
        if not r.isValid():
            return r
        w = max(r.width(), _EXCLUDE_TREE_CHECK_INDICATOR_PX)
        h = max(r.height(), _EXCLUDE_TREE_CHECK_INDICATOR_PX)
        inflated = QRect(r.left(), r.top(), w, h)
        m = _EXCLUDE_TREE_CHECK_HIT_MARGIN_PX
        inflated = inflated.adjusted(-m, -m, m, m)
        if opt.features & QStyleOptionViewItem.ViewItemFeature.HasDecoration:
            dec = sty.subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration, opt, vp
            )
            if dec.isValid() and inflated.right() >= dec.left():
                inflated.setRight(dec.left() - 2)
        inflated = inflated.intersected(opt.rect)
        if inflated.width() < 1 or inflated.height() < 1:
            return QRect(r.left(), r.top(), w, h)
        return inflated

    def _folder_expand_hit_rect(self, opt: QStyleOptionViewItem) -> QRect:
        """文件夹行：三角装饰 + 文字区域（不含复选框与行内空白边）。"""
        sty = self.style()
        vp = self.viewport()
        dec = sty.subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, opt, vp
        )
        txt = sty.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, vp)
        u = QRect()
        if dec.isValid():
            u = u.united(dec)
        if txt.isValid():
            u = u.united(txt)
        return u

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            vp_pos = self.viewport().mapFrom(self, event.position().toPoint())
            idx = self.indexAt(vp_pos)
            if idx.isValid() and idx.column() == 0:
                item = self.itemFromIndex(idx)
                if item is not None:
                    if (
                        item.childCount() > 0
                        and self.indexOfTopLevelItem(item) >= 0
                    ):
                        opt = self._style_option_for_index(idx)
                        check = self._check_indicator_hit_rect(opt)
                        if check.isValid() and check.contains(vp_pos):
                            super().mouseReleaseEvent(event)
                            return
                        expand_hit = self._folder_expand_hit_rect(opt)
                        if expand_hit.isValid() and expand_hit.contains(vp_pos):
                            item.setExpanded(not item.isExpanded())
                        event.accept()
                        return
                    data = item.data(0, _ROLE)
                    if isinstance(data, dict) and data.get("kind") == _KIND_RECIPE:
                        opt = self._style_option_for_index(idx)
                        check = self._check_indicator_hit_rect(opt)
                        if check.isValid() and check.contains(vp_pos):
                            super().mouseReleaseEvent(event)
                            return
                        cs = item.checkState(0)
                        item.setCheckState(
                            0,
                            Qt.CheckState.Unchecked
                            if cs == Qt.CheckState.Checked
                            else Qt.CheckState.Checked,
                        )
                        event.accept()
                        return
        super().mouseReleaseEvent(event)


class _DragHeaderBar(QWidget):
    """无框弹窗标题栏：按住拖动整个对话框。"""

    def __init__(self, dialog: QDialog) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._press_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_offset = event.globalPosition().toPoint() - self._dialog.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._dialog.move(event.globalPosition().toPoint() - self._press_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_offset = None
        super().mouseReleaseEvent(event)


class ExcludeSavedRecipesDialog(QDialog):
    """按文件夹展示已保存配方，支持文件夹/配方勾选与展开收起。"""

    def __init__(
        self,
        parent,
        entries: list[tuple[Path, dict[str, Any]]],
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ExcludeSavedRecipesDialog")
        self.setWindowTitle("配方数据")
        self.setModal(True)
        self._chosen_action: str | None = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        overlay = QWidget(self)
        overlay.setObjectName("excludeRecipeOverlay")
        overlay.setAttribute(Qt.WA_StyledBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(overlay)

        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QFrame()
        box.setObjectName("loginBox")
        box.setMinimumWidth(520)
        box.setMaximumWidth(720)
        box.setMinimumHeight(440)

        layout = QVBoxLayout(box)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        header = _DragHeaderBar(self)
        header.setMinimumHeight(40)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel("配方数据")
        title_label.setObjectName("loginTitle")
        header_row.addWidget(title_label)
        header_row.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setObjectName("loginCloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.reject)
        header_row.addWidget(close_btn)
        layout.addWidget(header)

        hint = QLabel(
            "按文件夹勾选配方。勾选文件夹将全选其下配方，取消勾选则全部取消。\n"
            "「排除配方」：取消勾选当前列表中与所选配方底物（皮肤 + 磨损）相同的行；\n"
            "「导入数据」：将配方中每个底物与当前列表按名称、磨损、平台匹配后同步状态。"
            "在配方管理中为底物设置的「排除 / 锁定」会一并写入；未设置标记的底物视为「参与计算、非必选」，同样会参与同步。"
        )
        hint.setObjectName("alchemyStep1Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._tree = _ExcludeRecipeTree()
        self._tree.setObjectName("excludeRecipeListWidget")
        self._tree.setAlternatingRowColors(False)
        self._tree.setMinimumHeight(260)
        self._tree.itemExpanded.connect(
            lambda it: _apply_folder_disclosure_icon(it, True, self._tree)
        )
        self._tree.itemCollapsed.connect(
            lambda it: _apply_folder_disclosure_icon(it, False, self._tree)
        )
        groups = _group_entries_by_folder(entries)
        for _key, title, rows in groups:
            folder_item = QTreeWidgetItem([title])
            folder_item.setFlags(
                folder_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            folder_item.setCheckState(0, Qt.CheckState.Unchecked)
            folder_item.setData(0, _ROLE, {"kind": _KIND_FOLDER})
            for _path, payload in rows:
                label = _list_row_label(payload)
                child = QTreeWidgetItem(folder_item, [label])
                child.setIcon(0, QIcon())
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setData(0, _ROLE, {"kind": _KIND_RECIPE, "payload": payload})
            self._tree.addTopLevelItem(folder_item)
            folder_item.setExpanded(False)
            _apply_folder_disclosure_icon(
                folder_item, folder_item.isExpanded(), self._tree
            )
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        layout.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._select_toggle_btn = QPushButton("全选")
        self._select_toggle_btn.setObjectName("alchemySelectFileBtn")
        self._select_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_toggle_btn.clicked.connect(self._on_select_toggle_clicked)
        btn_row.addWidget(self._select_toggle_btn)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("alchemyClearFileBtn")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.clicked.connect(self.reject)
        exclude_btn = QPushButton("排除配方")
        exclude_btn.setObjectName("alchemyClearFileBtn")
        exclude_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        exclude_btn.setAutoDefault(False)
        exclude_btn.setDefault(False)
        exclude_btn.clicked.connect(self._on_exclude_recipes_clicked)
        import_btn = QPushButton("导入数据")
        import_btn.setObjectName("alchemySelectFileBtn")
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.setDefault(True)
        import_btn.clicked.connect(self._on_import_data_clicked)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(exclude_btn)
        btn_row.addWidget(import_btn)
        layout.addLayout(btn_row)

        overlay_layout.addWidget(box)

        def on_overlay_click(event):
            if event.button() == Qt.MouseButton.LeftButton:
                w = overlay.childAt(event.pos().x(), event.pos().y())
                if w is None or (w != box and not box.isAncestorOf(w)):
                    self.reject()

        overlay.mousePressEvent = on_overlay_click

        self._refresh_select_toggle_btn_text()
        install_dialog_topmost_follow_parent(self)
        # 顶层 QDialog 在首次 Show 前若未设 minimum，Win32 可能先以 ~100x30 建窗，再与布局最小尺寸冲突并刷
        # QWindowsWindow::setGeometry 警告；与内部 loginBox 下限对齐。
        self.setMinimumSize(480, 500)

    def chosen_action(self) -> str | None:
        """``accept`` 后 ``exclude`` 或 ``import``；取消则为 None。"""
        return self._chosen_action

    def _require_selected_payloads(self) -> list[dict[str, Any]] | None:
        pl = self.selected_payloads()
        if not pl:
            show_toast(self, "请先勾选配方", style="warning")
            return None
        return pl

    def _on_exclude_recipes_clicked(self) -> None:
        pl = self._require_selected_payloads()
        if pl is None:
            return
        self._chosen_action = "exclude"
        self.accept()

    def _on_import_data_clicked(self) -> None:
        pl = self._require_selected_payloads()
        if pl is None:
            return
        self._chosen_action = "import"
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        # 在 super 之后布局最小尺寸已确定，再铺几何并与弹窗 minimum 取大，避免 QWindowsWindow::setGeometry 警告。
        apply_frameless_modal_geometry(self, self.parentWidget())

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        try:
            data = item.data(0, _ROLE)
            if not isinstance(data, dict):
                return
            kind = data.get("kind")
            if kind == _KIND_FOLDER:
                st = item.checkState(0)
                if st == Qt.CheckState.PartiallyChecked:
                    return
                self._tree.blockSignals(True)
                try:
                    for i in range(item.childCount()):
                        ch = item.child(i)
                        ch.setCheckState(0, st)
                finally:
                    self._tree.blockSignals(False)
            elif kind == _KIND_RECIPE:
                parent = item.parent()
                if parent is None:
                    return
                self._tree.blockSignals(True)
                try:
                    self._sync_folder_check_from_children(parent)
                finally:
                    self._tree.blockSignals(False)
        finally:
            self._refresh_select_toggle_btn_text()

    def _all_recipe_rows_checked(self) -> bool:
        total = 0
        checked = 0
        for ti in range(self._tree.topLevelItemCount()):
            folder = self._tree.topLevelItem(ti)
            for j in range(folder.childCount()):
                total += 1
                if folder.child(j).checkState(0) == Qt.CheckState.Checked:
                    checked += 1
        if total == 0:
            return False
        return checked == total

    def _refresh_select_toggle_btn_text(self) -> None:
        self._select_toggle_btn.setText(
            "全不选" if self._all_recipe_rows_checked() else "全选"
        )

    def _on_select_toggle_clicked(self) -> None:
        if self._all_recipe_rows_checked():
            self._select_none()
        else:
            self._select_all()
        self._refresh_select_toggle_btn_text()

    @staticmethod
    def _sync_folder_check_from_children(folder: QTreeWidgetItem) -> None:
        n = folder.childCount()
        if n == 0:
            return
        checked = 0
        unchecked = 0
        for i in range(n):
            cs = folder.child(i).checkState(0)
            if cs == Qt.CheckState.Checked:
                checked += 1
            elif cs == Qt.CheckState.Unchecked:
                unchecked += 1
        if checked == n:
            folder.setCheckState(0, Qt.CheckState.Checked)
        elif unchecked == n:
            folder.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            folder.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _select_all(self) -> None:
        self._tree.blockSignals(True)
        try:
            for ti in range(self._tree.topLevelItemCount()):
                folder = self._tree.topLevelItem(ti)
                for j in range(folder.childCount()):
                    folder.child(j).setCheckState(0, Qt.CheckState.Checked)
                folder.setCheckState(0, Qt.CheckState.Checked)
        finally:
            self._tree.blockSignals(False)
        self._refresh_select_toggle_btn_text()

    def _select_none(self) -> None:
        self._tree.blockSignals(True)
        try:
            for ti in range(self._tree.topLevelItemCount()):
                folder = self._tree.topLevelItem(ti)
                for j in range(folder.childCount()):
                    folder.child(j).setCheckState(0, Qt.CheckState.Unchecked)
                folder.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._tree.blockSignals(False)
        self._refresh_select_toggle_btn_text()

    def selected_payloads(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ti in range(self._tree.topLevelItemCount()):
            folder = self._tree.topLevelItem(ti)
            for j in range(folder.childCount()):
                ch = folder.child(j)
                if ch.checkState(0) != Qt.CheckState.Checked:
                    continue
                data = ch.data(0, _ROLE)
                if not isinstance(data, dict) or data.get("kind") != _KIND_RECIPE:
                    continue
                pl = data.get("payload")
                if isinstance(pl, dict):
                    out.append(pl)
        return out

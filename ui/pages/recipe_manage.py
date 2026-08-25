"""配方管理页：文件夹筛选、搜索、批量操作；左侧文件夹列表支持拖拽排序。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QMimeData,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCursor,
    QDrag,
    QDesktopServices,
    QFocusEvent,
    QFont,
    QHideEvent,
    QMouseEvent,
    QPalette,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from config import COLLECTED_JSON_DIR, CONTENT_PAGE_LAYOUT_MARGINS, RECIPE_ICON_PATH
from core.auth_client import AuthClient
from core.collected_json import list_collected_json
from core.recipe_bridge import cs2th_detail_to_saved_recipe
from core.saved_recipes import (
    ERR_DUPLICATE_RECIPE_FOLDER_NAME,
    create_recipe_folder,
    delete_recipe_files,
    delete_recipe_folder,
    delete_recipe_folder_and_recipes,
    format_recipe_summary_line,
    get_recipe_folder_stats,
    list_saved_recipes,
    load_recipe_folders,
    move_recipes_to_folder,
    rename_recipe_folder,
    rename_saved_recipe_title,
    reorder_recipe_folders,
    save_recipe_file,
)
from core.inventory_steam_accounts import (
    combo_display_name_for_profile,
    get_active_profile_id,
    list_profile_entries,
    load_steam_account_config_dict,
)
from core.purchase_tracking import load_profile_inventory_items
from core.purchase_batches import (
    create_purchase_batch,
    list_purchase_batches,
    purchase_batch_summary,
    update_purchase_batch_account,
)
from ui.feedback import ask_confirmation, ask_confirmation_sequence
from ui.dialogs.move_recipe_folder_dialog import MoveRecipeFolderDialog, build_move_targets
from ui.dialogs.wide_text_input_dialog import get_wide_text_input
from ui.icons import expand_section_triangle_icon, load_svg_icon
from ui.widgets.recipe_result_group import RecipeResultGroup
from ui.widgets.purchase_batch_card import PurchaseBatchCard
from ui.widgets.toast import show_toast
from ui.workers.recipe_bridge import RecipeLoadThread

FOLDER_KEY_UNCAT = "_uncat"
# 旧版左侧「全部」项的 UserRole，已移除；遇此值时按未分类处理
_LEGACY_FOLDER_KEY_ALL = "_all"
# 内联「新建文件夹」占位行：不可选、不可拖拽排序
FOLDER_SIDEBAR_PENDING_NEW_KEY = "__recipe_sidebar_pending_new__"
# 侧栏文件夹排序：自定义 MIME，避免 QListWidget 默认拖拽与 viewport 作 source 导致误走 super().dropEvent、项丢失
FOLDER_REORDER_MIME = "application/x-cshelper-folder-reorder"
# 拖动文件夹时：距 viewport 上/下边界的自动滚动触发带（像素上限；实际 band = min(本值, 视口高约 1/3)，且不超过视口一半）
_FOLDER_LIST_DRAG_AUTOSCROLL_MARGIN = 96
# 边缘自动滚动：定时器间隔（毫秒）越大整体越慢
_FOLDER_LIST_DRAG_AUTOSCROLL_INTERVAL_MS = 55
# 每 tick 滚动像素：刚进入边缘带最慢，越靠近上/下边界越快（按深入距离平方插值）
_FOLDER_LIST_DRAG_AUTOSCROLL_MIN_PX = 1
_FOLDER_LIST_DRAG_AUTOSCROLL_MAX_PX = 16
# 拖动文件夹时插入位置预览线（viewport 内高度，黑色）
_FOLDER_LIST_INSERT_PREVIEW_LINE_H = 2

# 左侧文件夹栏固定宽度；与右侧内容区之间的间距（像素）
_RECIPE_LEFT_PANEL_WIDTH = 240
_RECIPE_LEFT_RIGHT_GAP = 24

# 主窗口样式表不会作用到 QMenu 弹出层（与 tray 菜单相同），需挂在菜单自身。
# Windows 上鼠标划过项常用 :selected 而非 :hover，故两者都写。
_RECIPE_MANAGE_MENU_QSS = """
QMenu#recipeManageMenu::item:hover,
QMenu#recipeManageMenu::item:selected {
    background: palette(mid);
}
"""


def _apply_recipe_manage_menu_style(menu: QMenu) -> None:
    menu.setObjectName("recipeManageMenu")
    menu.setStyleSheet(_RECIPE_MANAGE_MENU_QSS)


def _widget_is_under_tree(root: QWidget | None, w: QWidget | None) -> bool:
    cur: QWidget | None = w
    while cur is not None:
        if cur is root:
            return True
        cur = cur.parentWidget()
    return False


def _format_saved_at_local(iso_str: str) -> str:
    if not iso_str or not isinstance(iso_str, str):
        return str(iso_str) if iso_str else ""
    s = iso_str.strip()
    if not s:
        return ""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return (
            f"{local.year}年{local.month}月{local.day}日 "
            f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
        )
    except (ValueError, TypeError, OSError):
        return iso_str


def _display_row_title(payload: dict[str, Any]) -> str:
    """列表主标题直接读取保存到 JSON 顶层的 ``title``。"""
    saved = (payload.get("title") or "").strip()
    return saved or "未命名配方"


def _folder_filter_from_key(key: str) -> str | None:
    if key == _LEGACY_FOLDER_KEY_ALL:
        return ""
    if key == FOLDER_KEY_UNCAT:
        return ""
    return key


def _sidebar_folder_key_normalized(key: str) -> str:
    """侧栏 UserRole 与 _folder_list_selected_key 比较用（旧版「全部」等同未分类）。"""
    if key == _LEGACY_FOLDER_KEY_ALL:
        return FOLDER_KEY_UNCAT
    return key


def _folder_display_name(folder_id: str) -> str:
    for f in load_recipe_folders():
        if str(f.get("id")) == folder_id:
            return str(f.get("name") or "").strip() or "文件夹"
    return "文件夹"


def _qmouse_global_point(me: QMouseEvent) -> QPoint:
    """兼容 Qt6（globalPosition）与旧绑定（globalPos）。"""
    if hasattr(me, "globalPosition"):
        return me.globalPosition().toPoint()
    return me.globalPos()


def _folder_list_item_role_key(item: QListWidgetItem | None) -> str | None:
    """UserRole 在 Qt 中可能为 QVariant/非 str，统一成 str 再比较，避免未分类行被误判为普通文件夹。"""
    if item is None:
        return None
    k = item.data(Qt.ItemDataRole.UserRole)
    if k is None:
        return None
    s = str(k).strip()
    return s if s else None


class _FolderList(QListWidget):
    """左侧文件夹列表：用户文件夹可拖拽排序（未分类固定首行，新建占位行固定末尾）。"""

    folder_order_committed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recipeFolderList")
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSpacing(2)
        self._folder_drag_autoscroll_delta = 0
        self._folder_drag_autoscroll_timer = QTimer(self)
        self._folder_drag_autoscroll_timer.setInterval(_FOLDER_LIST_DRAG_AUTOSCROLL_INTERVAL_MS)
        self._folder_drag_autoscroll_timer.timeout.connect(self._folder_drag_autoscroll_tick)
        self._folder_insert_line = QFrame(self.viewport())
        self._folder_insert_line.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._folder_insert_line.setObjectName("recipeFolderInsertLine")
        self._folder_insert_line.hide()
        self._suppress_scroll_to_for_folder_reorder = False

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible) -> None:
        if self._suppress_scroll_to_for_folder_reorder:
            return
        super().scrollTo(index, hint)

    def _hide_folder_insert_preview_line(self) -> None:
        self._folder_insert_line.hide()

    def _folder_insert_line_y_for_insert_index(self, insert_pos: int) -> int | None:
        """insert_pos 与 drop 时 insertItem 语义一致：n 表示末尾之后。"""
        n = self.count()
        if n <= 0:
            return None
        insert_pos = max(0, min(insert_pos, n))
        if insert_pos >= n:
            last = self.item(n - 1)
            if last is None:
                return None
            return self.visualItemRect(last).bottom() + 1
        it = self.item(insert_pos)
        if it is None:
            return None
        return self.visualItemRect(it).top()

    def _update_folder_insert_preview_line_from_cursor(self) -> None:
        vp = self.viewport()
        vp_pos = vp.mapFromGlobal(QCursor.pos())
        raw = self._insert_pos_from_drop_viewport_pos(vp_pos)
        p = max(1, min(raw, self.count()))
        y = self._folder_insert_line_y_for_insert_index(p)
        if y is None:
            self._folder_insert_line.hide()
            return
        h = _FOLDER_LIST_INSERT_PREVIEW_LINE_H
        self._folder_insert_line.setGeometry(
            0, max(0, y - h // 2), max(1, vp.width()), h
        )
        self._folder_insert_line.show()
        self._folder_insert_line.raise_()

    def _stop_folder_drag_autoscroll(self) -> None:
        self._folder_drag_autoscroll_delta = 0
        self._folder_drag_autoscroll_timer.stop()

    def _viewport_pos_from_drag_move_event(self, event) -> QPoint:
        vp = self.viewport()
        try:
            if hasattr(event, "globalPosition"):
                return vp.mapFromGlobal(event.globalPosition().toPoint())
        except (AttributeError, TypeError):
            pass
        try:
            if hasattr(event, "globalPos"):
                return vp.mapFromGlobal(event.globalPos())
        except (AttributeError, TypeError):
            pass
        # 与 QAbstractItemView::dragMoveEvent 一致：position 在 viewport 坐标内
        try:
            if hasattr(event, "position"):
                pf = event.position()
                return pf.toPoint() if hasattr(pf, "toPoint") else QPoint(int(pf.x()), int(pf.y()))
        except (AttributeError, TypeError):
            pass
        try:
            return QPoint(event.pos())
        except (AttributeError, TypeError):
            pass
        return vp.mapFromGlobal(QCursor.pos())

    def _folder_drag_autoscroll_tick(self) -> None:
        d = self._folder_drag_autoscroll_delta
        if d == 0:
            self._folder_drag_autoscroll_timer.stop()
            return
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + d)
        self._update_folder_insert_preview_line_from_cursor()

    def startDrag(self, supportedActions) -> None:
        _ = supportedActions  # 仅使用自定义 MIME + MoveAction，不调用 super，避免 Qt 内置 model 拖拽与 drop 冲突
        item = self.currentItem()
        if item is None:
            return
        fid = _folder_list_item_role_key(item)
        if not fid or fid in (
            FOLDER_KEY_UNCAT,
            _LEGACY_FOLDER_KEY_ALL,
            FOLDER_SIDEBAR_PENDING_NEW_KEY,
        ):
            return
        md = QMimeData()
        md.setData(FOLDER_REORDER_MIME, fid.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(md)
        rect = self.visualItemRect(item)
        if rect.isValid() and rect.width() > 0 and rect.height() > 0:
            pm = self.viewport().grab(QRect(rect))
            if pm is not None and not pm.isNull():
                drag.setPixmap(pm)
                pos_in_vp = self.viewport().mapFromGlobal(QCursor.pos())
                hot = pos_in_vp - rect.topLeft()
                drag.setHotSpot(
                    QPoint(
                        max(0, min(hot.x(), max(0, rect.width() - 1))),
                        max(0, min(hot.y(), max(0, rect.height() - 1))),
                    )
                )
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._stop_folder_drag_autoscroll()
            self._hide_folder_insert_preview_line()

    def _viewport_pos_for_folder_drop(self, event) -> QPoint:
        """Drop 坐标为 viewport 坐标，勿再 mapFrom(self, …)。"""
        try:
            if hasattr(event, "position"):
                pf = event.position()
                return pf.toPoint() if hasattr(pf, "toPoint") else QPoint(int(pf.x()), int(pf.y()))
        except (AttributeError, TypeError):
            pass
        try:
            if hasattr(event, "pos"):
                return QPoint(event.pos())
        except (AttributeError, TypeError):
            pass
        return self.viewport().mapFromGlobal(QCursor.pos())

    def _insert_pos_from_drop_viewport_pos(self, vp_pos: QPoint) -> int:
        """
        根据 viewport 内纵坐标决定插入索引。
        QListWidget 带 spacing 时，行与行之间有缝隙，indexAt 常返回无效，若回退到 count() 会总插到末尾。
        因此只用每行 visualItemRect 的 Y 范围判断，并处理行间缝隙。
        """
        n = self.count()
        if n <= 0:
            return 0
        y = vp_pos.y()

        rows_rects: list[tuple[int, QRect]] = []
        for row in range(n):
            it = self.item(row)
            if it is None:
                continue
            r = self.visualItemRect(it)
            if not r.isValid() or r.height() <= 0:
                continue
            rows_rects.append((row, r))

        if not rows_rects:
            return n

        first_row, r0 = rows_rects[0]
        if y < r0.top():
            return first_row

        last_row, r_last = rows_rects[-1]
        if y > r_last.bottom():
            return n

        for i, (row, r) in enumerate(rows_rects):
            if r.top() <= y <= r.bottom():
                mid_y = r.top() + r.height() // 2
                return row if y < mid_y else row + 1
            if i > 0:
                _, r_prev = rows_rects[i - 1]
                if r_prev.bottom() < y < r.top():
                    return row

        best_row = rows_rects[0][0]
        best_d = 10**9
        for row, r in rows_rects:
            cy = r.center().y()
            d = abs(y - cy)
            if d < best_d:
                best_d = d
                best_row = row
        r_near = next(rr for rr in rows_rects if rr[0] == best_row)[1]
        mid_y = r_near.top() + r_near.height() // 2
        return best_row if y < mid_y else best_row + 1

    def _normalize_folder_rows_after_internal_move(self, *, restore_scroll_value: int) -> bool:
        """未分类固定首行、新建占位固定末尾；失败则恢复原列表。restore_scroll_value 须为 takeItem 前的纵向滚动值。"""
        sb = self.verticalScrollBar()
        vscroll = int(restore_scroll_value)
        snapshot: list[QListWidgetItem] = []
        while self.count() > 0:
            it = self.takeItem(0)
            if it is not None:
                snapshot.append(it)
        uncat: QListWidgetItem | None = None
        pending: QListWidgetItem | None = None
        user_order: list[QListWidgetItem] = []
        for it in snapshot:
            k = _folder_list_item_role_key(it)
            if k == FOLDER_KEY_UNCAT or k == _LEGACY_FOLDER_KEY_ALL:
                uncat = it
            elif k == FOLDER_SIDEBAR_PENDING_NEW_KEY:
                pending = it
            else:
                user_order.append(it)
        if uncat is None:
            for it in snapshot:
                self.addItem(it)
            sb.setValue(min(max(0, vscroll), sb.maximum()))
            return False
        stats = get_recipe_folder_stats()
        uncategorized = int(stats.get("uncategorized") or 0)
        uncat.setText(f"未分类 ({uncategorized})")
        uncat.setData(Qt.ItemDataRole.UserRole, FOLDER_KEY_UNCAT)
        self.addItem(uncat)
        for it in user_order:
            self.addItem(it)
        if pending is not None:
            self.addItem(pending)
        sb.setValue(min(max(0, vscroll), sb.maximum()))
        return True

    def dragEnterEvent(self, event) -> None:
        md = event.mimeData()
        if md is not None and md.hasFormat(FOLDER_REORDER_MIME):
            event.acceptProposedAction()
            self._update_folder_insert_preview_line_from_cursor()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        md = event.mimeData()
        if md is not None and md.hasFormat(FOLDER_REORDER_MIME):
            vp = self.viewport()
            vp_pos = self._viewport_pos_from_drag_move_event(event)
            vh = vp.height()
            cap = _FOLDER_LIST_DRAG_AUTOSCROLL_MARGIN
            # 触发带：取「上限 cap」与「约视口 1/3」的较小值，且不超过半高，便于在更靠上/下处仍能滚
            band = min(cap, max(24, vh // 3))
            band = min(band, max(1, (vh - 1) // 2))
            mn = _FOLDER_LIST_DRAG_AUTOSCROLL_MIN_PX
            mx = _FOLDER_LIST_DRAG_AUTOSCROLL_MAX_PX
            mm = max(band, 1)
            if vh > 24 and vp_pos.y() < band:
                dist = max(0, min(band - vp_pos.y(), mm))
                # 深入距离 dist∈[0,mm]：步长从 mn 按 (dist/mm)² 过渡到 mx
                step = mn + (mx - mn) * dist * dist // (mm * mm)
                step = max(mn, min(mx, step))
                self._folder_drag_autoscroll_delta = -step
                if not self._folder_drag_autoscroll_timer.isActive():
                    self._folder_drag_autoscroll_timer.start()
            elif vh > 24 and vp_pos.y() > vh - band:
                dist = max(0, min(vp_pos.y() - (vh - band), mm))
                step = mn + (mx - mn) * dist * dist // (mm * mm)
                step = max(mn, min(mx, step))
                self._folder_drag_autoscroll_delta = step
                if not self._folder_drag_autoscroll_timer.isActive():
                    self._folder_drag_autoscroll_timer.start()
            else:
                self._stop_folder_drag_autoscroll()
            self._update_folder_insert_preview_line_from_cursor()
            event.acceptProposedAction()
            return
        self._stop_folder_drag_autoscroll()
        self._hide_folder_insert_preview_line()
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._stop_folder_drag_autoscroll()
        self._hide_folder_insert_preview_line()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._stop_folder_drag_autoscroll()
        self._hide_folder_insert_preview_line()
        md = event.mimeData()
        if md is None or not md.hasFormat(FOLDER_REORDER_MIME):
            super().dropEvent(event)
            return
        try:
            raw = md.data(FOLDER_REORDER_MIME)
            fid = bytes(raw).decode("utf-8").strip()
        except (UnicodeDecodeError, TypeError, ValueError):
            event.ignore()
            return
        if not fid:
            event.ignore()
            return

        src = -1
        for r in range(self.count()):
            it = self.item(r)
            if it is not None and _folder_list_item_role_key(it) == fid:
                src = r
                break
        if src < 0:
            event.ignore()
            return

        sk = _folder_list_item_role_key(self.item(src))
        if sk in (
            None,
            FOLDER_KEY_UNCAT,
            _LEGACY_FOLDER_KEY_ALL,
            FOLDER_SIDEBAR_PENDING_NEW_KEY,
        ):
            event.ignore()
            return

        restore_scroll = int(self.verticalScrollBar().value())
        vp_pos = self._viewport_pos_for_folder_drop(event)
        insert_pos = self._insert_pos_from_drop_viewport_pos(vp_pos)
        insert_pos = max(1, min(insert_pos, self.count()))

        # takeItem / 整表重插时 current 会多次变化；若不阻塞，currentItemChanged 连发导致右侧列表对每个文件夹各重建一遍
        # 注意：blockSignals(True) 期间对本对象 emit 任意 Signal（含 folder_order_committed）都不会投递到槽函数，
        # 会导致 reorder_recipe_folders 从未落盘；侧栏刷新（如移动配方后）又从磁盘按旧顺序重建，表现为排序失效。
        commit_folder_order = False
        self.blockSignals(True)
        try:
            self._suppress_scroll_to_for_folder_reorder = True
            was_updates = self.updatesEnabled()
            self.setUpdatesEnabled(False)
            ok_normalize = False
            try:
                taken = self.takeItem(src)
                if taken is None:
                    event.ignore()
                    return

                if src < insert_pos:
                    insert_pos -= 1
                insert_pos = max(0, min(insert_pos, self.count()))

                self.insertItem(insert_pos, taken)

                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()

                ok_normalize = self._normalize_folder_rows_after_internal_move(
                    restore_scroll_value=restore_scroll
                )
            finally:
                self.setUpdatesEnabled(was_updates)
                self._suppress_scroll_to_for_folder_reorder = False

            if not ok_normalize:
                return

            for i in range(self.count()):
                it = self.item(i)
                if it is not None and _folder_list_item_role_key(it) == fid:
                    self.setCurrentRow(i)
                    break

            # normalize 里已在 setUpdatesEnabled(False) 下 setValue；重新开启绘制后滚动常被重置，须在之后钳位；下一事件再设一次以应对 maximum 晚更新。
            vs = restore_scroll

            def _reapply_folder_list_scroll() -> None:
                sbr = self.verticalScrollBar()
                sbr.setValue(min(max(0, vs), sbr.maximum()))

            _reapply_folder_list_scroll()
            QTimer.singleShot(0, _reapply_folder_list_scroll)
            commit_folder_order = True
        finally:
            self.blockSignals(False)
        if commit_folder_order:
            self.folder_order_committed.emit()


class _FolderSidebarInlineEdit(QLineEdit):
    """与炼金「保存配方到」弹窗一致：失焦提交、Esc 取消、回车提交且 accept 避免误触主按钮。"""

    escape_pressed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        page: "RecipeManagePage",
        role: str,
    ) -> None:
        super().__init__(parent)
        self._page = page
        self._role = role

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        QTimer.singleShot(0, lambda: self._page._on_folder_sidebar_inline_blur(self, self._role))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            super().keyPressEvent(event)
            event.accept()
            return
        super().keyPressEvent(event)


# 与 recipe_result_group 折叠标题箭头一致
_RECIPE_DETAIL_DISCLOSURE_PX = 14


class _RecipeDetailPromptFrame(QFrame):
    """折叠态摘要条：点击后再创建 RecipeResultGroup（懒加载详情）。"""

    open_requested = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit()
            return
        super().mousePressEvent(event)


def _build_recipe_summary_header_row(
    parent: QWidget,
    recipe: dict[str, Any],
    *,
    interactive: bool,
) -> QFrame:
    """与 RecipeResultGroup 折叠标题一致的箭头 + 绿色摘要行；interactive 为 False 时仅展示（批量管理）。"""
    frame: QFrame
    if interactive:
        frame = _RecipeDetailPromptFrame(parent)
    else:
        frame = QFrame(parent)
    frame.setObjectName("alchemyGroupHeader")
    frame.setAttribute(Qt.WA_StyledBackground)
    frame.setCursor(Qt.PointingHandCursor if interactive else Qt.ArrowCursor)
    frame.setFixedHeight(44)
    hl = QHBoxLayout(frame)
    hl.setContentsMargins(16, 0, 16, 0)
    hl.setSpacing(8)
    arrow = QLabel(frame)
    arrow.setObjectName("alchemyGroupArrow")
    px = _RECIPE_DETAIL_DISCLOSURE_PX
    arrow.setFixedSize(px, px)
    rate_color = "#10b981" if recipe.get("rate", 0) >= 0 else "#ef4444"
    icon = expand_section_triangle_icon(
        expanded=False,
        size_px=px,
        fill_color=rate_color,
    )
    arrow.setPixmap(icon.pixmap(px, px))
    hl.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)
    tl = QLabel(format_recipe_summary_line(recipe))
    tl.setObjectName("alchemyGroupTitle")
    tl.setStyleSheet(f"color: {rate_color};")
    tl.setMinimumWidth(0)
    tl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    hl.addWidget(tl, 1, Qt.AlignmentFlag.AlignVCenter)
    return frame


class _RecipeTitleLabel(QLabel):
    """Title label that emits ``double_clicked`` for inline rename."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _SavedRecipeRow(QWidget):
    def _recipe_manage_page(self) -> RecipeManagePage | None:
        if self._recipe_page is not None:
            return self._recipe_page
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, RecipeManagePage):
                return p
            p = p.parentWidget()
        return None

    def __init__(
        self,
        path: Path,
        payload: dict,
        batch_mode: bool,
        parent=None,
        *,
        recipe_page: RecipeManagePage | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("recipeManageRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._path = path
        self._payload = payload
        self._recipe_page: RecipeManagePage | None = recipe_page
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        self._check = QCheckBox()
        self._check.setObjectName("alchemyGroupSelectAllCheck")
        self._check.setVisible(batch_mode)
        top.addWidget(self._check, 0, Qt.AlignTop)
        meta = QVBoxLayout()
        meta.setSpacing(4)
        self._can_sim_import = self._simulation_import_slot_count_label() is not None
        self._can_collect_import = bool(self._inner_recipe_dict().get("substrates_display"))
        self._renaming = False
        self._title_edit: QLineEdit | None = None
        title_text = _display_row_title(payload)
        self._title_label = _RecipeTitleLabel(title_text)
        self._title_label.setObjectName("recipeSavedTitle")
        self._title_label.setWordWrap(True)
        self._title_label.setMinimumWidth(0)
        self._title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._title_label.setCursor(Qt.PointingHandCursor)
        self._title_label.setToolTip("双击重命名配方")
        self._title_label.double_clicked.connect(self._begin_title_rename)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self._title_label, 1)
        self._title_row = title_row
        self._rename_btn = QPushButton("重命名", self)
        self._rename_btn.setObjectName("recipeSimImportBtn")
        self._rename_btn.setCursor(Qt.PointingHandCursor)
        self._rename_btn.setToolTip("重命名配方")
        self._rename_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rename_btn.setAutoDefault(False)
        self._rename_btn.setDefault(False)
        self._rename_btn.setVisible(not batch_mode)
        self._rename_btn.clicked.connect(self._begin_title_rename)
        title_row.addWidget(self._rename_btn, 0, Qt.AlignTop)
        self._sim_import_btn = QPushButton("导入模拟", self)
        self._sim_import_btn.setObjectName("recipeSimImportBtn")
        self._sim_import_btn.setCursor(Qt.PointingHandCursor)
        self._sim_import_btn.setToolTip("导入模拟")
        self._sim_import_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sim_import_btn.setAutoDefault(False)
        self._sim_import_btn.setDefault(False)
        self._sim_import_btn.setVisible(self._can_sim_import and not batch_mode)
        self._sim_import_btn.clicked.connect(self._on_sim_import_clicked)
        title_row.addWidget(self._sim_import_btn, 0, Qt.AlignTop)
        self._collect_import_btn = QPushButton("导入采集", self)
        self._collect_import_btn.setObjectName("recipeSimImportBtn")
        self._collect_import_btn.setCursor(Qt.PointingHandCursor)
        self._collect_import_btn.setToolTip("把配方材料及单一采购区间导入材料采集")
        self._collect_import_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._collect_import_btn.setVisible(self._can_collect_import and not batch_mode)
        self._collect_import_btn.clicked.connect(self._on_collect_import_clicked)
        title_row.addWidget(self._collect_import_btn, 0, Qt.AlignTop)
        meta.addLayout(title_row)
        saved = _format_saved_at_local(str(payload.get("saved_at") or ""))
        mode = payload.get("mode") or ""
        recipe_for_mode = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
        if mode == "scan":
            mode_cn = "扫描模式"
        elif mode == "target":
            mode_cn = "目标模式"
        elif mode == "special_wear":
            mode_cn = "特殊磨损"
        elif mode == "simulation":
            mode_cn = "模拟模式"
        else:
            mode_cn = str(mode)
        sl = QLabel(f"保存时间：{saved}　·　{mode_cn}")
        sl.setObjectName("alchemyStep1Hint")
        sl.setWordWrap(True)
        meta.addWidget(sl)
        top.addLayout(meta, 1)
        outer.addLayout(top)
        self._batch_mode = batch_mode
        self._group: RecipeResultGroup | None = None
        self._detail_prompt: _RecipeDetailPromptFrame | None = None
        self._batch_summary_strip: QFrame | None = None
        self._detail_slot = QWidget(self)
        ds_l = QVBoxLayout(self._detail_slot)
        ds_l.setContentsMargins(0, 0, 0, 0)
        ds_l.setSpacing(0)
        outer.addWidget(self._detail_slot)
        if batch_mode:
            self._ensure_batch_summary_strip()
        else:
            self._ensure_detail_prompt()

    def _clear_detail_prompt(self) -> None:
        if self._detail_prompt is None:
            return
        lay = self._detail_slot.layout()
        lay.removeWidget(self._detail_prompt)
        self._detail_prompt.deleteLater()
        self._detail_prompt = None

    def _clear_batch_summary_strip(self) -> None:
        if self._batch_summary_strip is None:
            return
        lay = self._detail_slot.layout()
        lay.removeWidget(self._batch_summary_strip)
        self._batch_summary_strip.deleteLater()
        self._batch_summary_strip = None

    def _ensure_batch_summary_strip(self) -> None:
        """批量管理：仅展示摘要，不可点击展开（无 RecipeResultGroup 时）。"""
        if self._group is not None or self._batch_summary_strip is not None:
            return
        recipe = self._inner_recipe_dict()
        frame = _build_recipe_summary_header_row(
            self._detail_slot, recipe, interactive=False
        )
        self._detail_slot.layout().addWidget(frame)
        self._batch_summary_strip = frame

    def _ensure_detail_prompt(self) -> None:
        if self._batch_mode or self._group is not None or self._detail_prompt is not None:
            return
        recipe = self._inner_recipe_dict()
        frame = _build_recipe_summary_header_row(
            self._detail_slot, recipe, interactive=True
        )
        assert isinstance(frame, _RecipeDetailPromptFrame)
        frame.open_requested.connect(self._on_detail_prompt_clicked)
        self._detail_slot.layout().addWidget(frame)
        self._detail_prompt = frame

    def _on_detail_prompt_clicked(self) -> None:
        if self._batch_mode or self._group is not None:
            return
        self._instantiate_recipe_group()

    def _instantiate_recipe_group(self) -> None:
        if self._group is not None:
            return
        rank = int(self._payload.get("rank", 1))
        recipe = self._payload.get("recipe") or {}
        lay = self._detail_slot.layout()
        self._clear_detail_prompt()
        self._clear_batch_summary_strip()
        self._group = RecipeResultGroup(
            rank,
            recipe,
            self._detail_slot,
            enable_save=False,
            recipe_storage_path=self._path,
            expand_enabled=not self._batch_mode,
            manage_substrate_disk_actions=True,
        )
        lay.addWidget(self._group)
        if not self._batch_mode:
            self._group.toggle()

    def _on_sim_import_clicked(self) -> None:
        page = self._recipe_manage_page()
        if page is not None:
            page.import_to_simulation_requested.emit(self._inner_recipe_dict())

    def _on_collect_import_clicked(self) -> None:
        page = self._recipe_manage_page()
        if page is not None:
            page.import_to_collection_requested.emit(
                {
                    "recipe": self._inner_recipe_dict(),
                    "title": _display_row_title(self._payload),
                }
            )

    def refresh_sim_import_button_icon(self, color: str) -> None:
        # Kept for the page theme-refresh call site; the action is now a
        # clearly labeled text button instead of an icon-only control.
        return

    def _inner_recipe_dict(self) -> dict[str, Any]:
        r = self._payload.get("recipe")
        return r if isinstance(r, dict) else {}

    def _simulation_import_slot_count_label(self) -> str | None:
        """若可导入模拟，返回「五合一」或「十合一」；否则 None。"""
        recipe = self._inner_recipe_dict()
        subs = recipe.get("substrates_display")
        if not isinstance(subs, list) or not subs:
            return None
        k_meta = recipe.get("simulation_slot_count")
        if k_meta is not None:
            try:
                k_meta = int(k_meta)
            except (TypeError, ValueError):
                k_meta = None
        n = len(subs)
        if k_meta == 5:
            return "五合一" if n == 5 else None
        if k_meta == 10:
            return "十合一" if n == 10 else None
        if n == 5:
            return "五合一"
        if n == 10:
            return "十合一"
        return None

    def path(self) -> Path:
        return self._path

    def is_checked(self) -> bool:
        return self._check.isChecked()

    def set_batch_visible(self, visible: bool) -> None:
        self._batch_mode = visible
        self._check.setVisible(visible)
        if not visible:
            self._check.setChecked(False)
        if self._group is not None:
            self._group.set_expand_enabled(not visible)
        if visible:
            self._clear_detail_prompt()
            if self._group is None:
                self._ensure_batch_summary_strip()
            else:
                self._clear_batch_summary_strip()
        else:
            self._clear_batch_summary_strip()
            if self._group is None:
                self._ensure_detail_prompt()
        if self._can_sim_import:
            self._sim_import_btn.setVisible(not visible)
        if self._can_collect_import:
            self._collect_import_btn.setVisible(not visible)
        self._rename_btn.setVisible(not visible)
        if visible and self._renaming:
            self._cancel_title_rename()
        self._update_batch_click_filters()

    def _begin_title_rename(self) -> None:
        if self._batch_mode or self._renaming:
            return
        self._renaming = True
        self._rename_btn.hide()
        self._sim_import_btn.hide()
        self._collect_import_btn.hide()
        self._title_row.removeWidget(self._title_label)
        self._title_label.hide()
        edit = QLineEdit(self)
        edit.setObjectName("recipeFolderListInlineEdit")
        edit.setText(_display_row_title(self._payload))
        edit.setPlaceholderText("配方名称")
        edit.returnPressed.connect(self._commit_title_rename)
        edit.installEventFilter(self)
        self._title_row.insertWidget(0, edit, 1)
        self._title_edit = edit
        edit.setFocus(Qt.FocusReason.OtherFocusReason)
        edit.selectAll()

    def _cancel_title_rename(self) -> None:
        if not self._renaming:
            return
        edit = self._title_edit
        if edit is not None:
            self._title_row.removeWidget(edit)
            edit.deleteLater()
        self._title_edit = None
        self._title_row.insertWidget(0, self._title_label, 1)
        self._title_label.show()
        self._renaming = False
        self._rename_btn.show()
        if self._can_sim_import and not self._batch_mode:
            self._sim_import_btn.show()
        if self._can_collect_import and not self._batch_mode:
            self._collect_import_btn.show()

    def _commit_title_rename(self) -> None:
        if not self._renaming or self._title_edit is None:
            return
        raw = self._title_edit.text().strip()
        try:
            stored = rename_saved_recipe_title(self._path, raw)
        except Exception as exc:  # noqa: BLE001
            show_toast(self, f"重命名失败：{exc}", style="warning")
            self._cancel_title_rename()
            return
        self._payload["title"] = stored
        self._title_label.setText(stored)
        self._cancel_title_rename()

    def _commit_title_rename_if_still_editing(self) -> None:
        if self._renaming and self._title_edit is not None:
            self._commit_title_rename()

    def _update_batch_click_filters(self) -> None:
        for w in self.findChildren(QWidget):
            w.removeEventFilter(self)
        self.removeEventFilter(self)
        if not self._check.isVisible():
            return
        self.installEventFilter(self)
        # 含复选框及其内部子控件：此前跳过复选框子树导致按下复选框时 eventFilter 不触发，无法开始框选拖动
        for w in self.findChildren(QWidget):
            w.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._renaming and watched is self._title_edit:
            et = event.type()
            if et == QEvent.Type.KeyPress and hasattr(event, "key"):
                if event.key() == Qt.Key.Key_Escape:
                    self._cancel_title_rename()
                    return True
            if et == QEvent.Type.FocusOut:
                QTimer.singleShot(0, self._commit_title_rename_if_still_editing)
            return super().eventFilter(watched, event)
        if not self._check.isVisible():
            return super().eventFilter(watched, event)
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                page = self._recipe_manage_page()
                if page is not None and page._batch_row_press_begin(
                    self, _qmouse_global_point(me)
                ):
                    return True
        return super().eventFilter(watched, event)


class RecipeManagePage(QWidget):
    """Saved recipe browsing plus simulation and material-collection handoff."""

    import_to_simulation_requested = Signal(object)
    import_to_collection_requested = Signal(object)
    import_json_to_alchemy_requested = Signal(object)
    navigation_route_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recipeManagePage")
        self._batch_mode = False
        self._rows: list[_SavedRecipeRow] = []
        self._folder_list_selected_key: str = FOLDER_KEY_UNCAT
        # 右侧配方列表上次 _rebuild_recipe_rows 所对应的文件夹（用于区分「侧栏已同步 key」与「列表尚未重建」）
        self._recipe_list_rendered_folder_key: str | None = None
        self._batch_primary_cached_w: int = 0
        self._batch_primary_cached_h: int = 0
        # 批量管理：按住左键滑动多选（在列表 viewport 上 grab + eventFilter，保证跨行拖动能收到 Move）
        self._batch_drag_active = False
        self._batch_drag_anchor_row: _SavedRecipeRow | None = None
        self._batch_drag_anchor_index = -1
        self._batch_drag_start_global = QPoint()
        self._batch_drag_moved = False
        self._batch_viewport_drag_hooked = False
        self._batch_drag_last_global = QPoint()
        self._batch_autoscroll_timer = QTimer(self)
        self._batch_autoscroll_timer.setInterval(16)
        self._batch_autoscroll_timer.timeout.connect(self._batch_autoscroll_tick)
        # 拖动笔刷：按下时各行的勾选快照；从未勾选行起拖为加选，从已勾选行起拖为减选（移出笔刷恢复快照）
        self._batch_drag_start_checked: list[bool] = []
        self._batch_drag_select_mode: bool = True
        self._folder_sidebar_app_filter_installed = False
        self._folder_pending_new_item: QListWidgetItem | None = None
        self._folder_pending_new_edit: _FolderSidebarInlineEdit | None = None
        self._folder_rename_item: QListWidgetItem | None = None
        self._folder_rename_edit: _FolderSidebarInlineEdit | None = None
        self._folder_rename_id: str | None = None
        # 批量管理因切换文件夹延迟恢复时，用于作废尚未执行的 QTimer.singleShot 回调
        self._batch_resume_token: int = 0
        self._recipe_import_thread: RecipeLoadThread | None = None
        self._view_mode = "recipes"
        self._purchase_batch_card_states: dict[str, dict[str, object]] = {}
        self._purchase_batch_scroll_value = 0

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(*CONTENT_PAGE_LAYOUT_MARGINS)
        main_layout.setSpacing(16)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        self._page_title_icon_label = QLabel(self)
        self._page_title_icon_label.setObjectName("contentPageTitleIcon")
        self._page_title_icon_label.setFixedSize(28, 28)
        self._page_title_icon_label.setAlignment(Qt.AlignCenter)
        title_row.addWidget(self._page_title_icon_label, 0, Qt.AlignVCenter)
        title = QLabel("配方管理")
        title.setObjectName("alchemyPageTitle")
        tf = QFont()
        tf.setPointSize(18)
        tf.setWeight(QFont.Weight.DemiBold)
        title.setFont(tf)
        title_row.addWidget(title)
        self._title_count_label = QLabel("共0条配方")
        self._title_count_label.setObjectName("recipePageTitleCount")
        title_row.addWidget(self._title_count_label, 0, Qt.AlignVCenter)
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        self._recipes_view_btn = QPushButton("已保存配方")
        self._purchase_batches_view_btn = QPushButton("采购管理")
        self._json_view_btn = QPushButton("已保存 JSON")
        for mode, button in (
            ("recipes", self._recipes_view_btn),
            ("purchase_batches", self._purchase_batches_view_btn),
            ("json", self._json_view_btn),
        ):
            button.setObjectName("platformModeButton")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, value=mode: self._switch_saved_view(value)
            )
            view_group.addButton(button)
            title_row.addWidget(button)
        self._recipes_view_btn.setChecked(True)
        title_row.addStretch(1)
        self.import_cs2th_btn = QPushButton("导入 CS2TH 链接")
        self.import_cs2th_btn.setObjectName("alchemySelectFileBtn")
        self.import_cs2th_btn.setCursor(Qt.PointingHandCursor)
        self.import_cs2th_btn.clicked.connect(self._on_import_cs2th_link)
        title_row.addWidget(self.import_cs2th_btn)
        self.batch_btn = QPushButton("批量管理")
        self.batch_btn.setObjectName("alchemySelectFileBtn")
        self.batch_btn.setCursor(Qt.PointingHandCursor)
        self.batch_btn.clicked.connect(self._on_toggle_batch)
        self._batch_primary_place = QWidget()
        self._batch_primary_place.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 使用 QWidget + QStackedLayout，勿用 QStackedWidget（继承 QFrame）；在 Windows 上
        # 重排/切换页时偶发画出带图标的浅灰条（类标题栏残影）。
        self._batch_primary_slot = QWidget()
        self._batch_primary_slot.setObjectName("recipeBatchPrimarySlot")
        self._batch_primary_slot.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self._batch_stack_layout = QStackedLayout(self._batch_primary_slot)
        self._batch_stack_layout.setContentsMargins(0, 0, 0, 0)
        self._batch_stack_layout.addWidget(self.batch_btn)
        self._batch_stack_layout.addWidget(self._batch_primary_place)
        title_row.addWidget(self._batch_primary_slot)
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setObjectName("alchemySelectFileBtn")
        self.select_all_btn.setCursor(Qt.PointingHandCursor)
        self.select_all_btn.setVisible(False)
        self.select_all_btn.clicked.connect(self._on_select_all_toggle)
        title_row.addWidget(self.select_all_btn)
        self.move_btn = QPushButton("移动到…")
        self.move_btn.setObjectName("alchemySelectFileBtn")
        self.move_btn.setCursor(Qt.PointingHandCursor)
        self.move_btn.setVisible(False)
        self.move_btn.setAutoDefault(False)
        self.move_btn.setDefault(False)
        # 勿用 setMenu：附着 QMenu 在布局/重绘时会在 Windows 上触发短暂空白弹出层闪烁
        self.move_btn.clicked.connect(self._on_move_to_clicked)
        title_row.addWidget(self.move_btn)
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.setObjectName("alchemyClearFileBtn")
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.setVisible(False)
        self.delete_btn.clicked.connect(self._on_delete_selected)
        title_row.addWidget(self.delete_btn)
        self.batch_done_btn = QPushButton("完成")
        self.batch_done_btn.setObjectName("alchemyNextBtn")
        self.batch_done_btn.setCursor(Qt.PointingHandCursor)
        self.batch_done_btn.setVisible(False)
        self.batch_done_btn.clicked.connect(self._on_batch_done)
        title_row.addWidget(self.batch_done_btn)
        main_layout.addLayout(title_row)

        # —— 左侧：卡片内文件夹列表 ——
        folder_card = QFrame()
        self._folder_card = folder_card
        folder_card.setObjectName("recipeFolderCard")
        folder_card.setFixedWidth(_RECIPE_LEFT_PANEL_WIDTH)
        folder_card.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        card_lay = QVBoxLayout(folder_card)
        card_lay.setContentsMargins(12, 12, 12, 12)
        card_lay.setSpacing(10)
        folder_heading = QLabel("文件夹（拖动排序，双击/右键改名）")
        folder_heading.setObjectName("recipeFolderHeading")
        card_lay.addWidget(folder_heading)

        self._folder_list = _FolderList()
        self._folder_list.folder_order_committed.connect(
            self._on_folder_sidebar_order_committed
        )
        self._folder_list.currentItemChanged.connect(self._on_folder_item_changed)
        self._folder_list.itemDoubleClicked.connect(self._on_folder_item_double_clicked)
        self._folder_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._folder_list.customContextMenuRequested.connect(self._on_folder_list_context_menu)
        card_lay.addWidget(self._folder_list, 1)

        self.new_folder_btn = QPushButton("新建文件夹")
        self.new_folder_btn.setObjectName("alchemySelectFileBtn")
        self.new_folder_btn.setCursor(Qt.PointingHandCursor)
        self.new_folder_btn.setAutoDefault(False)
        self.new_folder_btn.setDefault(False)
        self.new_folder_btn.clicked.connect(self._on_new_folder)
        card_lay.addWidget(self.new_folder_btn)

        # —— 右侧：工具条 + 列表 ——
        self.empty_label = QLabel("暂无已保存配方。在炼金计算结果中点击「保存配方」即可保存到此。")
        self.empty_label.setObjectName("alchemyStep1Hint")
        self.empty_label.setWordWrap(True)

        toolbar = QWidget()
        toolbar.setObjectName("recipeManageToolbar")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(12)
        self._location_label = QLabel(self)
        self._location_label.setObjectName("recipeLocationLabel")
        self._location_label.setWordWrap(False)
        self._location_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(self._location_label, 0, Qt.AlignmentFlag.AlignVCenter)
        tb.addStretch(1)
        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("recipeSearchEdit")
        self._search_edit.setPlaceholderText("在当前文件夹搜索…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMinimumWidth(180)
        self._search_edit.setMaximumWidth(320)
        self._search_edit.textChanged.connect(self._on_search_changed)
        tb.addWidget(self._search_edit, 0, Qt.AlignmentFlag.AlignVCenter)

        self.list_container = QWidget()
        self.list_container.setObjectName("alchemyGroupsContainer")
        self.list_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.setSizeConstraint(QLayout.SetMinimumSize)

        scroll = QScrollArea()
        scroll.setObjectName("alchemyScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setAttribute(Qt.WA_StyledBackground)
        scroll.setWidget(self.list_container)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._recipe_list_scroll = scroll

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(12)
        right_col.addWidget(toolbar)
        right_col.addWidget(self.empty_label)
        right_col.addWidget(scroll, 1)
        right_wrap = QWidget()
        right_wrap.setLayout(right_col)
        right_wrap.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        body = QWidget()
        body.setObjectName("recipeManageBody")
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(folder_card, 0)
        body_lay.addSpacing(_RECIPE_LEFT_RIGHT_GAP)
        body_lay.addWidget(right_wrap, 1)

        purchase_body = QWidget()
        purchase_body.setObjectName("recipeManageBody")
        purchase_lay = QVBoxLayout(purchase_body)
        purchase_lay.setContentsMargins(0, 0, 0, 0)
        purchase_lay.setSpacing(12)
        purchase_toolbar = QWidget()
        purchase_toolbar.setObjectName("recipeManageToolbar")
        purchase_tb = QHBoxLayout(purchase_toolbar)
        purchase_tb.setContentsMargins(12, 8, 12, 8)
        purchase_location = QLabel("按 Steam 收货账号管理整批配方和材料入库")
        purchase_location.setObjectName("recipeLocationLabel")
        purchase_tb.addWidget(purchase_location)
        purchase_tb.addStretch(1)
        self._new_purchase_batch_btn = QPushButton("新建采购批次")
        self._new_purchase_batch_btn.setObjectName("alchemySelectFileBtn")
        self._new_purchase_batch_btn.clicked.connect(self._create_purchase_batch)
        purchase_tb.addWidget(self._new_purchase_batch_btn)
        purchase_lay.addWidget(purchase_toolbar)
        self._purchase_batch_empty_label = QLabel(
            "暂无采购批次。先刷新收货账号库存，再新建批次；材料采集得到的方案可直接加入批次。"
        )
        self._purchase_batch_empty_label.setObjectName("alchemyStep1Hint")
        self._purchase_batch_empty_label.setWordWrap(True)
        purchase_lay.addWidget(self._purchase_batch_empty_label)
        self._purchase_batch_container = QWidget()
        self._purchase_batch_container.setObjectName("alchemyGroupsContainer")
        self._purchase_batch_layout = QVBoxLayout(self._purchase_batch_container)
        self._purchase_batch_layout.setContentsMargins(0, 0, 0, 0)
        self._purchase_batch_layout.setSpacing(8)
        self._purchase_scroll = QScrollArea()
        self._purchase_scroll.setObjectName("alchemyScrollArea")
        self._purchase_scroll.setWidgetResizable(True)
        self._purchase_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._purchase_scroll.setFrameShape(QFrame.NoFrame)
        self._purchase_scroll.setWidget(self._purchase_batch_container)
        purchase_lay.addWidget(self._purchase_scroll, 1)

        json_body = QWidget()
        json_body.setObjectName("recipeManageBody")
        json_lay = QVBoxLayout(json_body)
        json_lay.setContentsMargins(0, 0, 0, 0)
        json_lay.setSpacing(12)
        json_toolbar = QWidget()
        json_toolbar.setObjectName("recipeManageToolbar")
        json_tb = QHBoxLayout(json_toolbar)
        json_tb.setContentsMargins(12, 8, 12, 8)
        self._json_location_label = QLabel("采集 JSON 文件")
        self._json_location_label.setObjectName("recipeLocationLabel")
        json_tb.addWidget(self._json_location_label)
        json_tb.addStretch(1)
        open_json_dir_btn = QPushButton("打开文件夹")
        open_json_dir_btn.clicked.connect(self._open_collected_json_dir)
        json_tb.addWidget(open_json_dir_btn)
        json_lay.addWidget(json_toolbar)

        self._json_empty_label = QLabel(
            "暂无已保存 JSON。材料采集完成后点击「保存为 JSON」即可保存到此。"
        )
        self._json_empty_label.setObjectName("alchemyStep1Hint")
        self._json_empty_label.setWordWrap(True)
        json_lay.addWidget(self._json_empty_label)
        self._json_list_container = QWidget()
        self._json_list_container.setObjectName("alchemyGroupsContainer")
        self._json_list_layout = QVBoxLayout(self._json_list_container)
        self._json_list_layout.setContentsMargins(0, 0, 0, 0)
        self._json_list_layout.setSpacing(8)
        json_scroll = QScrollArea()
        json_scroll.setObjectName("alchemyScrollArea")
        json_scroll.setWidgetResizable(True)
        json_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        json_scroll.setFrameShape(QFrame.NoFrame)
        json_scroll.setWidget(self._json_list_container)
        json_lay.addWidget(json_scroll, 1)

        self._body_stack = QStackedWidget()
        self._body_stack.addWidget(body)
        self._body_stack.addWidget(purchase_body)
        self._body_stack.addWidget(json_body)
        main_layout.addWidget(self._body_stack, 1)

        self._recipe_theme_icon_refresh_pending = False
        self._apply_page_title_icon()
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()
        self._rebuild_purchase_batch_rows()
        self._rebuild_collected_json_rows()

    def _on_import_cs2th_link(self) -> None:
        if self._recipe_import_thread is not None and self._recipe_import_thread.isRunning():
            return
        reference, accepted = get_wide_text_input(
            self,
            title="导入 CS2TH 配方",
            label="粘贴配方链接：",
        )
        if not accepted or not reference.strip():
            return
        session = AuthClient().load_local_session()
        token = session.access_token if session is not None else ""
        self.import_cs2th_btn.setEnabled(False)
        self.import_cs2th_btn.setText("导入中…")
        thread = RecipeLoadThread(reference.strip(), token, self)
        thread.completed.connect(self._on_cs2th_recipe_loaded)
        thread.finished.connect(thread.deleteLater)
        self._recipe_import_thread = thread
        thread.start()

    def _on_cs2th_recipe_loaded(self, payload: object, error: str) -> None:
        self.import_cs2th_btn.setEnabled(True)
        self.import_cs2th_btn.setText("导入 CS2TH 链接")
        self._recipe_import_thread = None
        if error or not isinstance(payload, dict):
            show_toast(self, error or "配方导入失败", style="warning")
            return
        recipe = cs2th_detail_to_saved_recipe(payload)
        if not recipe.get("substrates_display"):
            show_toast(self, "CS2TH 配方没有可导入的材料", style="warning")
            return
        folder_id = (
            None
            if self._folder_list_selected_key == FOLDER_KEY_UNCAT
            else self._folder_list_selected_key
        )
        try:
            save_recipe_file(
                recipe,
                rank=1,
                mode="cs2th",
                norm_min=0.0,
                norm_max=1.0,
                title=str(payload.get("collection_name") or "CS2TH 导入配方"),
                folder_id=folder_id,
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"保存导入配方失败：{exc}", style="warning")
            return
        self.refresh_from_disk()
        show_toast(self, "CS2TH 配方已导入当前文件夹", style="success")

    def _on_move_to_clicked(self) -> None:
        if not any(r.is_checked() for r in self._rows):
            show_toast(self, "请先勾选要移动的配方", style="warning")
            return
        targets = build_move_targets(self._folder_list_selected_key)
        if not targets:
            show_toast(self, "当前没有可移动的目标", style="warning")
            return
        dlg = MoveRecipeFolderDialog(self.window(), targets=targets)
        if dlg.exec() != QDialog.Accepted:
            return
        self._move_selected_to_folder(dlg.chosen_folder_id())

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        # 勿在此做列表全量重建：任务栏恢复/窗口从最小化还原时子控件也会收到 showEvent，
        # 会重复 _rebuild_recipe_rows 并触发闪动。刷新改由主窗口 content_stack.currentChanged 在切换到本页时调用。
        self._apply_page_title_icon()
        # main.py 在 MainWindow() 之后才 apply_theme，__init__ 里测的「批量管理」尺寸仍是未挂 QSS 前的
        # sizeHint；首次显示本页时用当前样式表重算槽宽/高，避免按钮被裁切或样式不一致。
        self._sync_batch_primary_stack_geometry()

    def hideEvent(self, event: QHideEvent) -> None:
        self._batch_resume_token += 1
        app = QApplication.instance()
        if app is not None and self._folder_sidebar_app_filter_installed:
            app.removeEventFilter(self)
            self._folder_sidebar_app_filter_installed = False
        super().hideEvent(event)

    def refresh_from_disk(self) -> None:
        """侧栏切换到配方管理页时从磁盘刷新侧栏与列表。"""
        self._sync_batch_primary_stack_geometry()
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()
        self._rebuild_purchase_batch_rows()
        self._rebuild_collected_json_rows()

    def navigation_subroute(self) -> str:
        return self._view_mode

    def navigation_route_label(self) -> str:
        labels = {
            "recipes": "配方管理 · 已保存配方",
            "purchase_batches": "配方管理 · 采购管理",
            "json": "配方管理 · 已保存 JSON",
        }
        return labels.get(self._view_mode, "配方管理")

    def restore_navigation_subroute(self, mode: str) -> None:
        self._switch_saved_view(mode, emit_navigation=False)

    def _switch_saved_view(self, mode: str, *, emit_navigation: bool = True) -> None:
        if mode not in {"recipes", "purchase_batches", "json"}:
            mode = "recipes"
        previous_mode = self._view_mode
        if mode != "recipes" and self._batch_mode:
            self._on_batch_done()
        self._view_mode = mode
        showing_recipes = mode == "recipes"
        showing_batches = mode == "purchase_batches"
        self._body_stack.setCurrentIndex(0 if showing_recipes else 1 if showing_batches else 2)
        self._recipes_view_btn.setChecked(showing_recipes)
        self._purchase_batches_view_btn.setChecked(showing_batches)
        self._json_view_btn.setChecked(mode == "json")
        self._batch_primary_slot.setVisible(showing_recipes)
        self.import_cs2th_btn.setVisible(showing_recipes and not self._batch_mode)
        for button in (
            self.select_all_btn,
            self.move_btn,
            self.delete_btn,
            self.batch_done_btn,
        ):
            if not showing_recipes:
                button.hide()
        if showing_recipes:
            self._set_batch_ui()
            self._rebuild_recipe_rows()
        elif showing_batches:
            self._rebuild_purchase_batch_rows()
        else:
            self._rebuild_collected_json_rows()
        if emit_navigation and mode != previous_mode:
            self.navigation_route_changed.emit(mode)

    def _create_purchase_batch(self) -> None:
        accounts = [
            entry for entry in list_profile_entries() if str(entry.get("id") or "")
        ]
        if not accounts:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return
        if not ask_confirmation(
            self,
            "创建采购批次",
            "创建时会把该账号当前本地库存记为基线。请确认已经在 Steam 库存页刷新过该账号库存。",
        ):
            return
        default_name = datetime.now().strftime("采购批次 %Y-%m-%d %H:%M")
        name, accepted = get_wide_text_input(
            self,
            title="新建采购批次",
            label="批次名称：",
            value=default_name,
        )
        if not accepted or not name.strip():
            return
        labels = [combo_display_name_for_profile(entry) for entry in accounts]
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
            labels,
            current,
            False,
        )
        if not accepted:
            return
        account_index = labels.index(account_label)
        entry = accounts[account_index]
        profile_id = str(entry.get("id") or "")
        cfg = load_steam_account_config_dict(profile_id)
        try:
            create_purchase_batch(
                name,
                profile_id=profile_id,
                steam_id=str(cfg.get("steam_id") or ""),
                account_name=account_label,
                inventory_items=load_profile_inventory_items(profile_id),
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"采购批次创建失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        show_toast(self, "采购批次已创建，可从材料采集加入配方", style="success")

    def _rebuild_purchase_batch_rows(self) -> None:
        self._capture_purchase_batch_ui_state()
        while self._purchase_batch_layout.count():
            item = self._purchase_batch_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        entries = list_purchase_batches()
        valid_state_keys = {str(path) for path, _payload in entries}
        self._purchase_batch_card_states = {
            key: state
            for key, state in self._purchase_batch_card_states.items()
            if key in valid_state_keys
        }
        self._purchase_batch_empty_label.setVisible(not entries)
        if self._view_mode == "purchase_batches":
            recipe_total = sum(
                len(payload.get("recipes") or []) for _path, payload in entries
            )
            material_total = sum(
                int(purchase_batch_summary(payload).get("total") or 0)
                for _path, payload in entries
            )
            self._title_count_label.setText(
                f"共{len(entries)}个批次 · {recipe_total}个配方 · {material_total}件材料"
            )
        for path, payload in entries:
            state_key = str(path)
            card = PurchaseBatchCard(
                path,
                payload,
                self._purchase_batch_container,
                expanded=False,
            )
            card.restore_ui_state(self._purchase_batch_card_states.get(state_key))
            card.changed.connect(self._update_purchase_batch_title_count)
            card.deleted.connect(self._rebuild_purchase_batch_rows)
            card.change_account_requested.connect(
                self._change_purchase_batch_account
            )
            self._purchase_batch_layout.addWidget(card)
        self._purchase_batch_layout.addStretch(1)
        scroll_value = self._purchase_batch_scroll_value
        QTimer.singleShot(
            0,
            lambda value=scroll_value: self._purchase_scroll.verticalScrollBar().setValue(
                value
            ),
        )

    def _capture_purchase_batch_ui_state(self) -> None:
        scroll = getattr(self, "_purchase_scroll", None)
        if scroll is not None:
            self._purchase_batch_scroll_value = scroll.verticalScrollBar().value()
        layout = getattr(self, "_purchase_batch_layout", None)
        if layout is None:
            return
        for index in range(layout.count()):
            card = layout.itemAt(index).widget()
            if isinstance(card, PurchaseBatchCard):
                self._purchase_batch_card_states[str(card._path)] = card.ui_state()

    def _change_purchase_batch_account(self, path: Path) -> None:
        entries = [
            entry for entry in list_profile_entries() if str(entry.get("id") or "")
        ]
        if not entries:
            show_toast(self, "请先在 Steam 库存添加收货账号", style="warning")
            return
        try:
            current_batch = next(
                payload
                for batch_path, payload in list_purchase_batches()
                if batch_path == path
            )
        except StopIteration:
            show_toast(self, "采购批次已不存在", style="warning")
            return
        labels = [combo_display_name_for_profile(entry) for entry in entries]
        current_profile_id = str(current_batch.get("profile_id") or "")
        current_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if str(entry.get("id") or "") == current_profile_id
            ),
            0,
        )
        selected, accepted = QInputDialog.getItem(
            self,
            "修改收货账号",
            "Steam 收货账号：",
            labels,
            current_index,
            False,
        )
        if not accepted:
            return
        selected_entry = entries[labels.index(selected)]
        profile_id = str(selected_entry.get("id") or "")
        if profile_id == current_profile_id:
            show_toast(self, "收货账号没有变化", style="info")
            return
        if not ask_confirmation(
            self,
            "确认修改收货账号",
            "建议在开始采购前修改。修改后会以新账号当前库存重新建立入库基线；"
            "旧账号已经匹配的入库记录将撤销。确定继续吗？",
        ):
            return
        cfg = load_steam_account_config_dict(profile_id)
        try:
            reset_count = update_purchase_batch_account(
                path,
                profile_id=profile_id,
                steam_id=str(cfg.get("steam_id") or ""),
                account_name=selected,
                inventory_items=load_profile_inventory_items(profile_id),
            )
        except (OSError, ValueError) as exc:
            show_toast(self, f"收货账号修改失败：{exc}", style="warning")
            return
        self._rebuild_purchase_batch_rows()
        message = f"收货账号已改为 {selected}"
        if reset_count:
            message += f"，已撤销 {reset_count} 件旧账号入库匹配"
        show_toast(self, message, style="success")

    def _update_purchase_batch_title_count(self) -> None:
        if self._view_mode != "purchase_batches":
            return
        entries = list_purchase_batches()
        recipe_total = sum(len(payload.get("recipes") or []) for _path, payload in entries)
        material_total = sum(
            int(purchase_batch_summary(payload).get("total") or 0)
            for _path, payload in entries
        )
        self._title_count_label.setText(
            f"共{len(entries)}个批次 · {recipe_total}个配方 · {material_total}件材料"
        )

    def _open_collected_json_dir(self) -> None:
        COLLECTED_JSON_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(COLLECTED_JSON_DIR)))

    def _open_collected_json_file(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _rebuild_collected_json_rows(self) -> None:
        while self._json_list_layout.count():
            item = self._json_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        entries = list_collected_json()
        self._json_empty_label.setVisible(not entries)
        if self._view_mode == "json":
            self._title_count_label.setText(f"共{len(entries)}个 JSON 文件")
        for path, rows in entries:
            frame = QFrame()
            frame.setObjectName("recipeManageToolbar")
            row_layout = QHBoxLayout(frame)
            row_layout.setContentsMargins(14, 10, 14, 10)
            text_layout = QVBoxLayout()
            name_label = QLabel(path.stem)
            name_label.setObjectName("recipeSavedTitle")
            total = sum(float(row.get("price") or 0) for row in rows)
            try:
                saved_at = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y年%m月%d日 %H:%M:%S"
                )
            except OSError:
                saved_at = ""
            meta_label = QLabel(
                f"{len(rows)} 条数据 · 总价 ￥{total:,.2f}"
                + (f" · {saved_at}" if saved_at else "")
            )
            meta_label.setObjectName("muted")
            text_layout.addWidget(name_label)
            text_layout.addWidget(meta_label)
            row_layout.addLayout(text_layout, 1)
            open_button = QPushButton("打开文件")
            open_button.clicked.connect(
                lambda _checked=False, value=path: self._open_collected_json_file(value)
            )
            import_button = QPushButton("导入计算")
            import_button.setObjectName("primaryButton")
            import_button.clicked.connect(
                lambda _checked=False, value=rows: self.import_json_to_alchemy_requested.emit(
                    [dict(item) for item in value]
                )
            )
            row_layout.addWidget(open_button)
            row_layout.addWidget(import_button)
            self._json_list_layout.addWidget(frame)
        self._json_list_layout.addStretch(1)

    def changeEvent(self, event: QEvent):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if not self.isVisible():
                return
            self._sync_batch_primary_stack_geometry()
            if self._recipe_theme_icon_refresh_pending:
                return
            self._recipe_theme_icon_refresh_pending = True
            QTimer.singleShot(0, self._run_recipe_theme_icon_refresh)

    def _run_recipe_theme_icon_refresh(self) -> None:
        self._recipe_theme_icon_refresh_pending = False
        if not self.isVisible():
            return
        self._apply_page_title_icon()
        c = self.palette().color(QPalette.ColorRole.WindowText).name()
        for row in self._rows:
            row.refresh_sim_import_button_icon(c)

    def _capture_batch_primary_size_cache(self) -> None:
        """在「批量管理」按钮可见时测量；隐藏后 sizeHint 常为 0，不可再读。"""
        self.batch_btn.ensurePolished()
        self.batch_btn.updateGeometry()
        sh = self.batch_btn.sizeHint()
        mh = self.batch_btn.minimumSizeHint()
        w = max(sh.width(), mh.width(), 8)
        h = max(sh.height(), mh.height(), 8)
        self._batch_primary_cached_w = w
        self._batch_primary_cached_h = h

    def _sync_batch_primary_stack_geometry(self) -> None:
        """与「批量管理」同尺寸占位；占位页显示时勿用隐藏按钮的 sizeHint（否则会缩成 1px 并闪一下）。"""
        idx = self._batch_stack_layout.currentIndex()
        if idx == 0:
            self._capture_batch_primary_size_cache()
            w = self._batch_primary_cached_w
            h = self._batch_primary_cached_h
        else:
            w = max(self._batch_primary_cached_w, 72)
            h = max(self._batch_primary_cached_h, 28)
        self._batch_primary_place.setFixedSize(w, h)
        self._batch_primary_slot.setFixedSize(w, h)

    def _apply_page_title_icon(self) -> None:
        lb = getattr(self, "_page_title_icon_label", None)
        if lb is None or not RECIPE_ICON_PATH.is_file():
            return
        color = self.palette().color(QPalette.ColorRole.WindowText).name()
        px = 28
        ico = load_svg_icon(RECIPE_ICON_PATH, color, size=px)
        pm = ico.pixmap(px, px)
        if pm is not None and not pm.isNull():
            lb.setPixmap(pm)

    def _location_display_text(self) -> str:
        k = self._folder_list_selected_key
        if k == _LEGACY_FOLDER_KEY_ALL:
            k = FOLDER_KEY_UNCAT
        if k == FOLDER_KEY_UNCAT:
            loc = "未分类"
        else:
            loc = "文件夹"
            for f in load_recipe_folders():
                if str(f.get("id")) == k:
                    loc = str(f.get("name") or "").strip() or "文件夹"
                    break
        return f"当前文件夹：{loc}"

    def _update_location_label(self) -> None:
        self._location_label.setText(self._location_display_text())

    def _on_search_changed(self, _text: str) -> None:
        self._rebuild_recipe_rows()

    def _ordered_user_folder_ids_from_folder_list(self) -> list[str]:
        ids: list[str] = []
        for i in range(self._folder_list.count()):
            it = self._folder_list.item(i)
            if it is None:
                continue
            k = _folder_list_item_role_key(it)
            if k in (
                _LEGACY_FOLDER_KEY_ALL,
                FOLDER_KEY_UNCAT,
                FOLDER_SIDEBAR_PENDING_NEW_KEY,
            ):
                continue
            if k is None:
                continue
            ids.append(k)
        return ids

    def _on_folder_sidebar_order_committed(self) -> None:
        ids = self._ordered_user_folder_ids_from_folder_list()
        if not ids:
            return
        try:
            reorder_recipe_folders(ids)
        except ValueError:
            show_toast(self, "文件夹顺序保存失败", style="warning")
            self._refresh_folder_sidebar()
            return
        # 成功：侧栏顺序已与磁盘一致，勿 refresh（clear+setCurrentRow 会整表 scrollTo）

    def _detach_folder_sidebar_inline_before_repaint(self) -> None:
        self._folder_pending_new_item = None
        self._folder_pending_new_edit = None
        self._folder_rename_item = None
        self._folder_rename_edit = None
        self._folder_rename_id = None
        self._sync_folder_sidebar_app_filter()

    def _sync_folder_sidebar_app_filter(self) -> None:
        want = (
            self._folder_pending_new_edit is not None
            or self._folder_rename_edit is not None
        )
        app = QApplication.instance()
        if app is None:
            return
        if want and not self._folder_sidebar_app_filter_installed:
            app.installEventFilter(self)
            self._folder_sidebar_app_filter_installed = True
        elif not want and self._folder_sidebar_app_filter_installed:
            app.removeEventFilter(self)
            self._folder_sidebar_app_filter_installed = False

    def _toast_folder_warning(self, message: str, anchor: QWidget | None) -> None:
        if show_toast(self, message, style="warning"):
            return
        if anchor is not None:
            QToolTip.showText(
                anchor.mapToGlobal(anchor.rect().bottomLeft()),
                message,
                anchor,
            )

    def _on_folder_sidebar_inline_blur(self, edit: QLineEdit, role: str) -> None:
        if not self.isVisible():
            return
        if role == "pending_new":
            if self._folder_pending_new_edit is not edit:
                return
            fw = QApplication.focusWidget()
            host = edit.parentWidget()
            if fw is edit or (host is not None and host.isAncestorOf(fw)):
                return
            self._commit_folder_pending_new()
            return
        if role == "rename":
            if self._folder_rename_edit is not edit:
                return
            fw = QApplication.focusWidget()
            host = edit.parentWidget()
            if fw is edit or (host is not None and host.isAncestorOf(fw)):
                return
            self._commit_folder_inline_rename()

    def _folder_sidebar_on_app_mouse_press(self, event: QEvent) -> None:
        if self._folder_pending_new_edit is None and self._folder_rename_edit is None:
            return
        me = event
        if not isinstance(me, QMouseEvent) or me.button() != Qt.MouseButton.LeftButton:
            return
        if not self.isVisible():
            return
        w_at = QApplication.widgetAt(_qmouse_global_point(me))
        if w_at is None or not self.isAncestorOf(w_at):
            return
        if not self._folder_card.isAncestorOf(w_at):
            return
        edit = self._folder_pending_new_edit or self._folder_rename_edit
        if edit is None:
            return
        if _widget_is_under_tree(edit, w_at):
            return
        if _widget_is_under_tree(self.new_folder_btn, w_at):
            return
        if self._folder_pending_new_edit is not None:
            self._commit_folder_pending_new()
        else:
            self._commit_folder_inline_rename()

    def _restore_folder_list_scroll(self, saved: int) -> None:
        if not self.isVisible():
            return
        v = self._folder_list.verticalScrollBar()
        v.setValue(min(saved, v.maximum()))

    def _remove_folder_pending_new_row(self) -> None:
        if self._folder_pending_new_item is None:
            return
        vsb = self._folder_list.verticalScrollBar()
        saved = int(vsb.value())
        it = self._folder_pending_new_item
        w = self._folder_list.itemWidget(it)
        self._folder_list.removeItemWidget(it)
        if w is not None:
            w.deleteLater()
        row = self._folder_list.row(it)
        if row >= 0:
            self._folder_list.takeItem(row)
        self._folder_pending_new_item = None
        self._folder_pending_new_edit = None
        self._sync_folder_sidebar_app_filter()
        QTimer.singleShot(0, lambda s=saved: self._restore_folder_list_scroll(s))

    def _cancel_folder_inline_rename(self) -> None:
        if self._folder_rename_item is None or self._folder_rename_edit is None:
            self._folder_rename_id = None
            self._sync_folder_sidebar_app_filter()
            return
        it = self._folder_rename_item
        w = self._folder_list.itemWidget(it)
        self._folder_list.removeItemWidget(it)
        if w is not None:
            w.deleteLater()
        self._folder_rename_item = None
        self._folder_rename_edit = None
        self._folder_rename_id = None
        self._sync_folder_sidebar_app_filter()
        self._refresh_folder_sidebar()

    def _commit_folder_inline_rename(self) -> None:
        if (
            self._folder_rename_edit is None
            or self._folder_rename_id is None
            or self._folder_rename_item is None
        ):
            return
        name = self._folder_rename_edit.text().strip()
        if not name:
            self._cancel_folder_inline_rename()
            return
        try:
            rename_recipe_folder(self._folder_rename_id, name)
        except ValueError as e:
            if str(e) == ERR_DUPLICATE_RECIPE_FOLDER_NAME:
                self._toast_folder_warning(str(e), self._folder_rename_edit)
                self._cancel_folder_inline_rename()
                return
            self._toast_folder_warning(str(e), self._folder_rename_edit)
            re_edit = self._folder_rename_edit

            def _refocus() -> None:
                if re_edit is not None and self._folder_rename_edit is re_edit:
                    re_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                    re_edit.selectAll()

            QTimer.singleShot(0, _refocus)
            return
        it = self._folder_rename_item
        w = self._folder_list.itemWidget(it)
        self._folder_list.removeItemWidget(it)
        if w is not None:
            w.deleteLater()
        fid = self._folder_rename_id
        self._folder_rename_item = None
        self._folder_rename_edit = None
        self._folder_rename_id = None
        self._sync_folder_sidebar_app_filter()
        self._folder_list_selected_key = fid
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()

    def _commit_folder_pending_new(self) -> None:
        if self._folder_pending_new_edit is None:
            return
        name = self._folder_pending_new_edit.text().strip()
        if not name:
            self._remove_folder_pending_new_row()
            return
        try:
            rid = create_recipe_folder(name)
        except ValueError as e:
            if str(e) == ERR_DUPLICATE_RECIPE_FOLDER_NAME:
                self._toast_folder_warning(str(e), self._folder_pending_new_edit)
                self._remove_folder_pending_new_row()
                return
            self._toast_folder_warning(str(e), self._folder_pending_new_edit)
            re_edit = self._folder_pending_new_edit

            def _refocus() -> None:
                if re_edit is not None and self._folder_pending_new_edit is re_edit:
                    re_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                    re_edit.selectAll()

            QTimer.singleShot(0, _refocus)
            return
        self._remove_folder_pending_new_row()
        self._folder_list_selected_key = rid
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()

    def _folder_item_for_folder_id(self, folder_id: str) -> QListWidgetItem | None:
        for i in range(self._folder_list.count()):
            it = self._folder_list.item(i)
            if it is None:
                continue
            if _folder_list_item_role_key(it) == folder_id:
                return it
        return None

    def _folder_sidebar_item_pad_v(self) -> int:
        """与 theme/recipe_manage.qss 中 #recipeFolderList::item 上下 padding 8+8 一致。"""
        return 16

    def _folder_sidebar_normal_row_height(self) -> int:
        """普通文本行（无 itemWidget、非内联占位）的最大 sizeHintForRow，用于新建行与兄弟行同高。"""
        h = 0
        for i in range(self._folder_list.count()):
            it = self._folder_list.item(i)
            if it is None:
                continue
            if _folder_list_item_role_key(it) == FOLDER_SIDEBAR_PENDING_NEW_KEY:
                continue
            if self._folder_list.itemWidget(it) is not None:
                continue
            h = max(h, self._folder_list.sizeHintForRow(i))
        return h if h > 0 else 40

    def _folder_sidebar_edit_height_for_row_h(self, row_h: int) -> int:
        """与列表项内容区同高（总高 row_h 减去 QSS 上下 padding），不再额外限制为 26px。"""
        inner = max(0, row_h - self._folder_sidebar_item_pad_v())
        return max(16, inner)

    def _begin_folder_inline_rename(self, folder_id: str) -> None:
        self._remove_folder_pending_new_row()
        self._cancel_folder_inline_rename()
        it = self._folder_item_for_folder_id(folder_id)
        if it is None:
            return
        raw_name = ""
        for f in load_recipe_folders():
            if str(f.get("id")) == folder_id:
                raw_name = str(f.get("name") or "")
                break
        self._folder_list.ensurePolished()
        row = self._folder_list.row(it)
        row_h = self._folder_list.sizeHintForRow(row)
        if row_h <= 0:
            row_h = self._folder_sidebar_normal_row_height()
        edit_h = self._folder_sidebar_edit_height_for_row_h(row_h)
        host = QWidget()
        host.setFixedHeight(edit_h)
        hl = QHBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        edit = _FolderSidebarInlineEdit(host, page=self, role="rename")
        edit.setObjectName("recipeFolderListInlineEdit")
        edit.setFont(self._folder_list.font())
        edit.setText(raw_name)
        edit.setFixedHeight(edit_h)
        edit.returnPressed.connect(self._commit_folder_inline_rename)
        edit.escape_pressed.connect(self._cancel_folder_inline_rename)
        hl.addWidget(edit, 1, Qt.AlignmentFlag.AlignVCenter)
        self._folder_list.setItemWidget(it, host)
        vpw = max(self._folder_list.viewport().width(), max(0, self._folder_list.width() - 8))
        it.setSizeHint(QSize(vpw, row_h))
        self._folder_rename_item = it
        self._folder_rename_edit = edit
        self._folder_rename_id = folder_id
        self._sync_folder_sidebar_app_filter()

        def _focus_select() -> None:
            edit.setFocus(Qt.FocusReason.PopupFocusReason)
            edit.selectAll()

        QTimer.singleShot(0, _focus_select)

    def _on_new_folder(self) -> None:
        self._cancel_folder_inline_rename()
        if (
            self._folder_pending_new_item is not None
            and self._folder_list.row(self._folder_pending_new_item) >= 0
            and self._folder_pending_new_edit is not None
        ):

            def _again() -> None:
                if self._folder_pending_new_edit is not None:
                    self._folder_pending_new_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                    self._folder_pending_new_edit.selectAll()

            QTimer.singleShot(0, _again)
            return
        self._remove_folder_pending_new_row()
        self._folder_list.ensurePolished()
        row_h = self._folder_sidebar_normal_row_height()
        edit_h = self._folder_sidebar_edit_height_for_row_h(row_h)
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, FOLDER_SIDEBAR_PENDING_NEW_KEY)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        self._folder_list.addItem(it)
        host = QWidget()
        host.setFixedHeight(edit_h)
        hl = QHBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        edit = _FolderSidebarInlineEdit(host, page=self, role="pending_new")
        edit.setObjectName("recipeFolderListInlineEdit")
        edit.setFont(self._folder_list.font())
        edit.setText(MoveRecipeFolderDialog._default_new_folder_label())
        edit.setFixedHeight(edit_h)
        edit.returnPressed.connect(self._commit_folder_pending_new)
        edit.escape_pressed.connect(self._remove_folder_pending_new_row)
        hl.addWidget(edit, 1, Qt.AlignmentFlag.AlignVCenter)
        self._folder_list.setItemWidget(it, host)
        vpw = max(self._folder_list.viewport().width(), max(0, self._folder_list.width() - 8))
        it.setSizeHint(QSize(vpw, row_h))
        self._folder_pending_new_item = it
        self._folder_pending_new_edit = edit
        self._sync_folder_sidebar_app_filter()

        def _scroll_focus(*, _it=it, _edit=edit) -> None:
            QApplication.processEvents()
            self._folder_list.scrollToItem(_it, QAbstractItemView.ScrollHint.PositionAtBottom)
            _edit.setFocus(Qt.FocusReason.PopupFocusReason)
            _edit.selectAll()

        QTimer.singleShot(0, _scroll_focus)

    def _populate_folder_list(self, *, preserve_key: str | None) -> None:
        stats = get_recipe_folder_stats()
        uncategorized: int = int(stats.get("uncategorized") or 0)
        by_folder: dict[str, int] = stats.get("by_folder") or {}
        if not isinstance(by_folder, dict):
            by_folder = {}

        key_to_find = preserve_key if preserve_key is not None else self._folder_list_selected_key
        if key_to_find == _LEGACY_FOLDER_KEY_ALL:
            key_to_find = FOLDER_KEY_UNCAT

        self._folder_list.blockSignals(True)
        self._detach_folder_sidebar_inline_before_repaint()
        self._folder_list.clear()

        it_u = QListWidgetItem(f"未分类 ({uncategorized})")
        it_u.setData(Qt.ItemDataRole.UserRole, FOLDER_KEY_UNCAT)
        _f_sel = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        _f_dd = Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        it_u.setFlags(_f_sel | Qt.ItemFlag.ItemIsDropEnabled)
        self._folder_list.addItem(it_u)

        for f in load_recipe_folders():
            fid = str(f.get("id") or "")
            name = str(f.get("name") or "")
            c = int(by_folder.get(fid, 0))
            it = QListWidgetItem(f"{name} ({c})")
            it.setData(Qt.ItemDataRole.UserRole, fid)
            it.setFlags(_f_sel | _f_dd)
            self._folder_list.addItem(it)

        row = 0
        for i in range(self._folder_list.count()):
            it = self._folder_list.item(i)
            if it and _folder_list_item_role_key(it) == key_to_find:
                row = i
                break
        self._folder_list.setCurrentRow(row)
        cur = self._folder_list.currentItem()
        if cur is not None:
            k = _folder_list_item_role_key(cur)
            self._folder_list_selected_key = _sidebar_folder_key_normalized(
                k if k is not None else FOLDER_KEY_UNCAT
            )
        # 必须在 setCurrentRow 仍阻塞信号时结束，否则会误发 currentItemChanged，
        # 进而 _rebuild_recipe_rows，与调用方（如移动后刷新）形成二次重建与界面闪动。
        self._folder_list.blockSignals(False)

    def _refresh_folder_sidebar(self) -> None:
        self._populate_folder_list(preserve_key=self._folder_list_selected_key)
        self._update_location_label()

    def _on_folder_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        k = _folder_list_item_role_key(current)
        if k == FOLDER_SIDEBAR_PENDING_NEW_KEY:
            return
        new_key = _sidebar_folder_key_normalized(k if k is not None else FOLDER_KEY_UNCAT)
        # 勿用 new_key == _folder_list_selected_key：_populate_folder_list 会在 blockSignals 内先改 selected_key，
        # 再触发 currentItemChanged，此时若与「已渲染列表」不一致却会被误判为未切换，导致跳过重建、批量勾选失效。
        rendered = self._recipe_list_rendered_folder_key
        if rendered is not None and new_key == _sidebar_folder_key_normalized(rendered):
            if self._folder_list_selected_key != new_key:
                self._folder_list_selected_key = new_key
                self._update_location_label()
            return
        # 批量管理下切换文件夹：与手动「完成」一致先退出；列表重建后延迟一拍再进入批量，
        # 否则同一次事件循环内立刻 set_batch_visible/findChildren 装 filter 时布局未稳定，勾选/划选仍失效。
        resume_batch = self._batch_mode
        if resume_batch:
            self._on_batch_done()
        self._folder_list_selected_key = new_key
        self._update_location_label()
        self._rebuild_recipe_rows()
        if resume_batch:
            tok = self._batch_resume_token
            QTimer.singleShot(
                0,
                lambda t=tok: self._apply_deferred_batch_resume_after_folder_change(t),
            )

    def _on_folder_item_double_clicked(self, item: QListWidgetItem) -> None:
        key = _folder_list_item_role_key(item)
        if key in (None, _LEGACY_FOLDER_KEY_ALL, FOLDER_KEY_UNCAT, FOLDER_SIDEBAR_PENDING_NEW_KEY):
            return
        self._begin_folder_inline_rename(str(key))

    def _on_folder_list_context_menu(self, pos: QPoint) -> None:
        item = self._folder_list.itemAt(pos)
        if item is None:
            return
        key = _folder_list_item_role_key(item)
        # 仅用户文件夹项可右键；空白、未分类、旧版「全部」项不弹出菜单（新建用下方按钮）
        if key in (None, _LEGACY_FOLDER_KEY_ALL, FOLDER_KEY_UNCAT, FOLDER_SIDEBAR_PENDING_NEW_KEY):
            return
        fid = key
        menu = QMenu(self)
        _apply_recipe_manage_menu_style(menu)
        act_rename = QAction("重命名", self)
        act_rename.triggered.connect(lambda: self._begin_folder_inline_rename(fid))
        act_del_only = QAction("仅删除文件夹", self)
        act_del_only.triggered.connect(lambda: self._delete_folder_only_confirm(fid))
        act_del_all = QAction("删除文件夹及其中配方", self)
        act_del_all.triggered.connect(lambda: self._delete_folder_and_recipes_confirm(fid))
        menu.addAction(act_rename)
        menu.addSeparator()
        menu.addAction(act_del_only)
        menu.addAction(act_del_all)
        menu.exec(self._folder_list.mapToGlobal(pos))

    def _count_recipes_in_folder(self, folder_id: str) -> int:
        stats = get_recipe_folder_stats()
        by_folder = stats.get("by_folder") or {}
        if not isinstance(by_folder, dict):
            return 0
        return int(by_folder.get(folder_id, 0))

    def _delete_folder_only_confirm(self, folder_id: str) -> None:
        display = _folder_display_name(folder_id)
        if not ask_confirmation_sequence(
            self,
            (
                (
                    "仅删除文件夹",
                    f"将删除文件夹「{display}」，其中的配方会保留并移回「未分类」，不会删除配方文件。\n\n"
                    "确定继续？",
                ),
                (
                    "再次确认",
                    f"再次确认：仅删除文件夹「{display}」？配方将移至未分类。",
                ),
            ),
        ):
            return
        try:
            n = delete_recipe_folder(folder_id)
        except ValueError as e:
            show_toast(self, str(e), style="warning")
            return
        show_toast(self, f"已删除文件夹，{n} 条配方已移回未分类", style="success")
        self._folder_list_selected_key = FOLDER_KEY_UNCAT
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()

    def _delete_folder_and_recipes_confirm(self, folder_id: str) -> None:
        display = _folder_display_name(folder_id)
        n_recipes = self._count_recipes_in_folder(folder_id)
        if not ask_confirmation_sequence(
            self,
            (
                (
                    "删除文件夹及配方",
                    f"将永久删除文件夹「{display}」及其中的全部配方（共 {n_recipes} 条），"
                    "配方文件会从磁盘删除且无法恢复。\n\n"
                    "确定继续？",
                ),
                (
                    "再次确认",
                    f"最后确认：永久删除文件夹「{display}」及其中 {n_recipes} 条配方？此操作不可恢复。",
                ),
            ),
        ):
            return
        try:
            n = delete_recipe_folder_and_recipes(folder_id)
        except ValueError as e:
            show_toast(self, str(e), style="warning")
            return
        show_toast(self, f"已删除文件夹及 {n} 条配方", style="success")
        self._folder_list_selected_key = FOLDER_KEY_UNCAT
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()

    def _set_batch_ui(self) -> None:
        in_batch = self._batch_mode
        want_idx = 1 if in_batch else 0
        # 切到占位页前先在按钮仍可见时缓存尺寸，避免随后对隐藏控件读 sizeHint 得到近 0
        if self._batch_stack_layout.currentIndex() == 0 and want_idx == 1:
            self._capture_batch_primary_size_cache()
        if self._batch_stack_layout.currentIndex() != want_idx:
            self._batch_stack_layout.setCurrentIndex(want_idx)
        self._sync_batch_primary_stack_geometry()
        self.select_all_btn.setVisible(in_batch)
        self.move_btn.setVisible(in_batch)
        self.delete_btn.setVisible(in_batch)
        self.batch_done_btn.setVisible(in_batch)
        self.import_cs2th_btn.setVisible(not in_batch)
        if not in_batch:
            self.select_all_btn.setText("全选")
        for r in self._rows:
            r.set_batch_visible(in_batch)
        if in_batch:
            self._refresh_select_all_btn_label()

    def _refresh_select_all_btn_label(self) -> None:
        if not self._batch_mode or not self._rows:
            self.select_all_btn.setText("全选")
            return
        self.select_all_btn.setText(
            "全不选" if all(r.is_checked() for r in self._rows) else "全选"
        )

    def _on_toggle_batch(self) -> None:
        self._batch_mode = True
        self._set_batch_ui()

    def _on_batch_done(self) -> None:
        self._batch_drag_cancel_without_toggle()
        self._batch_resume_token += 1
        self._batch_mode = False
        self._set_batch_ui()

    def prepare_leave_for_sidebar_switch(self) -> None:
        """侧栏将切换到其它主页面之前调用：与点「完成」一致退出批量，并作废挂起的文件夹切换后恢复批量回调。"""
        if self._batch_mode:
            self._on_batch_done()
        else:
            self._batch_drag_cancel_without_toggle()
            self._batch_release_viewport_drag_hook()

    def _apply_deferred_batch_resume_after_folder_change(self, tok: int) -> None:
        if self._batch_resume_token != tok:
            return
        if not self.isVisible():
            return
        self._batch_mode = True
        self._set_batch_ui()
        QTimer.singleShot(
            0,
            lambda t=tok: self._refresh_batch_row_filters_after_folder_resume(t),
        )

    def _refresh_batch_row_filters_after_folder_resume(self, tok: int) -> None:
        if self._batch_resume_token != tok:
            return
        if not self.isVisible() or not self._batch_mode:
            return
        for r in self._rows:
            r._update_batch_click_filters()

    def _batch_row_press_begin(self, row: _SavedRecipeRow, global_pos: QPoint) -> bool:
        if not self._batch_mode:
            return False
        if self._batch_drag_active:
            self._batch_drag_cancel_without_toggle()
        elif self._batch_viewport_drag_hooked:
            self._batch_release_viewport_drag_hook()
        try:
            idx = self._rows.index(row)
        except ValueError:
            # 常见原因：列表已重建但旧行尚未从父控件脱离，仍命中测试并收到点击；不可吞掉事件
            return False
        self._batch_drag_active = True
        self._batch_drag_anchor_row = row
        self._batch_drag_anchor_index = idx
        self._batch_drag_start_global = QPoint(global_pos)
        self._batch_drag_last_global = QPoint(global_pos)
        self._batch_drag_moved = False
        self._batch_drag_start_checked = [r.is_checked() for r in self._rows]
        self._batch_drag_select_mode = not row._check.isChecked()
        vp = self._recipe_list_scroll.viewport()
        if not self._batch_viewport_drag_hooked:
            vp.installEventFilter(self)
            vp.grabMouse()
            self._batch_viewport_drag_hooked = True
        return True

    def _batch_row_index_at_global(self, global_pos: QPoint) -> int | None:
        """指针映射到 list_container 坐标（含列表外时钳位），再解析所在行。"""
        if not self._rows:
            return None
        lp = self.list_container.mapFromGlobal(global_pos)
        lr = self.list_container.rect()
        lp = QPoint(
            max(0, min(lr.width() - 1, lp.x())),
            max(0, min(lr.height() - 1, lp.y())),
        )
        for i, row in enumerate(self._rows):
            if row.geometry().contains(lp):
                return i
        best_i = 0
        best_d = 10**9
        for i, row in enumerate(self._rows):
            cy = row.geometry().center().y()
            d = abs(lp.y() - cy)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _batch_compute_autoscroll_delta(self, global_pos: QPoint) -> int:
        """相对视口边缘越外，单帧滚动量越大（正数向下）。"""
        vp = self._recipe_list_scroll.viewport()
        gr = QRect(vp.mapToGlobal(QPoint(0, 0)), vp.size())
        x, y = global_pos.x(), global_pos.y()
        if x < gr.left() or x > gr.right():
            return 0
        if y < gr.top():
            dist = gr.top() - y
            step = 2 + (dist * dist) // 200
            return -max(1, min(96, step))
        if y > gr.bottom():
            dist = y - gr.bottom()
            step = 2 + (dist * dist) // 200
            return max(1, min(96, step))
        return 0

    def _batch_autoscroll_maybe_update(self, global_pos: QPoint) -> None:
        if not self._batch_drag_active:
            return
        if self._batch_compute_autoscroll_delta(global_pos) != 0:
            if not self._batch_autoscroll_timer.isActive():
                self._batch_autoscroll_timer.start()
        else:
            self._batch_autoscroll_timer.stop()

    def _batch_autoscroll_tick(self) -> None:
        if not self._batch_drag_active:
            self._batch_autoscroll_timer.stop()
            return
        g = self._batch_drag_last_global
        d = self._batch_compute_autoscroll_delta(g)
        if d == 0:
            self._batch_autoscroll_timer.stop()
            return
        self._batch_drag_moved = True
        sb = self._recipe_list_scroll.verticalScrollBar()
        sb.setValue(sb.value() + d)
        self._batch_drag_paint_checks_for_global(g)

    def _batch_drag_paint_checks_for_global(self, global_pos: QPoint) -> None:
        if not self._batch_drag_active or self._batch_drag_anchor_index < 0:
            return
        cur = self._batch_row_index_at_global(global_pos)
        if cur is None:
            return
        lo, hi = min(self._batch_drag_anchor_index, cur), max(self._batch_drag_anchor_index, cur)
        snap = self._batch_drag_start_checked
        if len(snap) != len(self._rows):
            return
        sel = self._batch_drag_select_mode
        for i, row in enumerate(self._rows):
            if sel:
                want = snap[i] or (lo <= i <= hi)
            else:
                want = snap[i] and not (lo <= i <= hi)
            cb = row._check
            if cb.isChecked() == want:
                continue
            cb.blockSignals(True)
            cb.setChecked(want)
            cb.blockSignals(False)
        self._refresh_select_all_btn_label()

    def _batch_drag_apply_range(self, global_pos: QPoint) -> None:
        if not self._batch_drag_active or self._batch_drag_anchor_index < 0:
            return
        delta = QPoint(global_pos) - self._batch_drag_start_global
        if delta.manhattanLength() < QApplication.startDragDistance():
            return
        self._batch_drag_moved = True
        self._batch_drag_paint_checks_for_global(global_pos)

    def _batch_drag_end(self) -> None:
        self._batch_autoscroll_timer.stop()
        if not self._batch_drag_active:
            return
        self._batch_release_viewport_drag_hook()
        if not self._batch_drag_moved and self._batch_drag_anchor_row is not None:
            self._batch_drag_anchor_row._check.toggle()
        self._batch_drag_active = False
        self._batch_drag_anchor_row = None
        self._batch_drag_anchor_index = -1
        self._batch_drag_start_checked = []
        self._refresh_select_all_btn_label()

    def _batch_drag_cancel_without_toggle(self) -> None:
        self._batch_autoscroll_timer.stop()
        if not self._batch_drag_active:
            return
        self._batch_release_viewport_drag_hook()
        self._batch_drag_active = False
        self._batch_drag_anchor_row = None
        self._batch_drag_anchor_index = -1
        self._batch_drag_start_checked = []

    def _batch_release_viewport_drag_hook(self) -> None:
        if not self._batch_viewport_drag_hooked:
            return
        vp = self._recipe_list_scroll.viewport()
        vp.removeEventFilter(self)
        # PySide6 的 QWidget 无 hasMouseGrab；未 grab 时 releaseMouse() 也为空操作
        vp.releaseMouse()
        self._batch_viewport_drag_hooked = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._folder_sidebar_app_filter_installed:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._folder_sidebar_on_app_mouse_press(event)
        if not self._batch_drag_active or watched is not self._recipe_list_scroll.viewport():
            return super().eventFilter(watched, event)
        et = event.type()
        if et == QEvent.Type.MouseMove:
            me = event
            if isinstance(me, QMouseEvent) and me.buttons() & Qt.MouseButton.LeftButton:
                self._batch_drag_last_global = _qmouse_global_point(me)
                self._batch_autoscroll_maybe_update(self._batch_drag_last_global)
                self._batch_drag_apply_range(self._batch_drag_last_global)
            return False
        if et == QEvent.Type.MouseButtonRelease:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self._batch_drag_end()
            return False
        return super().eventFilter(watched, event)

    def _on_select_all_toggle(self) -> None:
        if not self._rows:
            return
        all_on = all(r.is_checked() for r in self._rows)
        for r in self._rows:
            r._check.blockSignals(True)
            r._check.setChecked(not all_on)
            r._check.blockSignals(False)
        self._refresh_select_all_btn_label()

    def _move_selected_to_folder(self, folder_id: str | None) -> None:
        paths = [r.path() for r in self._rows if r.is_checked()]
        if not paths:
            show_toast(self, "请先勾选要移动的配方", style="warning")
            return
        n = move_recipes_to_folder(paths, folder_id)
        show_toast(self, f"已移动 {n} 条", style="success")
        # 与切换文件夹一致：先退出批量再重建列表，延迟一拍再进入批量，避免布局未稳时勾选/划选失效
        resume_batch = self._batch_mode
        if resume_batch:
            self._on_batch_done()
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()
        if resume_batch:
            tok = self._batch_resume_token
            QTimer.singleShot(
                0,
                lambda t=tok: self._apply_deferred_batch_resume_after_folder_change(t),
            )

    def _on_delete_selected(self) -> None:
        paths = [r.path() for r in self._rows if r.is_checked()]
        if not paths:
            show_toast(self, "请先勾选要删除的配方", style="warning")
            return
        if not ask_confirmation(
            self,
            "删除配方",
            f"确定删除选中的 {len(paths)} 条配方？此操作不可恢复。",
        ):
            return
        n = delete_recipe_files(paths)
        show_toast(self, f"已删除 {n} 条", style="success")
        self._batch_resume_token += 1
        self._batch_mode = False
        self._set_batch_ui()
        self._refresh_folder_sidebar()
        self._rebuild_recipe_rows()

    def _rebuild_recipe_rows(self) -> None:
        self._batch_drag_cancel_without_toggle()
        # 非拖动结束时 _batch_drag_active 可能已为 False，但 viewport 上仍可能残留 filter/grab
        self._batch_release_viewport_drag_hook()
        for r in self._rows:
            try:
                r._check.stateChanged.disconnect(self._refresh_select_all_btn_label)
            except TypeError:
                pass
        while self.list_layout.count():
            ch = self.list_layout.takeAt(0)
            w = ch.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._rows.clear()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # 勿在此 processEvents：部分显卡/远程桌面下会强制处理绘制与内部浮层，表现为成片小弹窗闪烁。

        filt = _folder_filter_from_key(self._folder_list_selected_key)
        items = list_saved_recipes(folder_filter=filt)
        q = self._search_edit.text().strip().lower()
        if q:
            items = [
                (p, d)
                for p, d in items
                if q in _display_row_title(d).lower()
                or q in str(p.name).lower()
            ]

        stats = get_recipe_folder_stats()
        total_all = int(stats.get("total") or 0)
        if self._view_mode == "recipes":
            self._title_count_label.setText(f"共{total_all}条配方")

        if len(items) == 0:
            self.empty_label.setVisible(True)
            if total_all == 0:
                self.empty_label.setText(
                    "暂无已保存配方。在炼金计算结果中点击「保存配方」即可保存到此。"
                )
            else:
                self.empty_label.setText(
                    "当前没有可显示的配方。"
                )
        else:
            self.empty_label.setVisible(False)

        for path, payload in items:
            row = _SavedRecipeRow(
                path,
                payload,
                self._batch_mode,
                self.list_container,
                recipe_page=self,
            )
            row._check.stateChanged.connect(self._refresh_select_all_btn_label)
            self.list_layout.addWidget(row)
            self._rows.append(row)
        self.list_layout.addStretch(1)
        self._set_batch_ui()
        self._update_location_label()
        self._recipe_list_rendered_folder_key = _sidebar_folder_key_normalized(
            self._folder_list_selected_key
        )

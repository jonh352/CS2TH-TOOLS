"""配方管理批量移动：在弹窗中用方框勾选（互斥单选）选择目标文件夹。

样式与「排除配方」弹窗一致：遮罩 + loginBox，随主窗口主题 QSS。"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from core.saved_recipes import (
    ERR_DUPLICATE_RECIPE_FOLDER_NAME,
    create_recipe_folder,
    get_recipe_folder_stats,
    load_recipe_folders,
    rename_recipe_folder,
)
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.widgets.toast import show_toast

_UNCAT_KEY = "_uncat"
_LEGACY_ALL_KEY = "_all"
_ROLE_UNCAT = "__MOVE_TO_UNCAT__"


def build_all_recipe_folder_pick_targets() -> list[tuple[str | None, str]]:
    """保存配方时选文件夹：(folder_id, 显示名)；含「未分类」与全部用户文件夹，显示配方数量（与配方管理侧栏一致）。"""
    stats = get_recipe_folder_stats()
    uncategorized = int(stats.get("uncategorized") or 0)
    by_folder = stats.get("by_folder") or {}
    if not isinstance(by_folder, dict):
        by_folder = {}
    out: list[tuple[str | None, str]] = [(None, f"未分类 ({uncategorized})")]
    for f in load_recipe_folders():
        fid = str(f.get("id") or "")
        if not fid:
            continue
        name = str(f.get("name") or "").strip() or "文件夹"
        c = int(by_folder.get(fid, 0))
        out.append((fid, f"{name} ({c})"))
    return out


def build_move_targets(current_folder_key: str) -> list[tuple[str | None, str]]:
    """(folder_id, 显示名)；folder_id 为 None 表示未分类。排除当前所在文件夹。"""
    k = current_folder_key
    if k == _LEGACY_ALL_KEY:
        k = _UNCAT_KEY
    out: list[tuple[str | None, str]] = []
    if k != _UNCAT_KEY:
        out.append((None, "未分类"))
    for f in load_recipe_folders():
        fid = str(f.get("id") or "")
        if not fid:
            continue
        if k == fid:
            continue
        name = str(f.get("name") or "").strip() or "文件夹"
        out.append((fid, name))
    return out


class _EscapeLineEdit(QLineEdit):
    escape_pressed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        blur_dialog: MoveRecipeFolderDialog | None = None,
        blur_role: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._blur_dialog = blur_dialog
        self._blur_role = blur_role

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if self._blur_dialog is None or not self._blur_role:
            return
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        dlg = self._blur_dialog
        role = self._blur_role
        QTimer.singleShot(0, lambda: dlg._on_inline_edit_blur_commit(self, role))

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


class _RecipeTitleLineEdit(QLineEdit):
    """保存配方名：回车/小键盘回车仅失焦，不激活对话框默认按钮。"""

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class _FolderPickRow(QWidget):
    """整行可点选：左侧 QCheckBox，右侧空白区域同属一行命中。"""

    def __init__(
        self,
        cb: QCheckBox,
        parent: QWidget | None = None,
        *,
        rename_folder_id: str | None = None,
        dialog: MoveRecipeFolderDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("moveRecipeFolderPickRow")
        self._cb = cb
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(0)
        lay.addWidget(cb, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        cb.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        cb.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        cb.installEventFilter(self)
        if rename_folder_id and dialog is not None:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(
                lambda pos, d=dialog, r=self, c=cb, fid=rename_folder_id: d._on_pick_row_context_menu(
                    r, c, fid, pos
                ),
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._cb and event.type() == QEvent.Type.MouseButtonPress:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self._cb.setChecked(True)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._cb.setChecked(True)
        super().mousePressEvent(event)


class MoveRecipeFolderDialog(QDialog):
    """方框勾选（互斥）目标文件夹，再点「确定」。"""

    def __init__(
        self,
        parent,
        *,
        targets: list[tuple[str | None, str]],
        dialog_title: str = "移动到",
        hint_text: str = "请勾选一个目标文件夹，然后点击「确定」。",
        allow_create_folder: bool = False,
        recipe_name_default: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MoveRecipeFolderDialog")
        self.setWindowTitle(dialog_title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._allow_create_folder = allow_create_folder
        self._recipe_title_edit: QLineEdit | None = None
        self._chosen_folder_id: str | None = None
        self._pick_group = QButtonGroup(self)
        self._pick_group.setExclusive(True)

        self._pending_new_host: QWidget | None = None
        self._pending_new_edit: _EscapeLineEdit | None = None

        self._rename_edit: _EscapeLineEdit | None = None
        self._rename_cb: QCheckBox | None = None
        self._rename_row: _FolderPickRow | None = None
        self._rename_fid: str | None = None
        self._rename_idx: int = 0

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
        box.setMinimumWidth(440)
        box.setMaximumWidth(560)
        box.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )

        layout = QVBoxLayout(box)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel(dialog_title)
        title_label.setObjectName("loginTitle")
        header.addWidget(title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        self._header_close_btn = QPushButton("✕")
        self._header_close_btn.setObjectName("loginCloseBtn")
        self._header_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_close_btn.setAutoDefault(False)
        self._header_close_btn.setDefault(False)
        self._header_close_btn.clicked.connect(self.reject)
        header.addWidget(self._header_close_btn, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        layout.addSpacing(6)

        if recipe_name_default is not None:
            name_row = QHBoxLayout()
            name_row.setSpacing(8)
            name_row.setContentsMargins(0, 0, 0, 0)
            name_lbl = QLabel("配方名：")
            name_lbl.setObjectName("moveRecipeFolderHint")
            name_lbl.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            )
            self._recipe_title_edit = _RecipeTitleLineEdit()
            self._recipe_title_edit.setObjectName("moveRecipeFolderRecipeTitleEdit")
            self._recipe_title_edit.setClearButtonEnabled(True)
            self._recipe_title_edit.setText(recipe_name_default)
            self._recipe_title_edit.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            name_row.addWidget(name_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
            name_row.addWidget(self._recipe_title_edit, 1, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(name_row)
            layout.addSpacing(10)

        hint = QLabel(hint_text)
        hint.setObjectName("moveRecipeFolderHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(8)

        scroll = QScrollArea()
        scroll.setObjectName("moveRecipeFolderScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        scroll.setMaximumHeight(280)

        self._inner = QWidget()
        self._inner.setObjectName("moveRecipeFolderCheckHost")
        self._inner_lay = QVBoxLayout(self._inner)
        self._inner_lay.setContentsMargins(10, 8, 10, 8)
        self._inner_lay.setSpacing(2)

        scroll.setWidget(self._inner)
        layout.addWidget(scroll, 0)
        self._folder_scroll = scroll

        layout.addSpacing(14)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.setContentsMargins(0, 0, 0, 0)

        self._new_folder_btn: QPushButton | None = None
        if allow_create_folder:
            self._new_folder_btn = QPushButton("新建文件夹")
            self._new_folder_btn.setObjectName("alchemySelectFileBtn")
            self._new_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._new_folder_btn.setAutoDefault(False)
            self._new_folder_btn.setDefault(False)
            self._new_folder_btn.clicked.connect(self._on_new_folder_clicked)
            btn_row.addWidget(self._new_folder_btn, 0, Qt.AlignmentFlag.AlignLeft)

        btn_row.addStretch(1)

        self._ok_btn = QPushButton("确定")
        self._ok_btn.setObjectName("alchemySelectFileBtn")
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok_btn.setAutoDefault(False)
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self._on_accept_clicked)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        self._rebuild_checkboxes(targets)

        overlay_layout.addWidget(box)
        self._content_box = box

        def on_overlay_click(event):
            if event.button() == Qt.LeftButton:
                w = overlay.childAt(event.pos().x(), event.pos().y())
                if w is None or (w != box and not box.isAncestorOf(w)):
                    self._reset_transient_ui()
                    self.reject()

        overlay.mousePressEvent = on_overlay_click
        install_dialog_topmost_follow_parent(self)
        # 避免首次 Show 时 Win32 以 ~100x30 建窗再与 minimumSizeHint 冲突（见 ExcludeSavedRecipesDialog）
        self.setMinimumSize(420, 464)

    @staticmethod
    def _widget_is_under_tree(root: QWidget, w: QWidget) -> bool:
        cur: QWidget | None = w
        while cur is not None:
            if cur is root:
                return True
            cur = cur.parentWidget()
        return False

    def _blur_recipe_title_if_click_outside(self, global_pos: QPoint) -> None:
        ed = self._recipe_title_edit
        if ed is None or not ed.hasFocus():
            return
        w_at = QApplication.widgetAt(global_pos)
        if w_at is None or not self.isAncestorOf(w_at):
            return
        if w_at is ed or ed.isAncestorOf(w_at):
            return
        ed.clearFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """点击滚动区空白、列表底边等不抢焦点的区域时，QLineEdit 不会失焦；用全局鼠标按下补做「点外部即提交」。"""
        if event.type() == QEvent.Type.MouseButtonPress:
            me = event
            if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.LeftButton:
                self._blur_recipe_title_if_click_outside(me.globalPosition().toPoint())

        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        if self._pending_new_edit is None and self._rename_edit is None:
            return False
        me = event
        if not isinstance(me, QMouseEvent) or me.button() != Qt.MouseButton.LeftButton:
            return False
        w_at = QApplication.widgetAt(me.globalPosition().toPoint())
        if w_at is None or not self.isAncestorOf(w_at):
            return False
        if w_at is not self._content_box and not self._content_box.isAncestorOf(w_at):
            return False
        edit = self._pending_new_edit or self._rename_edit
        if edit is None:
            return False
        if self._widget_is_under_tree(edit, w_at):
            return False
        if w_at is self._header_close_btn:
            return False
        if self._widget_is_under_tree(self._header_close_btn, w_at):
            if self._pending_new_edit is not None:
                self._remove_pending_new_folder_row()
            elif self._rename_edit is not None:
                self._cancel_inline_rename()
            return False
        if self._pending_new_edit is not None:
            self._commit_pending_new_folder()
        else:
            self._commit_inline_rename()
        return False

    def hideEvent(self, event) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def _scroll_folder_list_to_bottom(self) -> None:
        bar = self._folder_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _sync_folder_list_hover_styles(self) -> None:
        """点击区外提交时鼠标未动，QSS :hover 可能仍留在旧控件上；合成 Leave 并刷新。"""
        scroll = self._folder_scroll
        vp = scroll.viewport()
        if vp is not None:
            QApplication.sendEvent(vp, QEvent(QEvent.Type.Leave))
            vp.update()
        vsb = scroll.verticalScrollBar()
        QApplication.sendEvent(vsb, QEvent(QEvent.Type.Leave))
        vsb.update()
        for i in range(self._inner_lay.count()):
            it = self._inner_lay.itemAt(i)
            if it is None:
                continue
            row = it.widget()
            if row is None:
                continue
            QApplication.sendEvent(row, QEvent(QEvent.Type.Leave))
            for cb in row.findChildren(QCheckBox):
                if cb.objectName() == "moveRecipeFolderCheck":
                    QApplication.sendEvent(cb, QEvent(QEvent.Type.Leave))
            row.update()
        self._inner.update()
        scroll.update()

    def _schedule_sync_folder_list_hover(self) -> None:
        self._sync_folder_list_hover_styles()
        QTimer.singleShot(0, self._sync_folder_list_hover_styles)

    def _toast_target_widget(self) -> QWidget:
        """Toast 能力在主窗口；本对话框是独立 window() 时 self.window() 不是主窗口。"""
        p = self.parentWidget()
        if p is not None:
            return p.window()
        return self

    def _toast_warning_or_tooltip(self, message: str, anchor: QWidget | None) -> None:
        """主窗口 Toast；若无 toast 则回退到锚点旁气泡。"""
        if show_toast(self._toast_target_widget(), message, style="warning"):
            return
        if anchor is not None:
            QToolTip.showText(
                anchor.mapToGlobal(anchor.rect().bottomLeft()),
                message,
                anchor,
            )

    def _on_inline_edit_blur_commit(self, edit: QLineEdit, role: str) -> None:
        if not self.isVisible():
            return
        if role == "pending_new":
            if self._pending_new_edit is not edit:
                return
            fw = QApplication.focusWidget()
            if fw is edit or (
                self._pending_new_host is not None and self._pending_new_host.isAncestorOf(fw)
            ):
                return
            if fw is self._header_close_btn:
                self._remove_pending_new_folder_row()
                return
            self._commit_pending_new_folder()
            return
        if role == "rename":
            if self._rename_edit is not edit:
                return
            fw = QApplication.focusWidget()
            if fw is edit or (
                self._rename_row is not None and self._rename_row.isAncestorOf(fw)
            ):
                return
            if fw is self._header_close_btn:
                self._cancel_inline_rename()
                return
            self._commit_inline_rename()

    @staticmethod
    def _default_new_folder_label() -> str:
        """仅用于「新建文件夹」内联框的初始占位：自动 新建文件夹 / 新建文件夹1 … 不与已有重名。"""
        names = {str(f.get("name") or "").strip() for f in load_recipe_folders()}
        base = "新建文件夹"
        if base not in names:
            return base
        i = 1
        while f"{base}{i}" in names:
            i += 1
        return f"{base}{i}"

    def _clear_rename_refs(self) -> None:
        self._rename_edit = None
        self._rename_cb = None
        self._rename_row = None
        self._rename_fid = None

    def _cancel_inline_rename(self) -> None:
        if self._rename_edit is None or self._rename_row is None or self._rename_cb is None:
            self._clear_rename_refs()
            return
        lay = self._rename_row.layout()
        if lay is not None:
            if self._rename_edit is not None:
                QApplication.sendEvent(self._rename_edit, QEvent(QEvent.Type.Leave))
            lay.removeWidget(self._rename_edit)
            self._rename_edit.deleteLater()
            lay.insertWidget(self._rename_idx, self._rename_cb, 0, Qt.AlignmentFlag.AlignVCenter)
            self._rename_cb.show()
        self._clear_rename_refs()
        self._schedule_sync_folder_list_hover()

    def _remove_pending_new_folder_row(self) -> None:
        if self._pending_new_host is None:
            return
        if self._pending_new_edit is not None:
            QApplication.sendEvent(self._pending_new_edit, QEvent(QEvent.Type.Leave))
        QApplication.sendEvent(self._pending_new_host, QEvent(QEvent.Type.Leave))
        if self._inner_lay.indexOf(self._pending_new_host) >= 0:
            self._inner_lay.removeWidget(self._pending_new_host)
        self._pending_new_host.deleteLater()
        self._pending_new_host = None
        self._pending_new_edit = None
        self._schedule_sync_folder_list_hover()

        def _scroll_bottom_after_remove() -> None:
            if not self.isVisible():
                return
            QApplication.processEvents()
            self._scroll_folder_list_to_bottom()

        QTimer.singleShot(0, _scroll_bottom_after_remove)

    def _reset_transient_ui(self) -> None:
        self._cancel_inline_rename()
        self._remove_pending_new_folder_row()

    def _on_pick_row_context_menu(
        self,
        row: _FolderPickRow,
        cb: QCheckBox,
        folder_id: str,
        pos,
    ) -> None:
        if self._rename_row is row and self._rename_edit is not None:
            return
        if self._rename_edit is not None:
            self._cancel_inline_rename()
        menu = QMenu(self)
        menu.setObjectName("moveRecipeFolderContextMenu")
        act_rename = menu.addAction("重命名")
        chosen = menu.exec(row.mapToGlobal(pos))
        if chosen == act_rename:
            self._begin_inline_rename(row, cb, folder_id)

    def _begin_inline_rename(self, row: _FolderPickRow, cb: QCheckBox, folder_id: str) -> None:
        self._cancel_inline_rename()
        self._remove_pending_new_folder_row()
        lay = row.layout()
        if lay is None:
            return
        idx = lay.indexOf(cb)
        if idx < 0:
            return
        lay.removeWidget(cb)
        cb.hide()
        edit = _EscapeLineEdit(row, blur_dialog=self, blur_role="rename")
        edit.setObjectName("moveRecipeFolderInlineEdit")
        raw_name = ""
        for f in load_recipe_folders():
            if str(f.get("id") or "") == folder_id:
                raw_name = str(f.get("name") or "").strip()
                break
        edit.setText(raw_name)
        lay.insertWidget(idx, edit, 0, Qt.AlignmentFlag.AlignVCenter)
        edit.setFixedHeight(26)
        edit.returnPressed.connect(self._commit_inline_rename)
        edit.escape_pressed.connect(self._cancel_inline_rename)
        self._rename_edit = edit
        self._rename_cb = cb
        self._rename_row = row
        self._rename_fid = folder_id
        self._rename_idx = idx

        def _focus_select() -> None:
            edit.setFocus(Qt.FocusReason.PopupFocusReason)
            edit.selectAll()

        QTimer.singleShot(0, _focus_select)

    def _commit_inline_rename(self) -> None:
        if (
            self._rename_edit is None
            or self._rename_fid is None
            or self._rename_cb is None
            or self._rename_row is None
        ):
            return
        name = self._rename_edit.text().strip()
        if not name:
            self._cancel_inline_rename()
            return
        try:
            rename_recipe_folder(self._rename_fid, name)
        except ValueError as e:
            if str(e) == ERR_DUPLICATE_RECIPE_FOLDER_NAME:
                self._toast_warning_or_tooltip(str(e), self._rename_edit)
                self._cancel_inline_rename()
                return
            self._toast_warning_or_tooltip(str(e), self._rename_edit)
            re_edit = self._rename_edit

            def _refocus() -> None:
                if re_edit is not None and self._rename_edit is re_edit:
                    re_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                    re_edit.selectAll()

            QTimer.singleShot(0, _refocus)
            return
        lay = self._rename_row.layout()
        QApplication.sendEvent(self._rename_edit, QEvent(QEvent.Type.Leave))
        lay.removeWidget(self._rename_edit)
        self._rename_edit.deleteLater()
        lay.insertWidget(self._rename_idx, self._rename_cb, 0, Qt.AlignmentFlag.AlignVCenter)
        stats = get_recipe_folder_stats()
        by_f = stats.get("by_folder") or {}
        cnt = int(by_f.get(self._rename_fid, 0)) if isinstance(by_f, dict) else 0
        label = f"{name} ({cnt})"
        self._rename_cb.setText(label)
        self._rename_cb.show()
        self._clear_rename_refs()
        self._schedule_sync_folder_list_hover()

    def _on_new_folder_clicked(self) -> None:
        if self._rename_edit is not None:
            self._cancel_inline_rename()
        if (
            self._pending_new_host is not None
            and self._inner_lay.indexOf(self._pending_new_host) >= 0
            and self._pending_new_edit is not None
        ):

            def _again() -> None:
                self._pending_new_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                self._pending_new_edit.selectAll()

            QTimer.singleShot(0, _again)
            return

        self._remove_pending_new_folder_row()
        host = QWidget(self._inner)
        host.setObjectName("moveRecipeFolderPickRow")
        host.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        h = QHBoxLayout(host)
        h.setContentsMargins(2, 0, 2, 0)
        h.setSpacing(8)
        edit = _EscapeLineEdit(host, blur_dialog=self, blur_role="pending_new")
        edit.setObjectName("moveRecipeFolderInlineEdit")
        edit.setFixedHeight(26)
        edit.setText(self._default_new_folder_label())
        h.addWidget(edit, 1, Qt.AlignmentFlag.AlignVCenter)
        edit.returnPressed.connect(self._commit_pending_new_folder)
        edit.escape_pressed.connect(self._remove_pending_new_folder_row)
        self._inner_lay.addWidget(host)
        self._pending_new_host = host
        self._pending_new_edit = edit

        def _scroll_focus_select() -> None:
            QApplication.processEvents()
            self._scroll_folder_list_to_bottom()
            edit.setFocus(Qt.FocusReason.PopupFocusReason)
            edit.selectAll()

        QTimer.singleShot(0, _scroll_focus_select)

    def _commit_pending_new_folder(self) -> None:
        if self._pending_new_edit is None:
            return
        raw = self._pending_new_edit.text()
        name = (raw or "").strip()
        if not name:
            self._remove_pending_new_folder_row()
            return
        try:
            rid = create_recipe_folder(name)
        except ValueError as e:
            if str(e) == ERR_DUPLICATE_RECIPE_FOLDER_NAME:
                self._toast_warning_or_tooltip(str(e), self._pending_new_edit)
                self._remove_pending_new_folder_row()
                return
            self._toast_warning_or_tooltip(
                str(e),
                self._pending_new_edit,
            )
            return
        self._remove_pending_new_folder_row()
        rid_final = rid

        def _deferred_rebuild() -> None:
            self._rebuild_checkboxes(
                build_all_recipe_folder_pick_targets(),
                preselect_folder_id=rid_final,
            )

        QTimer.singleShot(0, _deferred_rebuild)

    def _rebuild_checkboxes(
        self,
        targets: list[tuple[str | None, str]],
        *,
        preselect_folder_id: str | None = None,
        preselect_uncategorized: bool = False,
    ) -> None:
        self._reset_transient_ui()
        self._pick_group.setExclusive(False)
        for b in list(self._pick_group.buttons()):
            self._pick_group.removeButton(b)
        while (item := self._inner_lay.takeAt(0)) is not None:
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._pick_group.setExclusive(True)

        for fid, name in targets:
            cb = QCheckBox(name)
            cb.setObjectName("moveRecipeFolderCheck")
            cb.setTristate(False)
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.setProperty(
                "move_fid",
                _ROLE_UNCAT if fid is None else fid,
            )
            self._pick_group.addButton(cb)
            cb.toggled.connect(lambda _checked: self._sync_ok_enabled())
            rename_id = str(fid) if fid is not None else None
            row = _FolderPickRow(
                cb,
                self._inner,
                rename_folder_id=rename_id,
                dialog=self,
            )
            self._inner_lay.addWidget(row)

        if preselect_uncategorized:
            for b in self._pick_group.buttons():
                if b.property("move_fid") == _ROLE_UNCAT:
                    b.setChecked(True)
                    break
        elif preselect_folder_id is not None:
            pid = str(preselect_folder_id)
            for b in self._pick_group.buttons():
                if str(b.property("move_fid")) == pid:
                    b.setChecked(True)
                    break
        self._sync_ok_enabled()
        self._schedule_sync_folder_list_hover()
        # 整页 takeAt+deleteLater 重建后滚动条会回到顶部；新建文件夹失焦提交后会 preselect 新 id，须把列表滚回选中行
        if preselect_folder_id is not None:

            def _scroll_preselected_row_visible() -> None:
                if not self.isVisible():
                    return
                QApplication.processEvents()
                btn = self._pick_group.checkedButton()
                if btn is None:
                    self._scroll_folder_list_to_bottom()
                    return
                row = btn.parentWidget()
                if row is not None:
                    self._folder_scroll.ensureWidgetVisible(row, 12, 12)
                else:
                    self._scroll_folder_list_to_bottom()

            QTimer.singleShot(0, _scroll_preselected_row_visible)

    def showEvent(self, event) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
            app.installEventFilter(self)

        super().showEvent(event)

        apply_frameless_modal_geometry(self, self.parentWidget())

        self._reset_transient_ui()
        self._clear_checks()
        self._sync_ok_enabled()
        if self._recipe_title_edit is not None:

            def _focus_recipe_title() -> None:
                ed = self._recipe_title_edit
                if ed is not None and self.isVisible():
                    ed.setFocus(Qt.FocusReason.PopupFocusReason)
                    ed.selectAll()

            QTimer.singleShot(0, _focus_recipe_title)
        else:
            btns = self._pick_group.buttons()
            if btns:
                btns[0].setFocus()

    def _clear_checks(self) -> None:
        self._pick_group.setExclusive(False)
        for b in self._pick_group.buttons():
            b.setChecked(False)
        self._pick_group.setExclusive(True)

    def _sync_ok_enabled(self) -> None:
        has = self._pick_group.checkedButton() is not None
        self._ok_btn.setEnabled(has)
        self._ok_btn.setDefault(has)

    def _on_accept_clicked(self) -> None:
        btn = self._pick_group.checkedButton()
        if btn is None:
            return
        raw = btn.property("move_fid")
        if raw == _ROLE_UNCAT:
            self._chosen_folder_id = None
        else:
            self._chosen_folder_id = str(raw)
        self.accept()

    def chosen_folder_id(self) -> str | None:
        """与 ``move_recipes_to_folder`` 一致：None 表示未分类。仅在 ``Accepted`` 后有效。"""
        return self._chosen_folder_id

    def chosen_recipe_title(self) -> str | None:
        """保存配方时：若构造时传入 ``recipe_name_default`` 则返回输入框 strip 后文本（可空）；否则为 None。"""
        ed = self._recipe_title_edit
        if ed is None:
            return None
        return ed.text().strip()

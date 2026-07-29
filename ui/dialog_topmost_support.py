"""主窗用 Win32 TOPMOST 置顶时，子弹窗可能压在主窗下面；在 Show 时把弹窗同步到 TOPMOST。"""
from __future__ import annotations

from PySide6.QtCore import QObject, QEvent, Qt, QTimer
from PySide6.QtWidgets import QDialog, QMainWindow, QWidget

from ui.native_topmost import is_window_native_topmost_win32, try_set_topmost_win32


def pin_frameless_dialog_win32_minimum_size(dlg: QDialog) -> None:
    """在首次 show 之前调用，使 Win32 MINMAXINFO.minTrack 与布局一致。

    无此步骤时 Qt 可能先以约 100×30 建 QWidgetWindow，再与真实 minimumSize 冲突，
    触发 QWindowsWindow::setGeometry 与 mintrack 警告。
    """
    dlg.adjustSize()
    lay = dlg.layout()
    hint = dlg.minimumSizeHint()
    if lay is not None:
        sh = lay.sizeHint()
        if sh.isValid():
            hint = hint.expandedTo(sh)
    hint = hint.expandedTo(dlg.minimumSize())
    if hint.width() >= 32 and hint.height() >= 32:
        dlg.setMinimumSize(hint)


def _geometry_anchor_window_for_modal(parent: QWidget | None) -> QWidget | None:
    """蒙层铺满的参照窗：链上若有 QMainWindow 则用其几何（含设置等小窗上再弹 Confirm 时）。"""
    if parent is None:
        return None
    p: QWidget | None = parent
    while p is not None:
        if isinstance(p, QMainWindow):
            return p
        p = p.parentWidget()
    top = parent.window()
    if top is not None and top.isWindow():
        return top
    return None


def apply_frameless_modal_geometry(dlg: QDialog, parent: QWidget | None) -> None:
    """无框蒙层弹窗：仅当主窗可作为参照时铺满主窗（保持原有遮罩样式）；否则关闭弹窗随主窗收起。

    不再改用「屏幕居中小窗」，避免与原有全窗口蒙层视觉不一致。
    """
    win = _geometry_anchor_window_for_modal(parent)
    if (
        win is not None
        and win.isWindow()
        and win.isVisible()
        and not win.isMinimized()
    ):
        fg = win.frameGeometry()
        # 布局尚未提交或父窗瞬时几何过小时，frame 可能小于 QDialog 的 minimumSize，
        # 会触发 QWindowsWindow::setGeometry 与 mintrack 冲突警告。
        need = dlg.minimumSize().expandedTo(dlg.minimumSizeHint())
        if need.width() > 0 and need.height() > 0:
            if fg.width() < need.width():
                fg.setWidth(need.width())
            if fg.height() < need.height():
                fg.setHeight(need.height())
        dlg.setGeometry(fg)
        return

    def _reject_later() -> None:
        try:
            if dlg.isVisible():
                dlg.reject()
        except RuntimeError:
            pass

    QTimer.singleShot(0, _reject_later)


def _top_level_effective_always_on_top(top) -> bool:
    if top is None:
        return False
    if is_window_native_topmost_win32(top):
        return True
    return bool(top.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)


def _ensure_dialog_above_topmost_main(dlg: QDialog) -> None:
    p = dlg.parentWidget()
    if p is None:
        return
    top = p.window()
    if top is None or top is dlg:
        return
    if not _top_level_effective_always_on_top(top):
        return
    try:
        dlg.winId()
    except Exception:
        return
    try_set_topmost_win32(dlg, True)


class _DialogTopmostOnShowFilter(QObject):
    def __init__(self, dlg: QDialog):
        super().__init__(dlg)

    def eventFilter(self, obj, event):
        dlg = self.parent()
        if isinstance(dlg, QDialog) and obj is dlg and event.type() == QEvent.Type.Show:
            _ensure_dialog_above_topmost_main(dlg)
        return False


def install_dialog_topmost_follow_parent(dlg: QDialog) -> None:
    """各 QDialog.__init__ 末尾调用一次即可。"""
    pin_frameless_dialog_win32_minimum_size(dlg)
    dlg.installEventFilter(_DialogTopmostOnShowFilter(dlg))

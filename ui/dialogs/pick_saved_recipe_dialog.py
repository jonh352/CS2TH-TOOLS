"""材料采集：从配方管理导入——先选文件夹，再选该文件夹内的配方。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.saved_recipes import list_saved_recipes, load_recipe_folders
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.modal_shell import (
    MODAL_WIDTH_MD,
    add_modal_footer_buttons,
    build_frameless_modal_content,
    wire_overlay_dismiss,
)
from ui.pages.recipe_manage import _display_row_title, _format_saved_at_local


def _recipe_label(payload: dict[str, Any]) -> str:
    title = _display_row_title(payload)
    saved = _format_saved_at_local(str(payload.get("saved_at") or ""))
    if saved:
        return f"{title} · {saved}"
    return title


def _group_nonempty_folders(
    entries: list[tuple[Path, dict[str, Any]]],
) -> list[tuple[str | None, str, list[tuple[Path, dict[str, Any]]]]]:
    """仅含有配方的文件夹；(folder_id, 显示名, entries)。未分类 folder_id 为 None。"""
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
    for folder in load_recipe_folders():
        fid = str(folder.get("id") or "")
        if not fid:
            continue
        lst = by_fid.get(fid)
        if not lst:
            continue
        known.add(fid)
        name = str(folder.get("name") or "").strip() or "文件夹"
        groups.append((fid, f"{name} ({len(lst)})", lst))
    for fid in sorted(by_fid.keys() - known):
        lst = by_fid[fid]
        groups.append((fid, f"其他 ({len(lst)})", lst))
    return groups


class PickSavedRecipeDialog(QDialog):
    """两级选择：文件夹 → 配方文件。"""

    def __init__(
        self,
        parent: QWidget | None,
        entries: list[tuple[Path, dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("从配方管理导入")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        raw = entries if entries is not None else list_saved_recipes()
        self._groups = _group_nonempty_folders(raw)
        self._chosen_payload: dict[str, Any] | None = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        overlay, box, layout, close_btn = build_frameless_modal_content(
            self,
            "从配方管理导入",
            "先选择文件夹，再选择其中的配方。",
            box_width=MODAL_WIDTH_MD,
            message_object_name="alertDialogMessage",
        )
        main_layout.addWidget(overlay)
        close_btn.clicked.connect(self.reject)

        folder_label = QLabel("文件夹")
        folder_label.setObjectName("loginFormLabel")
        layout.addWidget(folder_label)
        self._folder_combo = QComboBox()
        self._folder_combo.setObjectName("loginInput")
        for _fid, name, _entries in self._groups:
            self._folder_combo.addItem(name)
        self._folder_combo.currentIndexChanged.connect(self._on_folder_changed)
        layout.addWidget(self._folder_combo)

        recipe_label = QLabel("配方")
        recipe_label.setObjectName("loginFormLabel")
        layout.addWidget(recipe_label)
        self._recipe_combo = QComboBox()
        self._recipe_combo.setObjectName("loginInput")
        layout.addWidget(self._recipe_combo)

        _cancel, self._ok_btn = add_modal_footer_buttons(
            layout,
            ok_text="导入",
            on_cancel=self.reject,
            on_ok=self._accept_selection,
        )

        wire_overlay_dismiss(overlay, box, self, accept=False)
        install_dialog_topmost_follow_parent(self)

        if self._groups:
            self._folder_combo.setCurrentIndex(0)
            self._on_folder_changed(0)
        else:
            self._ok_btn.setEnabled(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())

    def _on_folder_changed(self, index: int) -> None:
        self._recipe_combo.clear()
        if index < 0 or index >= len(self._groups):
            self._ok_btn.setEnabled(False)
            return
        _fid, _name, entries = self._groups[index]
        for _path, payload in entries:
            self._recipe_combo.addItem(_recipe_label(payload), payload)
        self._ok_btn.setEnabled(self._recipe_combo.count() > 0)

    def _accept_selection(self) -> None:
        payload = self._recipe_combo.currentData()
        if not isinstance(payload, dict):
            return
        self._chosen_payload = payload
        self.accept()

    def chosen_payload(self) -> dict[str, Any] | None:
        return self._chosen_payload


def pick_saved_recipe(
    parent: QWidget | None,
    entries: list[tuple[Path, dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """打开选择对话框；确定返回配方文件顶层 payload，取消返回 None。"""
    dlg = PickSavedRecipeDialog(parent, entries)
    if not dlg._groups:
        return None
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.chosen_payload()

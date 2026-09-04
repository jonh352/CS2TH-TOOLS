"""Progress and Steam QR-login dialog for one-click trade-ups."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.steam_tradeup import has_saved_tradeup_session
from ui.dialog_topmost_support import (
    apply_frameless_modal_geometry,
    install_dialog_topmost_follow_parent,
)
from ui.modal_shell import (
    MODAL_WIDTH_MD,
    add_modal_footer_buttons,
    build_frameless_modal_content,
)
from ui.widgets.purchase_qr_label import _qr_base_pixmap
from ui.workers.steam_tradeup import SteamTradeupWorker


class SteamTradeupDialog(QDialog):
    def __init__(self, parent: QWidget | None, plan: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("一键汰换")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._running = True
        self._irreversible = False
        self._cancel_requested = False
        self._success = False
        self._message = ""
        self._result_payload: dict[str, Any] = {}
        self._plan = dict(plan)
        self._worker: SteamTradeupWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        overlay, _box, layout, close_btn = build_frameless_modal_content(
            self,
            "一键汰换",
            "库存页的网页登录只用于读取库存；执行汰换需要 Steam 客户端游戏授权。"
            "授权会加密保存在本机，账号凭据不会上传到 CS2TH。",
            box_width=MODAL_WIDTH_MD,
            message_object_name="alertDialogMessage",
        )
        root.addWidget(overlay)
        close_btn.clicked.connect(self._request_cancel)

        self._login_controls = QWidget(self)
        login_layout = QVBoxLayout(self._login_controls)
        login_layout.setContentsMargins(0, 0, 0, 0)
        login_layout.setSpacing(12)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self._qr_mode_btn = QPushButton("二维码登录")
        self._credentials_mode_btn = QPushButton("账密令牌登录")
        self._auth_mode_group = QButtonGroup(self)
        self._auth_mode_group.setExclusive(True)
        for button in (self._qr_mode_btn, self._credentials_mode_btn):
            button.setObjectName("alchemyModeButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._auth_mode_group.addButton(button)
            mode_row.addWidget(button, 1)
        login_layout.addLayout(mode_row)

        self._credentials_panel = QFrame(self._login_controls)
        credentials_layout = QVBoxLayout(self._credentials_panel)
        credentials_layout.setContentsMargins(0, 4, 0, 0)
        credentials_layout.setSpacing(8)
        account_label = QLabel("Steam 登录账号（不是个人昵称）")
        account_label.setObjectName("loginFormLabel")
        credentials_layout.addWidget(account_label)
        self._account_edit = QLineEdit()
        self._account_edit.setObjectName("loginInput")
        self._account_edit.setPlaceholderText("请输入 Steam 登录账号")
        credentials_layout.addWidget(self._account_edit)
        password_label = QLabel("Steam 密码")
        password_label.setObjectName("loginFormLabel")
        credentials_layout.addWidget(password_label)
        self._password_edit = QLineEdit()
        self._password_edit.setObjectName("loginInput")
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("密码仅用于本机本次登录，不会保存")
        credentials_layout.addWidget(self._password_edit)
        guard_label = QLabel("Steam Guard 令牌")
        guard_label.setObjectName("loginFormLabel")
        credentials_layout.addWidget(guard_label)
        self._guard_edit = QLineEdit()
        self._guard_edit.setObjectName("loginInput")
        self._guard_edit.setPlaceholderText("输入手机令牌或邮件验证码")
        self._guard_edit.setMaxLength(10)
        credentials_layout.addWidget(self._guard_edit)
        login_layout.addWidget(self._credentials_panel)
        layout.addWidget(self._login_controls)

        self._qr = QLabel()
        self._qr.setObjectName("steamTradeupQr")
        self._qr.setFixedSize(220, 220)
        self._qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr.setText("正在准备 Steam 授权…")
        self._qr.hide()
        layout.addWidget(self._qr, 0, Qt.AlignmentFlag.AlignHCenter)

        self._status = QLabel("请选择 Steam 登录方式")
        self._status.setObjectName("alertDialogMessage")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

        material_count = len(plan.get("asset_ids") or [])
        self._safety = QLabel(
            f"执行前会再次核对指定的 {material_count} 件材料。"
            "开始汰换后不可取消，请勿同时在游戏内操作这些物品。"
        )
        self._safety.setObjectName("alchemyStep1Hint")
        self._safety.setWordWrap(True)
        layout.addWidget(self._safety)

        self._cancel_btn, self._progress_btn = add_modal_footer_buttons(
            layout,
            cancel_text="取消",
            ok_text="开始授权",
            on_cancel=self._request_cancel,
            on_ok=self._start_authorization,
        )

        install_dialog_topmost_follow_parent(self)
        self._qr_mode_btn.clicked.connect(lambda: self._set_auth_mode("qr"))
        self._credentials_mode_btn.clicked.connect(
            lambda: self._set_auth_mode("credentials")
        )
        self._set_auth_mode("qr")
        profile_id = str(plan.get("profile_id") or "").strip()
        if has_saved_tradeup_session(profile_id):
            self._login_controls.hide()
            self._status.setText("正在使用已保存的 Steam 游戏授权…")
            self._progress_btn.setText("处理中…")
            self._progress_btn.setEnabled(False)
            QTimer.singleShot(0, lambda: self._start_worker({"mode": "saved"}))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_frameless_modal_geometry(self, self.parentWidget())

    def closeEvent(self, event) -> None:
        if self._running:
            self._request_cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _show_qr(self, url: str) -> None:
        if not url:
            return
        pixmap = _qr_base_pixmap(url, 210)
        self._qr.setPixmap(pixmap)
        self._qr.show()

    def _set_auth_mode(self, mode: str) -> None:
        credentials = mode == "credentials"
        self._qr_mode_btn.setChecked(not credentials)
        self._credentials_mode_btn.setChecked(credentials)
        self._credentials_panel.setVisible(credentials)
        self._status.setText(
            "账号、密码和令牌仅发送给本机 Steam 授权组件，不会保存或上传"
            if credentials
            else "开始授权后，请使用 Steam 手机 App 扫描二维码并确认"
        )

    def _start_authorization(self) -> None:
        if self._worker is not None:
            return
        if self._credentials_mode_btn.isChecked():
            account_name = self._account_edit.text().strip()
            password = self._password_edit.text()
            guard_code = self._guard_edit.text().strip()
            if not account_name or not password or not guard_code:
                self._status.setText("请输入 Steam 登录账号、密码和 Steam Guard 令牌")
                return
            auth = {
                "mode": "credentials",
                "account_name": account_name,
                "password": password,
                "guard_code": guard_code,
            }
        else:
            auth = {"mode": "qr"}
        self._start_worker(auth)

    def _start_worker(self, auth: dict[str, str]) -> None:
        if not self._running or self._worker is not None:
            return
        self._login_controls.hide()
        self._progress_btn.setText("处理中…")
        self._progress_btn.setEnabled(False)
        self._status.setText("正在启动本地 Steam 授权组件…")
        self._worker = SteamTradeupWorker(
            self._plan,
            self,
            auth=auth,
        )
        self._password_edit.clear()
        self._guard_edit.clear()
        self._worker.status.connect(self._status.setText)
        self._worker.qr_ready.connect(self._show_qr)
        self._worker.irreversible.connect(self._on_irreversible)
        self._worker.completed.connect(self._on_completed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_irreversible(self) -> None:
        self._irreversible = True
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("不可取消")

    def _request_cancel(self) -> None:
        if not self._running:
            self.reject()
            return
        if self._irreversible:
            self._status.setText("汰换请求已经发出，正在等待 Steam 返回结果，请勿关闭")
            return
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_btn.setEnabled(False)
        self._status.setText("正在取消…")
        if self._worker is None:
            self._running = False
            self.reject()
            return
        self._worker.cancel()

    def _on_completed(self, success: bool, message: str, payload: object) -> None:
        self._success = success
        self._message = message
        self._result_payload = dict(payload) if isinstance(payload, dict) else {}
        error_code = str(self._result_payload.get("error_code") or "").strip()
        displayed = message
        if error_code:
            displayed = f"{message}\n错误代码：{error_code}"
        self._status.setText(displayed)

    def _on_worker_finished(self) -> None:
        self._running = False
        if self._success:
            self._progress_btn.setText("已完成")
            QTimer.singleShot(120, self.accept)
        else:
            self._cancel_btn.setEnabled(True)
            self._cancel_btn.setText("关闭")
            self._progress_btn.setText(
                "请核对库存"
                if self._result_payload.get("craft_succeeded")
                or self._result_payload.get("uncertain")
                else "未执行"
            )

    def result_payload(self) -> dict[str, Any]:
        return dict(self._result_payload)

    def result_message(self) -> str:
        return self._message

"""登录 / 注册对话框。连接与认证在后台线程进行，界面不会卡住。"""
import os
import ssl

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..errors import AuthError
from ..net import FTPClient
from .common import run_in_background

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_ca_path() -> str:
    for base in (os.getcwd(), PROJECT_ROOT):
        p = os.path.join(base, "certs", "ca-cert.pem")
        if os.path.isfile(p):
            return p
    return os.path.join(PROJECT_ROOT, "certs", "ca-cert.pem")


def friendly_error(e: BaseException) -> str:
    if isinstance(e, AuthError):
        return e.message
    if isinstance(e, FileNotFoundError):
        return f"找不到文件：{e.filename or e}"
    if isinstance(e, ssl.SSLError):
        return f"证书文件无效：{e}"
    if isinstance(e, TimeoutError):
        return "服务端响应超时"
    return str(e) or type(e).__name__


class LoginDialog(QDialog):
    def __init__(self, parent=None, settings: QSettings = None):
        super().__init__(parent)
        self.setWindowTitle("安全文件传输 - 连接服务器")
        self.setMinimumWidth(460)
        self.settings = settings or QSettings("SecureFTP", "Client")
        self.client = None
        self.server_label = ""
        self._cancelled = False
        S = self.settings

        self.host = QLineEdit(S.value("host", "127.0.0.1"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(S.value("port", 9443)))
        self.ca = QLineEdit(S.value("ca", "") or default_ca_path())
        self.cert = QLineEdit(S.value("cert", ""))
        self.key = QLineEdit(S.value("key", ""))
        self.user = QLineEdit(S.value("user", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_box = QCheckBox("注册新用户（首次使用请勾选）")
        for w, ph in ((self.cert, "可选：服务端开启双向认证时需要"), (self.key, "可选：客户端私钥")):
            w.setPlaceholderText(ph)

        form = QFormLayout()
        form.addRow("服务器地址", self.host)
        form.addRow("端口", self.port)
        form.addRow("用户名", self.user)
        form.addRow("密码", self.password)
        form.addRow("", self.register_box)

        adv = QGroupBox("证书设置")
        adv.setCheckable(True)
        adv.setChecked(False)
        adv_form = QFormLayout()
        adv_form.addRow("CA 证书", self._with_browse(self.ca))
        adv_form.addRow("客户端证书", self._with_browse(self.cert))
        adv_form.addRow("客户端私钥", self._with_browse(self.key))
        self._adv_body = QWidget()
        self._adv_body.setLayout(adv_form)
        self._adv_body.setVisible(False)
        adv.toggled.connect(self._adv_body.setVisible)
        adv_lay = QVBoxLayout(adv)
        adv_lay.addWidget(self._adv_body)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        self.ok_btn = QPushButton("连接")
        self.ok_btn.setDefault(True)
        self.quit_btn = QPushButton("退出")
        self.ok_btn.clicked.connect(self._connect_clicked)
        self.quit_btn.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(self.quit_btn)
        btns.addWidget(self.ok_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(adv)
        lay.addWidget(self.error)
        lay.addLayout(btns)
        (self.password if self.user.text() else self.user).setFocus()

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        b = QPushButton("浏览…")
        b.clicked.connect(lambda: self._browse(edit))
        h.addWidget(edit, 1)
        h.addWidget(b)
        return box

    def _browse(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", edit.text() or PROJECT_ROOT,
                                              "证书/密钥 (*.pem *.crt *.cer *.key);;所有文件 (*)")
        if path:
            edit.setText(path)

    # ---- 连接 ----
    def _set_busy(self, busy: bool, msg: str = ""):
        for w in (self.host, self.port, self.user, self.password, self.register_box,
                  self.ok_btn, self.ca, self.cert, self.key):
            w.setEnabled(not busy)
        self.error.setStyleSheet("color: #555;" if busy else "color: #c0392b;")
        self.error.setText(msg)

    def _connect_clicked(self):
        host, port = self.host.text().strip(), self.port.value()
        user, pwd = self.user.text().strip(), self.password.text()
        ca, cert, key = self.ca.text().strip(), self.cert.text().strip(), self.key.text().strip()
        reg = self.register_box.isChecked()
        if not host or not user or not pwd:
            self.error.setText("请填写服务器地址、用户名和密码")
            return
        self._set_busy(True, "正在连接…")

        def work():
            client = FTPClient(host, port, ca, cert or None, key or None)
            try:
                client.connect()
                if reg:
                    client.register(user, pwd)
                client.login(user, pwd)
            except BaseException:
                client.close()
                raise
            return client

        def ok(client):
            if self._cancelled:             # 用户在连接期间已关闭对话框
                client.close()
                return
            self.client = client
            self.server_label = f"{user}@{host}:{port}"
            S = self.settings
            for k, v in (("host", host), ("port", port), ("ca", ca), ("cert", cert),
                         ("key", key), ("user", user)):
                S.setValue(k, v)                # 不保存密码
            self.accept()

        def err(e):
            if not self._cancelled:
                self._set_busy(False)
                self.error.setText(friendly_error(e))

        run_in_background(work, ok, err)

    def reject(self):
        self._cancelled = True
        super().reject()
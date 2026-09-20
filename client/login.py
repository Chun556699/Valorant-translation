"""登录/注册/游客对话框。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QLabel, QTabWidget, QWidget,
                               QFormLayout, QMessageBox)


class LoginDialog(QDialog):
    """成功登录后 result=1，self.user_info 含 token/权益。"""

    def __init__(self, api, cfg, parent=None):
        super().__init__(parent)
        self.api = api
        self.cfg = cfg
        self.user_info = None
        self.setWindowTitle("VTranslate 登录")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(360)
        self.setModal(True)

        lay = QVBoxLayout(self)
        title = QLabel("VTranslate · 瓦罗兰特实时语音翻译")
        title.setStyleSheet("font-size:16px;font-weight:bold;color:#3a8fd9;"
                            "padding:6px 0")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        tabs = QTabWidget()
        lay.addWidget(tabs)

        # ---- 登录 ----
        login_w = QWidget()
        lf = QFormLayout(login_w)
        self.li_user = QLineEdit(self.cfg["username"])
        self.li_pwd = QLineEdit()
        self.li_pwd.setEchoMode(QLineEdit.Password)
        lf.addRow("用户名", self.li_user)
        lf.addRow("密码", self.li_pwd)
        login_btn = QPushButton("登 录")
        login_btn.setStyleSheet("background:#3a8fd9;color:#fff;padding:8px;"
                                "border-radius:4px;font-size:14px")
        login_btn.clicked.connect(self._do_login)
        lf.addRow(login_btn)
        tabs.addTab(login_w, "登录")

        # ---- 注册 ----
        reg_w = QWidget()
        rf = QFormLayout(reg_w)
        self.rg_user = QLineEdit()
        self.rg_pwd = QLineEdit()
        self.rg_pwd.setEchoMode(QLineEdit.Password)
        self.rg_ref = QLineEdit()
        self.rg_ref.setPlaceholderText("选填，双方都有奖励")
        rf.addRow("用户名", self.rg_user)
        rf.addRow("密码", self.rg_pwd)
        rf.addRow("推广码", self.rg_ref)
        reg_btn = QPushButton("注 册（送免费额度）")
        reg_btn.setStyleSheet("background:#2ea86e;color:#fff;padding:8px;"
                              "border-radius:4px;font-size:14px")
        reg_btn.clicked.connect(self._do_register)
        rf.addRow(reg_btn)
        tabs.addTab(reg_w, "注册")

        # ---- 游客 ----
        guest_btn = QPushButton("游客模式（无需账号，本机试用）")
        guest_btn.clicked.connect(self._do_guest)
        lay.addWidget(guest_btn)

        self.msg = QLabel("")
        self.msg.setStyleSheet("color:#d00")
        self.msg.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.msg)

        self.li_pwd.returnPressed.connect(self._do_login)

    def _finish(self, resp):
        if resp.get("code") != 1:
            self.msg.setText(resp.get("msg", "失败"))
            return
        self.user_info = resp
        self.api.token = resp["token"]
        self.cfg["token"] = resp["token"]
        self.cfg["username"] = resp.get("username", "")
        self.cfg.save()
        self.accept()

    def _do_login(self):
        try:
            self._finish(self.api.auth_login(self.li_user.text().strip(),
                                             self.li_pwd.text()))
        except Exception as e:
            self.msg.setText(f"网络错误: {e}")

    def _do_register(self):
        try:
            self._finish(self.api.auth_register(
                self.rg_user.text().strip(), self.rg_pwd.text(),
                ref=self.rg_ref.text().strip(),
                device_id=self.cfg["device_id"]))
        except Exception as e:
            self.msg.setText(f"网络错误: {e}")

    def _do_guest(self):
        try:
            self._finish(self.api.auth_guest(self.cfg["device_id"]))
        except Exception as e:
            self.msg.setText(f"网络错误: {e}")

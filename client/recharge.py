"""充值窗口：选商品 -> 下单 -> 展示二维码/收银台链接 -> 轮询到账。"""
import io
import logging
import webbrowser

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QComboBox, QRadioButton, QButtonGroup)

log = logging.getLogger("recharge")


class RechargeDialog(QDialog):
    def __init__(self, api, on_paid, parent=None):
        super().__init__(parent)
        self.api = api
        self.on_paid = on_paid
        self.out_trade_no = None
        self.setWindowTitle("充值")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(340)

        lay = QVBoxLayout(self)
        self.product_combo = QComboBox()
        try:
            prods = self.api.products().get("products", [])
        except Exception as e:
            prods = []
            lay.addWidget(QLabel(f"获取商品失败: {e}"))
        for p in prods:
            unit = f"{p['seconds']//60}分钟" if p["kind"] == "credits" else f"{p['days']}天"
            self.product_combo.addItem(f"{p['name']}  ¥{p['money']}（{unit}）",
                                       p["code"])
        lay.addWidget(self.product_combo)

        pay_row = QHBoxLayout()
        self.pay_group = QButtonGroup(self)
        for i, (val, text) in enumerate((("alipay", "支付宝"), ("wxpay", "微信"))):
            r = QRadioButton(text)
            r.setChecked(i == 0)
            self.pay_group.addButton(r)
            r.setProperty("val", val)
            pay_row.addWidget(r)
        pay_row.addStretch(1)
        lay.addLayout(pay_row)

        create_btn = QPushButton("创建订单")
        create_btn.clicked.connect(self._create)
        lay.addWidget(create_btn)

        self.qr_lbl = QLabel("")
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setMinimumHeight(220)
        lay.addWidget(self.qr_lbl)

        row = QHBoxLayout()
        self.open_btn = QPushButton("浏览器打开收银台")
        self.open_btn.clicked.connect(self._open_page)
        self.open_btn.setEnabled(False)
        self.status_lbl = QLabel("")
        row.addWidget(self.open_btn)
        row.addWidget(self.status_lbl, 1)
        lay.addLayout(row)

        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._check_status)

    # ---- 下单 ----
    def _create(self):
        code = self.product_combo.currentData()
        pay_type = self.pay_group.checkedButton().property("val")
        try:
            resp = self.api.pay_create(code, pay_type)
        except Exception as e:
            self.status_lbl.setText(f"下单失败: {e}")
            return
        if resp.get("code") != 1:
            self.status_lbl.setText(f"下单失败: {resp.get('msg')}")
            return
        self.out_trade_no = resp["out_trade_no"]
        self._page_url = resp.get("page_url", "")
        self.open_btn.setEnabled(bool(self._page_url))
        qr_content = resp.get("qrcode") or resp.get("payurl") or self._page_url
        if qr_content:
            self._show_qr(qr_content)
            self.status_lbl.setText("请扫码支付")
        else:
            self.status_lbl.setText("请用浏览器打开收银台支付")
        self._poll.start()

    def _show_qr(self, content: str):
        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qimg = QImage.fromData(buf.getvalue())
        self.qr_lbl.setPixmap(QPixmap.fromImage(qimg).scaled(
            220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _open_page(self):
        if getattr(self, "_page_url", ""):
            webbrowser.open(self._page_url)

    # ---- 查单 ----
    def _check_status(self):
        if not self.out_trade_no:
            return
        try:
            resp = self.api.pay_status(self.out_trade_no)
        except Exception:
            return
        if resp.get("status") == "paid":
            self._poll.stop()
            self.status_lbl.setText("支付成功，已到账 ✓")
            self.on_paid(resp)

    def closeEvent(self, e):
        self._poll.stop()
        super().closeEvent(e)

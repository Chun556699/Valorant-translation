"""Z-Pay 支付接入（彩虹易支付 V1 协议，MD5 签名）。

接口：
  submit.php  页面跳转支付（拼 URL 让浏览器打开收银台）
  mapi.php    API 下单（返回 payurl/qrcode，客户端内嵌二维码用）
  api.php     商户查询（act=order 查单，pid+key 鉴权，不签名）
  notify      异步回调（GET/POST，需验签）
"""
import hashlib
import logging
from urllib.parse import urlencode

import httpx

log = logging.getLogger("epay")


def epay_sign(params: dict, key: str) -> str:
    """易支付 V1 签名：非空参数按 key 排序，k=v& 拼接后直接接密钥，MD5。"""
    items = sorted(
        (k, str(v)) for k, v in params.items()
        if k not in ("sign", "sign_type") and v is not None and str(v) != ""
    )
    raw = "&".join(f"{k}={v}" for k, v in items) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class EPayClient:
    def __init__(self, base_url: str, pid: str, key: str):
        self.base_url = base_url.rstrip("/")
        self.pid = pid
        self.key = key

    def _signed(self, params: dict) -> dict:
        params = dict(params)
        params["pid"] = self.pid
        params["sign"] = epay_sign(params, self.key)
        params["sign_type"] = "MD5"
        return params

    def build_page_pay_url(self, *, name, money, out_trade_no, pay_type,
                           notify_url, return_url="", param="", clientip="") -> str:
        """生成收银台跳转 URL（GET submit.php）。"""
        params = self._signed({
            "name": name, "money": money, "out_trade_no": out_trade_no,
            "notify_url": notify_url, "return_url": return_url,
            "param": param, "type": pay_type, "clientip": clientip,
        })
        return f"{self.base_url}/submit.php?{urlencode(params)}"

    async def create_api_pay(self, http: httpx.AsyncClient, *, name, money,
                             out_trade_no, pay_type, notify_url, param="",
                             clientip="127.0.0.1") -> dict:
        """mapi.php 下单，返回 {code,msg,payurl,qrcode,trade_no,...}。"""
        params = self._signed({
            "name": name, "money": money, "out_trade_no": out_trade_no,
            "notify_url": notify_url, "param": param, "type": pay_type,
            "clientip": clientip,
        })
        r = await http.post(f"{self.base_url}/mapi.php", data=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        log.info("mapi 下单返回: %s", data)
        return data

    def verify_notify(self, params: dict) -> bool:
        """验签异步回调参数。trade_status 判断在调用方做。"""
        sign = params.get("sign", "")
        return bool(sign) and epay_sign(params, self.key) == sign.lower()

    async def query_order(self, http: httpx.AsyncClient, out_trade_no: str) -> dict:
        """api.php 查单（回调丢失时兜底）。返回含 status: 1=已支付。"""
        r = await http.get(
            f"{self.base_url}/api.php",
            params={"act": "order", "pid": self.pid, "key": self.key,
                    "out_trade_no": out_trade_no},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

"""服务端配置：全部走环境变量，见 .env.example"""
import os
import json


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---- DashScope / Qwen LiveTranslate ----
DASHSCOPE_API_KEY = _env("DASHSCOPE_API_KEY")
# 默认走阿里云百炼国内端点；千问AI平台端点为 wss://maas.qianwenaiapi.com/api-ws/v1/realtime
DASHSCOPE_WS_URL = _env(
    "DASHSCOPE_WS_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
)
TRANSLATE_MODEL = _env("TRANSLATE_MODEL", "qwen3.8-livetranslate-flash-realtime")
# 可选：JSON 字符串，合并进 session.update 的 session 字段，用于不打码调参
DASH_SESSION_EXTRA = json.loads(_env("DASH_SESSION_EXTRA_JSON", "{}") or "{}")

# ---- Z-Pay（易支付 V1 协议）----
ZPAY_BASE_URL = _env("ZPAY_BASE_URL", "https://z-pay.cn").rstrip("/")
ZPAY_PID = _env("ZPAY_PID")          # 商户号
ZPAY_KEY = _env("ZPAY_KEY")          # 商户密钥（MD5 通讯密钥）
# 回调地址：必须是公网可达地址，例如 https://api.example.com/api/pay/notify
ZPAY_NOTIFY_URL = _env("ZPAY_NOTIFY_URL")
# 支付完成后浏览器跳转地址（可选）
ZPAY_RETURN_URL = _env("ZPAY_RETURN_URL")

# ---- 服务 ----
HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", "8765"))
DB_PATH = _env("DB_PATH", os.path.join(os.path.dirname(__file__), "billing.db"))
ADMIN_TOKEN = _env("ADMIN_TOKEN")    # 管理接口令牌，发卡/测试用
# MOCK_UPSTREAM=1 时不连 DashScope，用假数据回显，便于无 Key 调试客户端
MOCK_UPSTREAM = _env("MOCK_UPSTREAM", "0") == "1"
# 免费试用秒数（新设备注册赠送，0 关闭）
TRIAL_SECONDS = int(_env("TRIAL_SECONDS", "300"))

# ---- 商品表 ----
# kind=credits 按点数（秒）充值；kind=sub 订阅（天）
PRODUCTS = {
    "credits_30min": {"kind": "credits", "seconds": 1800,  "money": "9.90",  "name": "30分钟翻译点数"},
    "credits_120min": {"kind": "credits", "seconds": 7200, "money": "29.90", "name": "120分钟翻译点数"},
    "credits_600min": {"kind": "credits", "seconds": 36000, "money": "99.00", "name": "600分钟翻译点数"},
    "sub_month": {"kind": "sub", "days": 30, "money": "59.00", "name": "包月订阅（不限量）"},
    "sub_season": {"kind": "sub", "days": 90, "money": "149.00", "name": "包季订阅（不限量）"},
}

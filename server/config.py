"""服务端配置：全部走环境变量，见 .env.example"""
import os
import json


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---- DashScope / Qwen LiveTranslate ----
DASHSCOPE_API_KEY = _env("DASHSCOPE_API_KEY")
# 通用端点；业务空间专属域名可通过环境变量覆盖
DASHSCOPE_WS_URL = _env(
    "DASHSCOPE_WS_URL",
    "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
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
# TRUST_PROXY=1 时限流按 X-Forwarded-For 首跳识别客户端（仅部署在 Nginx/CF 后开启，
# 且需同时以 uvicorn --proxy-headers 启动，否则客户端可伪造该头绕过限流）
TRUST_PROXY = _env("TRUST_PROXY", "0") == "1"

# ---- 权益与定价 ----
# 新用户免费额度（秒 + 有效期天数，0 关闭试用）
TRIAL_SECONDS = int(_env("TRIAL_SECONDS", "300"))
TRIAL_DAYS = int(_env("TRIAL_DAYS", "7"))
# 同一 IP 每 24h 最多新建游客账号数（防刷免费额度；轮换已有游客不占额度）
GUEST_GRANTS_PER_IP_PER_DAY = int(_env("GUEST_GRANTS_PER_IP_PER_DAY", "10"))
# 余额按量计费单价：分/分钟（瓦语通模式：余额按翻译实际时长扣钱）
PRICE_FEN_PER_MINUTE = int(_env("PRICE_FEN_PER_MINUTE", "30"))
# 推广返利：被邀请人实付金额的百分比返到邀请人余额（分）
REF_BONUS_PERCENT = int(_env("REF_BONUS_PERCENT", "10"))
# 公告栏文本（客户端顶部横幅，空则不显示）
ANNOUNCEMENT = _env("ANNOUNCEMENT", "")
# 可供选择的播报音色（客户端下拉）
VOICES = ["Cherry", "Tina", "Ethan", "Serena", "Kiki", "Anna"]

# ---- 商品表 ----
# kind=money 余额充值（分）；kind=card 时长卡（秒）；kind=sub 订阅（天）
PRODUCTS = {
    "money_10":  {"kind": "money", "fen": 1000,  "money": "10.00", "name": "余额充值 ¥10"},
    "money_30":  {"kind": "money", "fen": 3000,  "money": "30.00", "name": "余额充值 ¥30"},
    "money_50":  {"kind": "money", "fen": 5000,  "money": "50.00", "name": "余额充值 ¥50"},
    "card_60min":  {"kind": "card", "seconds": 3600,  "money": "9.90",  "name": "时长卡 60分钟"},
    "card_300min": {"kind": "card", "seconds": 18000, "money": "39.90", "name": "时长卡 300分钟"},
    "sub_month":  {"kind": "sub", "days": 30, "money": "59.00",  "name": "包月订阅（不限量）"},
    "sub_season": {"kind": "sub", "days": 90, "money": "149.00", "name": "包季订阅（不限量）"},
}

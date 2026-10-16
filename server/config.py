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
# 客户端可自选的中转模型白名单（逗号分隔）；不在名单内的请求回落 TRANSLATE_MODEL。
# 计费按统一单价，暴露更贵模型前先评估 PRICE_FEN_PER_MINUTE 是否需要调整
ALLOWED_MODELS = [m.strip() for m in _env("ALLOWED_MODELS", TRANSLATE_MODEL).split(",") if m.strip()]
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
# 队友流断句静音时长（毫秒）：越短出字越快，越长整句越完整。游戏短指令 700~900 较合适
MATE_SILENCE_MS = int(_env("MATE_SILENCE_MS", "800"))
# 开放注册 / 游客模式（可在管理后台随时切换）
ALLOW_REGISTER = _env("ALLOW_REGISTER", "1") == "1"
ALLOW_GUEST = _env("ALLOW_GUEST", "1") == "1"
# 管理后台登录会话有效期（小时）
ADMIN_SESSION_HOURS = int(_env("ADMIN_SESSION_HOURS", "12"))

# 管理后台可在线修改的运行时参数：key -> (类型, 取值范围校验)
# 修改后写入 settings 表并立即覆盖本模块同名大写变量，重启后仍生效
RUNTIME = {
    "announcement": (str, lambda v: len(v) <= 500),
    "trial_seconds": (int, lambda v: 0 <= v <= 864000),
    "trial_days": (int, lambda v: 0 <= v <= 3650),
    "price_fen_per_minute": (int, lambda v: 1 <= v <= 100000),
    "ref_bonus_percent": (int, lambda v: 0 <= v <= 100),
    "guest_grants_per_ip_per_day": (int, lambda v: 0 <= v <= 100000),
    "mate_silence_ms": (int, lambda v: 200 <= v <= 6000),
    "allow_register": (bool, lambda v: True),
    "allow_guest": (bool, lambda v: True),
}


def runtime_values() -> dict:
    return {k: globals()[k.upper()] for k in RUNTIME}


def apply_runtime(key: str, raw) -> object:
    """校验并应用一个运行时参数，返回规范化后的值；非法抛 ValueError。"""
    if key not in RUNTIME:
        raise ValueError(f"未知参数 {key}")
    typ, check = RUNTIME[key]
    if typ is bool:
        val = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
    elif typ is int:
        if isinstance(raw, bool):
            raise ValueError(f"{key} 须为整数")
        val = int(raw)
    else:
        val = str(raw).strip()
    if not check(val):
        raise ValueError(f"{key} 超出允许范围")
    globals()[key.upper()] = val
    return val

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

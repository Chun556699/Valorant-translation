"""Valorant 专属术语热词表（corpus.phrases，源语言词 -> 目标语言词）。

提升特工名/地图名/枪械名/游戏术语的翻译准确性。
目标语言为 zh 时用 EN2ZH（队友英语->中文），为 en 时用 ZH2EN（自己中文->英语）。
可按需增删。
"""

# ---- 中文 -> 英文（自翻流，按住说话时生效）----
TERMS_ZH2EN = {
    # 特工（官方名 + 常见外号）
    "捷风": "Jett", "不死鸟": "Phoenix", "火男": "Phoenix",
    "猎枭": "Sova", "苏法": "Sova", "贤者": "Sage", "奶妈": "Sage",
    "蝰蛇": "Viper", "毒妹": "Viper", "零": "Cypher", "保安": "Cypher",
    "芮娜": "Reyna", "蕾娜": "Reyna", "恺宙": "Killjoy", "奇乐": "Killjoy",
    "铁臂": "Breach", "幽影": "Omen", "欧门": "Omen",
    "布史东": "Brimstone", "烟男": "Brimstone",
    "雷兹": "Raze", "炸弹妹": "Raze", "丝凯": "Skye",
    "夜露": "Yoru", "星礈": "Astra", "亚星卓": "Astra",
    "机器人": "KAY/O", "KO": "KAY/O", "尚勃勒": "Chamber",
    "霓虹": "Neon", "菲德": "Fade", "黑梦": "Fade",
    "哈珀": "Harbor", "海神": "Harbor", "盖可": "Gekko",
    "蒂罗": "Deadlock", "死锁": "Deadlock", "壹决": "Iso", "一绝": "Iso",
    "暮蝶": "Clove", "薇丝": "Vyse", "维斯": "Vyse",
    "泰霍": "Tejo", "维蕾": "Waylay",
    # 地图
    "亚海悬城": "Ascent", "意境空岛": "Ascent", "隐世修所": "Haven",
    "霓虹町": "Split", "源工重镇": "Bind", "劫境之地": "Bind",
    "极地寒港": "Icebox", "微风岛屿": "Breeze", "天漠之峡": "Fracture",
    "深海明珠": "Pearl", "莲华古城": "Lotus", "日落之城": "Sunset",
    "幽邃地窟": "Abyss",
    # 枪械
    "标配": "Classic", "制式手枪": "Classic", "短管": "Shorty",
    "狂怒": "Frenzy", "鬼魅": "Ghost", "警长": "Sheriff",
    "蜂刺": "Stinger", "骇灵": "Spectre", "重炮": "Bucky",
    "判官": "Judge", "斗牛犬": "Bulldog", "戍卫": "Bulldog",
    "守护": "Guardian", "幻影": "Phantom", "狂徒": "Vandal",
    "暴徒": "Vandal", "游侠": "Marshal", "冥驹": "Operator",
    "大狙": "Operator", "间谍": "Operator", "战神": "Ares",
    "奥丁": "Odin", "亡命徒": "Outlaw",
    # 战术/术语
    "爆能器": "Spike", "下包": "plant the Spike", "装包": "plant the Spike",
    "拆包": "defuse", "假拆": "fake defuse", "保枪": "save",
    "经济局": "eco round", "强起": "force buy", "手枪局": "pistol round",
    "全甲": "full shield", "半甲": "light shield",
    "大招": "ultimate", "开大": "use ultimate", "技能": "ability",
    "烟雾": "smoke", "给烟": "smoke", "闪光": "flash", "燃烧弹": "molly",
    "决斗者": "duelist", "先锋": "initiator", "控场": "controller",
    "哨卫": "sentinel", "排位": "ranked", "加时": "overtime",
    "赛点": "match point", "残局": "clutch", "补枪": "trade",
    "一换一": "trade", "拉枪": "peek", "干拉": "dry peek",
    "双架": "double peek", "绕后": "flank", "断后": "lurk",
    "前压": "push", "回防": "rotate", "转点": "rotate",
    "快攻": "rush", "抱团": "group up", "包点": "site",
    "A点": "A site", "B点": "B site", "C点": "C site", "中路": "mid",
    "高台": "heaven", "警家": "CT", "出生点": "spawn",
    "奶一口": "heal me", "治疗": "heal", "架枪": "hold the angle",
    "静步": "walk", "抢点": "take site", "进点": "enter site",
    "清点": "clear site", "守包": "guard the Spike",
    "五杀": "ace", "完胜": "flawless", "刀": "knife",
}


# ---- 英文 -> 中文（队友流，队友说英语时生效）----
TERMS_EN2ZH = {
    # 特工
    "jett": "捷风", "phoenix": "不死鸟", "sova": "猎枭", "sage": "贤者",
    "viper": "蝰蛇", "cypher": "零", "reyna": "芮娜", "killjoy": "恺宙",
    "breach": "铁臂", "omen": "幽影", "brimstone": "布史东",
    "raze": "雷兹", "skye": "丝凯", "yoru": "夜露", "astra": "星礈",
    "kay/o": "K/O", "kayo": "K/O", "chamber": "尚勃勒", "neon": "霓虹",
    "fade": "菲德", "harbor": "哈珀", "gekko": "盖可",
    "deadlock": "蒂罗", "iso": "壹决", "clove": "暮蝶",
    "vyse": "薇丝", "tejo": "泰霍", "waylay": "维蕾",
    # 地图
    "ascent": "亚海悬城", "haven": "隐世修所", "split": "霓虹町",
    "bind": "源工重镇", "icebox": "极地寒港", "breeze": "微风岛屿",
    "fracture": "天漠之峡", "pearl": "深海明珠", "lotus": "莲华古城",
    "sunset": "日落之城", "abyss": "幽邃地窟",
    # 枪械
    "classic": "标配", "shorty": "短管", "frenzy": "狂怒",
    "ghost": "鬼魅", "sheriff": "警长", "stinger": "蜂刺",
    "spectre": "骇灵", "bucky": "重炮", "judge": "判官",
    "bulldog": "戍卫", "guardian": "守护", "phantom": "幻影",
    "vandal": "狂徒", "marshal": "游侠", "operator": "冥驹",
    "ares": "战神", "odin": "奥丁", "outlaw": "亡命徒",
    # 战术/术语
    "spike": "爆能器", "plant": "下包", "defuse": "拆包",
    "eco": "经济局", "force buy": "强起", "pistol round": "手枪局",
    "ult": "大招", "ultimate": "大招", "smoke": "烟", "flash": "闪",
    "molly": "燃烧弹", "duelist": "决斗者", "initiator": "先锋",
    "controller": "控场", "sentinel": "哨卫", "ranked": "排位",
    "overtime": "加时", "match point": "赛点", "clutch": "残局",
    "trade": "补枪", "peek": "拉枪", "flank": "绕后", "lurk": "断后",
    "push": "前压", "rotate": "转点", "rush": "快攻", "site": "包点",
    "mid": "中路", "heaven": "高台", "hell": "低台",
    "hookah": "水烟房", "showers": "浴室", "garage": "车库",
    "spawn": "出生点", "heal": "治疗", "ace": "五杀",
    "flawless": "完胜", "knife": "刀", "enemy spotted": "发现敌人",
    "spike planted": "爆能器已安装", "last round": "最后一回合",
    "match point": "赛点", "one enemy remaining": "还剩一名敌人",
}


def for_target(lang: str) -> dict:
    """按目标语言返回对应热词表。"""
    if lang == "zh":
        return TERMS_EN2ZH
    if lang == "en":
        return TERMS_ZH2EN
    return {}

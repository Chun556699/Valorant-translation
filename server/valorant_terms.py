"""游戏术语热词表（corpus.phrases，源语言词 -> 目标语言词）。

数据在 game_terms.json（客户端直连模式共用同一份：client-electron/src/game_terms.json）。
仅作为首次启动的种子数据 / 管理后台「恢复内置词表」的来源；
运行时以数据库 terms 表为准，可在管理后台在线增删。
目标语言为 zh 时用 en2zh（队友英语->中文），为 en 时用 zh2en（自己中文->英语）。
"""
import json
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_terms.json"),
          encoding="utf-8") as _f:
    _DATA = json.load(_f)

TERMS_EN2ZH: dict = _DATA["en2zh"]
TERMS_ZH2EN: dict = _DATA["zh2en"]


def for_target(lang: str) -> dict:
    """按目标语言返回对应热词表。"""
    if lang == "zh":
        return TERMS_EN2ZH
    if lang == "en":
        return TERMS_ZH2EN
    return {}

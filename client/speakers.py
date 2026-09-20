"""说话人命名：把模型返回的 speaker 标识映射为 好友1/好友2/...

模型（qwen3.8 speaker_detection）原生区分说话人时直接用其标识；
没有标识时所有队友语音归入"队友"。
"""


class SpeakerNamer:
    def __init__(self):
        self._map: dict[str, int] = {}   # raw_speaker -> 序号
        self._next = 1

    def name(self, raw: str | None, stream: str) -> str:
        if stream == "self":
            return "我"
        if raw is None:
            return "队友"
        if raw not in self._map:
            self._map[raw] = self._next
            self._next += 1
        return f"好友{self._map[raw]}"

    def reset(self):
        self._map.clear()
        self._next = 1

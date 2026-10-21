"""轻量英语屈折还原。

设计约束：本模块只负责「提出候选原形」，不判定正确性。调用方必须
逐个候选去本地库中验证，命中才采用。因此规则可以写得宽松——错误
候选只会被丢弃，不会污染数据。

不引入 nltk / spacy 这类重型依赖：它们会显著增大打包体积，
而本项目只需要处理高频屈折形式。
"""

from __future__ import annotations

MIN_LEN = 2

# 高频不规则形式。放在候选序列最前面，优先级最高。
_IRREGULAR: dict[str, str] = {
    "went": "go", "gone": "go", "goes": "go", "doing": "do", "did": "do", "done": "do",
    "ran": "run", "runs": "run",
    "better": "good", "best": "good", "worse": "bad", "worst": "bad",
    "children": "child", "men": "man", "women": "woman", "people": "person",
    "teeth": "tooth", "feet": "foot", "mice": "mouse", "geese": "goose",
    "took": "take", "taken": "take", "made": "make", "said": "say", "says": "say",
    "found": "find", "thought": "think", "brought": "bring", "buys": "buy",
    "bought": "buy", "taught": "teach", "caught": "catch",
    "sang": "sing", "sung": "sing", "swam": "swim", "swum": "swim",
    "drank": "drink", "drunk": "drink", "wrote": "write", "written": "write",
    "spoke": "speak", "spoken": "speak", "began": "begin", "begun": "begin",
    "knew": "know", "known": "know", "grew": "grow", "grown": "grow",
    "threw": "throw", "thrown": "throw", "flew": "fly", "flown": "fly",
    "drew": "draw", "drawn": "draw", "fell": "fall", "fallen": "fall",
    "felt": "feel", "kept": "keep", "slept": "sleep", "left": "leave",
    "meant": "mean", "met": "meet", "paid": "pay", "sent": "send",
    "spent": "spend", "built": "build", "lent": "lend", "lost": "lose",
    "held": "hold", "told": "tell", "stood": "stand", "understood": "understand",
    "won": "win", "sat": "sit", "led": "lead", "rose": "rise", "risen": "rise",
    "drove": "drive", "driven": "drive", "ate": "eat", "eaten": "eat",
    "gave": "give", "given": "give", "saw": "see", "seen": "see",
    "came": "come", "became": "become", "had": "have", "has": "have",
    "was": "be", "were": "be", "been": "be", "is": "be", "are": "be", "am": "be",
    "lay": "lie", "laid": "lay", "lying": "lie", "studies": "study",
}

_VOWELS = "aeiou"


def _is_simple_word(word: str) -> bool:
    return bool(word) and word.isascii() and word.replace("-", "").isalpha()


def candidates(word: str) -> list[str]:
    """按可能性降序返回候选原形。结果不含输入本身。"""
    w = (word or "").strip().lower()
    if not _is_simple_word(w):
        return []

    out: list[str] = []

    def add(*forms: str) -> None:
        for form in forms:
            if MIN_LEN <= len(form) < len(w) and form not in out:
                out.append(form)

    irregular = _IRREGULAR.get(w)
    if irregular:
        add(irregular)

    # 复数 / 第三人称单数
    if w.endswith("ies") and len(w) > 4:
        add(w[:-3] + "y")
    if w.endswith(("ses", "xes", "zes", "ches", "shes")) and len(w) > 4:
        add(w[:-2])
    if w.endswith("es") and len(w) > 3:
        add(w[:-2], w[:-1])
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        add(w[:-1])

    # 过去式 / 过去分词
    if w.endswith("ied") and len(w) > 4:
        add(w[:-3] + "y")
    if w.endswith("ed") and len(w) > 3:
        add(w[:-1], w[:-2])
        # 双写辅音：stopped -> stop
        if len(w) > 4 and w[-3] == w[-4] and w[-3] not in _VOWELS:
            add(w[:-3])

    # 现在分词
    if w.endswith("ing") and len(w) > 4:
        add(w[:-3], w[:-3] + "e")
        # 双写辅音：running -> run
        if len(w) > 5 and w[-4] == w[-5] and w[-4] not in _VOWELS:
            add(w[:-4])

    # 比较级 / 最高级
    if w.endswith("est") and len(w) > 4:
        add(w[:-3], w[:-2])
        if len(w) > 5 and w[-4] == w[-5] and w[-4] not in _VOWELS:
            add(w[:-4])
    if w.endswith("er") and len(w) > 3:
        add(w[:-2], w[:-1])
        if len(w) > 4 and w[-3] == w[-4] and w[-3] not in _VOWELS:
            add(w[:-3])

    return out

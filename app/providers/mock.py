"""离线示例 Provider。

用途：未配置 API 密钥时软件仍可完整走通「查询 → 收藏 → 浏览 → 导出」
全流程，便于先确认界面与交互是否符合预期。
"""

from __future__ import annotations

from ..core.models import Collocation, Entry, Phrase, Synonym
from .base import ProviderError

_SAMPLES: dict[str, dict] = {
    "ubiquitous": {
        "phonetic": "/juːˈbɪkwɪtəs/",
        "pos": "adj.",
        "definition_en": "seeming to be everywhere at the same time",
        "definition_zh": "无处不在的；普遍存在的",
        "example": "Mobile phones have become ubiquitous in everyday life.",
        "example_zh": "手机在日常生活中已变得无处不在。",
        "synonyms": [
            ("pervasive", "弥漫的；渗透的", "强调逐渐扩散并渗透到各处"),
            ("omnipresent", "无所不在的", "语域较高，常带神性或绝对意味"),
            ("universal", "普遍的", "侧重被广泛接受，而非空间分布"),
        ],
        "collocations": [
            (
                "become ubiquitous",
                "变得无处不在",
                "Plastic bags have become ubiquitous in daily life.",
                "塑料袋在日常生活中已变得无处不在。",
            ),
            (
                "ubiquitous presence",
                "无处不在的存在",
                "The app has a ubiquitous presence on campus.",
                "这款应用在校园里无处不在。",
            ),
            (
                "be ubiquitous in",
                "在……中随处可见",
                "Such slogans are ubiquitous in advertising.",
                "这类口号在广告中随处可见。",
            ),
        ],
    },
    "ephemeral": {
        "phonetic": "/ɪˈfemərəl/",
        "pos": "adj.",
        "definition_en": "lasting for only a very short time",
        "definition_zh": "短暂的；转瞬即逝的",
        "example": "The beauty of the cherry blossom is ephemeral.",
        "example_zh": "樱花之美转瞬即逝。",
        "synonyms": [
            ("transient", "短暂的；临时的", "偏中性客观，常用于现象或状态"),
            ("fleeting", "飞逝的", "强调速度快，多修饰感觉或瞬间"),
            ("momentary", "片刻的", "时长最短，仅指一瞬间"),
        ],
        "collocations": [
            (
                "ephemeral beauty",
                "转瞬即逝的美",
                "The poem celebrates ephemeral beauty.",
                "这首诗赞美转瞬即逝的美。",
            ),
            (
                "the ephemeral nature of",
                "……的短暂性",
                "He often reflects on the ephemeral nature of fame.",
                "他常常思考名声的短暂。",
            ),
            (
                "ephemeral pleasures",
                "短暂的欢愉",
                "Sweets offer only ephemeral pleasures.",
                "甜食带来的只是短暂的愉悦。",
            ),
        ],
    },
    "meticulous": {
        "phonetic": "/məˈtɪkjələs/",
        "pos": "adj.",
        "definition_en": "showing great attention to small details",
        "definition_zh": "一丝不苟的；极为细致的",
        "example": "She kept meticulous records of every experiment.",
        "example_zh": "她为每一次实验都做了极为细致的记录。",
        "synonyms": [
            ("thorough", "彻底的；周密的", "强调覆盖面完整，不含「琐碎」意味"),
            ("scrupulous", "严谨的", "带道德色彩，强调不越界"),
            ("painstaking", "费尽苦心的", "强调投入大量时间精力"),
        ],
        "collocations": [
            (
                "meticulous attention to",
                "对……一丝不苟",
                "She is known for meticulous attention to detail.",
                "她以对细节一丝不苟著称。",
            ),
            (
                "be meticulous about",
                "对……极为讲究",
                "He is meticulous about his handwriting.",
                "他对自己的书写极为讲究。",
            ),
            (
                "meticulous planning",
                "周密的计划",
                "The trip was the result of meticulous planning.",
                "这次旅行是周密计划的结果。",
            ),
        ],
    },
}


class MockProvider:
    """离线 Provider，仅用于演示与自检。"""

    name = "mock"

    def lookup(self, word: str) -> Entry:
        key = (word or "").strip().lower()
        data = _SAMPLES.get(key)
        if data is None:
            raise ProviderError(
                f"离线示例模式仅有 {len(_SAMPLES)} 个词条，"
                f"「{word}」不在其中。请在设置中配置 API 后重试。"
            )
        return Entry(
            lemma=key,
            phonetic=data["phonetic"],
            pos=data["pos"],
            definition_en=data["definition_en"],
            definition_zh=data["definition_zh"],
            example=data["example"],
            example_zh=data["example_zh"],
            synonyms=[
                Synonym(word=w, gloss_zh=g, diff_note=d) for w, g, d in data["synonyms"]
            ],
            # 示例模式**要有搭配**：它是「没配密钥的人看到的整个产品」。
            # 这里空着的话，搭配区在演示模式下永远不存在，用户会以为没做这个功能。
            collocations=[
                Collocation(phrase=p, gloss_zh=g, example=e, example_zh=z)
                for p, g, e, z in data["collocations"]
            ],
        )

    def lookup_phrase(self, phrase: str) -> Phrase:
        """示例模式也能演示词组查询。

        数据直接取自各示例词条的固定搭配：那本来就是真实词组，凭空再造一批
        只会多一份要维护的假数据。找不到时**如实说找不到并给出出路**——返回
        一条只有标题的空词组会让演示模式看起来「能用」，而收藏进去是空的。
        """
        key = " ".join((phrase or "").split()).lower()
        for data in _SAMPLES.values():
            for text, gloss, example, example_zh in data["collocations"]:
                if text.strip().lower() == key:
                    return Phrase(
                        phrase=text,
                        gloss_zh=gloss,
                        example=example,
                        example_zh=example_zh,
                    )
        raise ProviderError(
            f"离线示例模式只有 {sum(len(d['collocations']) for d in _SAMPLES.values())}"
            f" 条词组，「{phrase}」不在其中。请在设置中配置 API 后重试。"
        )

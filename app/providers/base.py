"""Provider 协议定义。

UI 与业务层只依赖本协议，不感知具体厂商。更换或新增服务时
只需实现一个 lookup()，其余代码无需改动。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.models import Entry, Phrase


class ProviderError(RuntimeError):
    """查词失败（网络、鉴权、解析等）。"""


class NonRetryableError(ProviderError):
    """确定性失败，重试无意义（鉴权错误、地址不存在等）。"""


@runtime_checkable
class DictionaryProvider(Protocol):
    name: str

    def lookup(self, word: str) -> Entry:
        """查询单个词条，返回结构化 Entry（不含入库字段）。"""
        ...

    def lookup_phrase(self, phrase: str) -> Phrase:
        """查询一条词组，返回结构化 Phrase（不含入库字段）。

        与 `lookup` 分开而不是复用它：两者的输出结构不同（词组没有音标、
        词性、同义语），产物也要落进另一张表。混成一个方法，调用方就得
        再判一次「这次拿到的东西该进哪张表」，而那个判断本来就该由
        「方法的名字」承担。
        """
        ...


# 各接入方式「结构上能提供哪些要素」。界面据此决定要不要喊「缺项」。
#
# 这份表不是可有可无的元数据。详情面板对空字段是不渲染的，而缺失提示是
# 照实喊出来的——如果离线词库（结构上就不含同义语）也照喊，那么每一条离线
# 词条底下都会挂一句「缺少同义语」。提示一旦恒定出现就等于没有提示，
# 用户会连真正的缺项（接口这次少给了一条例句）一起忽略掉。
PROVIDER_FIELDS: dict[str, frozenset[str]] = {
    "openai-compat": frozenset(
        {"definition_en", "definition_zh", "synonyms", "collocations", "example"}
    ),
    "mock": frozenset(
        {"definition_en", "definition_zh", "synonyms", "collocations", "example"}
    ),
    # ECDICT 是「词 → 释义」语料：有释义、约三成词条配了例句，但没有近义词关系，
    # **也没有固定搭配**——它是词典数据，不是搭配数据。离线时如实标缺，
    # 而不是假装有那么一块（空白区会让人以为「这个词就是没有搭配」）。
    "offline": frozenset({"definition_en", "definition_zh", "example"}),
}

# 未知来源按「全都能给」处理：宁可多喊一次缺项，也不要把真缺项藏起来。
ALL_FIELDS = frozenset(
    {"definition_en", "definition_zh", "synonyms", "collocations", "example"}
)


def provided_fields(source: str) -> frozenset[str]:
    """返回该来源结构上能提供的要素集合。"""
    return PROVIDER_FIELDS.get((source or "").strip().lower(), ALL_FIELDS)


# 来源在界面上的显示名。直接把内部标识（`openai-compat`）摆在用户面前
# 等于没解释——他要判断的是「这条内容是接口给的还是本机词典给的」。
PROVIDER_LABELS: dict[str, str] = {
    "openai-compat": "接口",
    "offline": "本机离线词库",
    "mock": "内置示例",
}


def provider_label(name: str) -> str:
    """把来源标识翻译成界面用语；未知标识原样返回，不隐藏信息。"""
    key = (name or "").strip().lower()
    return PROVIDER_LABELS.get(key, name or "")


def source_badge(source: str) -> str:
    """详情区那个来源小标签的文字。

    离线词条**不用**「来源 X」的句式。别的来源说的是「这条从哪来」，而离线这条
    还要多说一件事：接了接口重查一次，它会明显更全（例句、固定搭配、近义辨析
    接口才有）。「离线待重查」五个字把两件事说完了，也不必在详情区多摆一行
    解释——那行说明会一直占着位置，而用户看一眼就知道该做什么。

    判据是来源本身，不看 `fallback_reason`：主动选离线模式拿到的词条同样值得
    重查，两者在这一点上没有区别。
    """
    key = (source or "").strip().lower()
    if key == "offline":
        return "离线待重查"
    return f"来源 {provider_label(source)}"


# 哪些来源**结构上**给不出词组。
#
# 判据是「有没有这类数据」，不是「试一次看看」：内置离线词库在构建时就把含
# 空格的条目排除掉了（`scripts/build_offline_dict.py` 的 PLAIN_WORD_COND 只
# 保留 `[a-z-]` 组成的词）。**两种档位都是如此**——全量档 769,435 条里含空格的
# 仍然是 0 条，词多了不等于有词组。所以离线模式下查词组不是「这次没查着」，
# 而是这个来源里根本没有；界面据此直接拒绝并说明出路，比让用户看一串失败诚实。
#
# 未登记的来源按「能给」处理，与 `ALL_FIELDS` 的乐观口径一致：判错的代价是
# 一次说明清楚的报错；反过来判错，用户会以为这个功能不存在。
PHRASE_INCAPABLE = frozenset({"offline"})


def supports_phrases(name: str) -> bool:
    """该来源能不能查词组。"""
    return (name or "").strip().lower() not in PHRASE_INCAPABLE

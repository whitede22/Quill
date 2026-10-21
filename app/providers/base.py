"""Provider 协议定义。

UI 与业务层只依赖本协议，不感知具体厂商。更换或新增服务时
只需实现一个 lookup()，其余代码无需改动。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.models import Entry


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

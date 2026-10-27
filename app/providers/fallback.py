"""接口失败时回落到离线词库。

## 为什么值得做

接口挂掉、额度用尽、断网——这些都不是用户当场能解决的问题，但「查不到词」
会让他什么都做不了。回落至少让他继续读下去，而不是对着一个报错干等。

## 为什么必须标注，不能悄悄回落

悄悄地回落会让「接口失败」这件事从界面上消失：用户看到释义就以为接口是通的，
直到某天发现同义语一直空着、例句越来越稀，才回头怀疑配置。因此兜底产出的词条
会带上真实来源（`source="offline"`）与失败原因（`fallback_reason`），
详情面板显式说明「接口请求失败，以下内容来自本机离线词库」。

## 兜底也没命中时抛哪个错

抛**原始**的接口错误，而不是「离线词库未收录」。前者指向用户真正需要处理的
问题（密钥失效、地址写错、网络不通），后者只是说兜底也没碰上这个词——
把它当结论抛出去，会把一个可修复的配置问题伪装成词库容量问题。
"""

from __future__ import annotations

from ..core.models import Entry, Phrase
from .base import DictionaryProvider, ProviderError


class FallbackProvider:
    """先问接口，失败则回落到备用 Provider（离线词库）。"""

    def __init__(self, primary: DictionaryProvider, fallback: DictionaryProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        # 对外仍以主 Provider 自居：它才是用户选择的接入方式，兜底只是它的失败
        # 分支。真正的产出方写在词条的 source 上，由 DictionaryService 保留。
        self.name = primary.name

    def lookup(self, word: str) -> Entry:
        try:
            return self.primary.lookup(word)
        except ProviderError as exc:
            try:
                entry = self.fallback.lookup(word)
            except ProviderError:
                # 兜底也没有这个词：把原始错误抛出去，它更有诊断价值
                raise exc from None
            entry.source = self.fallback.name
            entry.fallback_reason = str(exc)
            return entry

    def lookup_phrase(self, phrase: str) -> Phrase:
        """词组**不兜底**，只问接口。

        兜底在这里帮不上忙：离线词库结构上不含词组（构建时就排除了含空格的
        条目），回落过去必然再失败一次。而失败一次之后抛哪个错是个真问题——
        `lookup` 那边刻意抛原始接口错误，因为离线「未收录」会把密钥失效伪装成
        词库容量问题；词组这条路径更彻底：回落本身就是空动作，只是把同一个
        失败多绕一圈、外加一句误导性的「离线未收录」。
        """
        return self.primary.lookup_phrase(phrase)

"""查词服务：串联缓存、词形归并与外部 Provider。"""

from __future__ import annotations

import string

from ..providers.base import DictionaryProvider, ProviderError
from ..providers.prompts import PROMPT_VERSION
from . import lemmatizer
from .models import Entry
from .repository import Repository

_PUNCT = string.punctuation + " \t\r\n\u3000"


def normalize(word: str) -> str:
    """规整用户输入：去首尾标点，纯字母词转小写以便命中缓存。"""
    w = (word or "").strip().strip(_PUNCT)
    if w and w.isascii():
        probe = w.replace("-", "").replace("'", "")
        if probe.isalpha():
            w = w.lower()
    return w


class DictionaryService:
    """查词入口。

    缓存策略为三级查找，避免同一词的不同形态重复调用接口：
      1. 按原形直接命中 entries
      2. 按别名表命中（此前记录过的屈折形式）
      3. 用本地屈折规则提出候选，逐个回查，命中则建立别名
    三级都未命中才真正请求 Provider。

    缓存的**有效性**还与提示词版本有关：词条记录着它是哪一版提示词生成的，
    与当前 PROMPT_VERSION 不一致时会被标记为陈旧（`Entry.prompt_stale`）。
    注意标记陈旧**不等于缓存失效**——内容照常返回，只是界面会提示可重查。
    若把陈旧当成未命中，离线状态下点开已有词条会直接失败，那是把
    「内容可能是旧的」升级成了「根本看不到」。
    """

    def __init__(self, repo: Repository, provider: DictionaryProvider) -> None:
        self.repo = repo
        self.provider = provider

    def set_provider(self, provider: DictionaryProvider) -> None:
        self.provider = provider

    # ---------- 提示词版本 ----------

    @staticmethod
    def is_stale(entry: Entry) -> bool:
        """该词条是否由旧版提示词生成。"""
        return int(entry.prompt_version or 0) != PROMPT_VERSION

    def mark_stale(self, entry: Entry | None) -> Entry | None:
        """给「从列表打开」的词条补上陈旧标记。

        列表走 repo.get_by_id 直接渲染，不经过 _from_cache，因此需要显式标记，
        否则同一个词条「搜索打开」会提示、「点击打开」不会提示。
        """
        if entry is not None:
            entry.prompt_stale = self.is_stale(entry)
        return entry

    # ---------- 对外接口 ----------

    def lookup(
        self,
        word: str,
        use_cache: bool = True,
        *,
        persist: bool = True,
        accept_fallback: bool = True,
    ) -> tuple[Entry, bool]:
        """返回 (词条, 是否来自缓存)。查词结果默认写入本地库。

        两个关键字参数只为「批量重查」而存在，单条查词一律用默认值：

        - `accept_fallback=False`：**不采纳**接口失败后由离线词库兜底产出的内容，
          改为抛 `ProviderError`（原因就是接口那次的失败原因）。单条重查时用户
          看得见状态栏那句「接口请求失败，已回落到离线词库」，他当场就能判断；
          而批量跑几十上百条时没人盯得住每一条，一次网络抖动就会把已经拿到的
          接口内容换成离线内容——而且是覆盖写，不可撤销。
        - `persist=False`：只取回、不写库、也不建别名。批量重查要先判定这一条
          结果可用，再由调用方决定写不写。
        """
        surface = normalize(word)
        if not surface:
            raise ValueError("请输入要查询的单词")

        if use_cache:
            hit = self._from_cache(surface)
            if hit is not None:
                return hit, True

        entry = self.provider.lookup(surface)
        entry.lemma = entry.lemma or surface
        # Provider 自带来源时以它为准——「接口失败后由离线词库兜底」产出的词条
        # 会带着自己的 source。无条件覆盖会把离线内容说成接口给的，
        # 而用户恰恰需要看出这个区别。
        entry.source = entry.source or self.provider.name
        # 落盘「由哪一版提示词生成」。缺了这一步，改提示词后重查已存词条会
        # 静默复用旧内容，而版本号看起来又像是已经兜住了，反而更难发现。
        entry.prompt_version = PROMPT_VERSION
        entry.prompt_stale = False

        # 兜底结果的判定必须在这里、在写库之前。`fallback_reason` 是唯一判据
        # （不能看 source：用户主动选离线模式时 source 同样是 "offline"），
        # 与详情区那句提示、状态栏那句来源说明用的是同一个字段。
        if not accept_fallback:
            reason = str(getattr(entry, "fallback_reason", "") or "").strip()
            if reason:
                raise ProviderError(f"接口这次没有返回内容：{reason}")

        # 空壳拒收。既没有英文也没有中文释义的返回，实际上等于请求失败——
        # 存进去只会在生词本里留下一条空白记录，并被一并导出、打印。
        # 缺其中一部分则不拒收（拿到多少算多少），但界面会提示缺了什么。
        if not entry.has_definition():
            raise ProviderError(
                f"接口没有返回「{surface}」的释义。"
                "可能是本次返回不完整，或该词不在模型的知识范围内，请重试。"
            )

        if not persist:
            return entry, False

        saved = self.repo.upsert(entry)
        self._link_variant(surface, saved)
        return self._attach_aliases(saved), False

    def refresh(self, word: str, *, accept_fallback: bool = True) -> Entry:
        """忽略缓存强制重新查询（用于结果不理想时重试）。

        `accept_fallback=False` 供批量重查使用：接口这次没答上来的词条保持原样，
        而不是被兜底内容顶掉。
        """
        return self.lookup(
            word, use_cache=False, accept_fallback=accept_fallback
        )[0]

    def cached(self, word: str) -> Entry | None:
        """只在本地缓存里找，**绝不发网络请求**。

        给「输入框一次粘进来一串词」那条路径用：拆词之前要先判断整串是不是一条
        已经收录过的词组（`ice cream`、`New York`），命中就当词组查。

        判据必须是「本地有没有」，不能是「查一次看看」——后者会在判断阶段就
        发出网络请求：粘 10 个词进去，程序先花几十秒问接口「hello world warm
        weather 这些词的那个整串」在不在，然后才拆开。而用户要的只是把 10 个词
        收进来。
        """
        surface = normalize(word)
        if not surface:
            return None
        return self._from_cache(surface)

    # ---------- 内部 ----------

    @staticmethod
    def _usable(entry: Entry | None) -> bool:
        """空壳词条（无任何释义）不算命中，否则会阻塞后续重试。"""
        return bool(entry and (entry.definition_en or entry.definition_zh))

    def _from_cache(self, surface: str) -> Entry | None:
        direct = self.repo.get(surface)
        if self._usable(direct):
            return self._decorate(direct)

        aliased = self.repo.find_by_alias(surface)
        if self._usable(aliased):
            return self._decorate(aliased)

        for candidate in lemmatizer.candidates(surface):
            hit = self.repo.get(candidate)
            if self._usable(hit):
                self.repo.link_alias(surface, hit.id)
                return self._decorate(hit)

        return None

    def _decorate(self, entry: Entry) -> Entry:
        """缓存命中的统一出口：补齐变体形式并判定提示词是否落后。"""
        entry.prompt_stale = self.is_stale(entry)
        return self._attach_aliases(entry)

    def _link_variant(self, surface: str, entry: Entry) -> None:
        """输入形态与词条原形不同时建立映射，下次即走缓存。"""
        if entry.id and entry.lemma.strip().lower() != surface.lower():
            self.repo.link_alias(surface, entry.id)

    def _attach_aliases(self, entry: Entry) -> Entry:
        if entry.id:
            entry.aliases = self.repo.aliases_of(entry.id)
        return entry

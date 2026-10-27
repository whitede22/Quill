"""查词服务：串联缓存、词形归并与外部 Provider。"""

from __future__ import annotations

import string

from ..providers.base import DictionaryProvider, ProviderError
from ..providers.prompts import PROMPT_VERSION
from . import lemmatizer
from .models import Entry, Phrase
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


def normalize_phrase(text: str) -> str:
    """规整词组输入：折叠空白、去首尾标点、纯英文则转小写。

    **不能复用 `normalize()`**：它判「纯字母」用的是 `probe.isalpha()`，而词组
    里必然有空格，于是 `Take Off` 会原样保留大小写。词组表按 NOCASE 去重，库里
    存的是小写那份，两串在 Python 里却是两个不同的字符串——缓存永远命不中，
    用户每查一次都会多打一次接口，而界面上看起来「已经收藏过了」。

    所以这里先把空格摘掉再判：`Take Off` → `takeoff` 是纯字母，于是整串转小写。
    """
    w = " ".join((text or "").split())
    w = w.strip(_PUNCT)
    probe = w.replace(" ", "").replace("-", "").replace("'", "")
    if w and w.isascii() and probe.isalpha():
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

    def lookup_phrase(self, text: str) -> tuple[Phrase, bool]:
        """查一条词组。返回 (词组, 是否本次新增)。

        与 `lookup` 的差别不止「查表不同」，每一处都是「词条有、词组没有」：

        - 缓存只有一张表。`phrases` 按内容唯一（NOCASE），没有别名表，也没有
          词形归并——词组没有屈折变化这回事，`take off` 的过去式不是另一条
          词组，而是同一句子里的事；
        - 不写 `entries`，也绝不借道 `upsert`。两张表各自从 1 发号，写错表
          不会报错，只会在另一个集合里多出一条同号、毫不相干的数据；
        - 不碰提示词版本。`phrases` 没有那一列，所以词组不参与「待重查」的
          判定——这也是它不出现在批量重查里的原因（见 `_sync_mode_buttons`）。

        「是否新增」按**收藏条数有没有涨**判定，与详情区那个「收藏」按钮同一
        判据：`add_phrase` 是幂等的，它返回的行在「刚写进去」与「本来就存在」
        两种情况下长得一样，分不出来。
        """
        surface = normalize_phrase(text)
        if not surface:
            raise ValueError("请输入要查询的词组")
        if " " not in surface:
            # 单个词在词组模式下查，会在词组收藏里留下一条与生词本重复的记录：
            # 同一份内容两个集合各存一份，删了一边另一边还在，而界面上都叫
            # 「释义」。这个动作没有正确的落点，所以拒绝并指路。
            raise ProviderError(
                f"「{surface}」是单个词，不是词组。\n"
                "查单词请切到左栏「生词本」再查，那样才会进生词本。"
            )

        stored_hit = self.repo.get_phrase(surface)
        if stored_hit is not None:
            return stored_hit, False

        lookup = getattr(self.provider, "lookup_phrase", None)
        if lookup is None:
            # Provider 协议里有这个方法，但测试替身与将来可能的第三方实现未必
            # 都提供。AttributeError 会直接糊到界面上，用户只能看到一句
            # 「'XxxProvider' object has no attribute 'lookup_phrase'」。
            name = str(getattr(self.provider, "name", "") or "")
            raise ProviderError(
                f"当前接入方式（{name or '未知'}）不支持词组查询。\n"
                "到右上角「设置」里接上接口后再试。"
            )

        phrase = lookup(surface)
        phrase.phrase = " ".join((phrase.phrase or surface).split())
        # 空壳拒收，与词条同一条理由：只有标题、没有简释的行，在列表里看着
        # 正常，点开是空的，还会被一并导出。
        if not phrase.gloss_zh:
            raise ProviderError(
                f"接口没有返回「{surface}」的释义，请重试。"
            )
        # 它不是从某个词的搭配区收藏来的，`source_lemma` 保持空——填上用户
        # 输入的原串会让「按来源词筛选词组」多出一批查不到出处的行。
        phrase.source_lemma = ""

        before = self.repo.count_phrases()
        stored = self.repo.add_phrase(phrase)
        return stored, self.repo.count_phrases() > before

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

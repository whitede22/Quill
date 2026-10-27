"""领域模型：词条与同义语。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

# 标签的分隔符。**这是唯一一份定义**，三个消费方都走它：
#   1. 详情区的标签输入框（用户可能敲逗号，也可能敲空格）
#   2. 按标签筛选（判据必须是「解析出来的标签里有它」，不能是字符串包含）
#   3. 导入回填（外部文件里的 tags 字段形态不可控）
#
# 中英文逗号、分号与任意空白都算分隔符：用户写「考研,高频」和「考研 高频」
# 表达的是同一件事，而**存进库里的形态必须是确定的**（单空格分隔）——否则
# 按标签筛选只好像素级地猜边界，而那种猜测一定会在某个标签是另一个的子串时
# 出错（筛「考研」把「考研英语」也带出来）。
_TAG_SEP_RE = re.compile(r"[,\uFF0C;；、\s]+")


def tags_of(value: object) -> list[str]:
    """把任意输入当成标签序列来看：字符串是一整串，序列逐项取，其余当空。

    单独成一个函数，是为了让 `split_tags` 不必自己判类型——「哪些形态算标签
    输入」这件事只在这里回答一次。
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return []


def split_tags(value: object) -> list[str]:
    """把任意形态的标签输入拆成标签列表：去空、去重、保序。

    「去重」不是可选项：界面上顺手多打一个分隔符（`考研,,高频`）是很常见的
    输入，重复的标签会在筛选下拉里出现两次，看起来像两个不同的标签。
    """
    out: list[str] = []
    for chunk in tags_of(value):
        for part in _TAG_SEP_RE.split(chunk):
            tag = part.strip()
            if tag and tag not in out:
                out.append(tag)
    return out


def join_tags(value: object) -> str:
    """标签输入 → 存储形态（单空格分隔）。

    存储形态必须确定：按标签筛选的判据是「解析出来的标签里有它」，若库里存着
    逗号、双空格、大小写不一的形态，筛选就只能退化成子串匹配，而子串匹配一定
    会在「某个标签是另一个的前缀」时出错（筛「考研」带出「考研英语」）。
    """
    return " ".join(split_tags(value))


def _json_list(raw: object) -> list:
    """把「一串列表形态的东西」规整成 list。**坏输入一律当空，绝不抛**。

    它要同时应付两种来源，而且这两种来源长得像、实际完全不同：

    - **库里的 JSON 文本**（`synonyms_json` / `collocations_json`）：是一段
      字符串，必须 `json.loads` 才拿得到列表；
    - **模型刚返回的对象**（`Entry.from_payload` 拿到的已经是 list 了）：
      再 `json.loads` 一次会抛 `TypeError`，被下面的 `except` 吞成空列表——
      **搭配会一条不剩地消失，而且不报任何错**。实测就是这么丢的。

    所以能直接用的形态先用，剩下的才去解析。非列表（`{"a": 1}` 这种）也要挡掉：
    直接迭代 dict 拿到的是**键名**，随后被 `from_raw` 当成内容收下，产出「一条
    叫 phrase 的同义语」这种谁也解释不清的东西。
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if not isinstance(raw, str):
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


@dataclass
class Synonym:
    """一个同义语及其与主词的差异说明。"""

    word: str
    gloss_zh: str = ""
    diff_note: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> "Synonym | None":
        """容忍 LLM 返回的多种形态：字符串 / 字典。"""
        if isinstance(raw, str):
            word = raw.strip()
            return cls(word=word) if word else None
        if isinstance(raw, dict):
            word = str(raw.get("word") or raw.get("term") or "").strip()
            if not word:
                return None
            return cls(
                word=word,
                gloss_zh=str(raw.get("gloss_zh") or raw.get("zh") or "").strip(),
                diff_note=str(raw.get("diff_note") or raw.get("note") or "").strip(),
            )
        return None

    def to_dict(self) -> dict:
        return {"word": self.word, "gloss_zh": self.gloss_zh, "diff_note": self.diff_note}


@dataclass
class Collocation:
    """一个固定搭配：词组本身 + 中文简释 + 一条例句与它的翻译。

    **为什么搭配要自带例句，而不是与词条的例句区共用**：一个词有几条搭配，
    每条搭配的用法差异恰恰体现在它自己的句子里（`be in charge of` 与
    `take charge of` 的差别，只有各自的例句说得清）。共用一个例句区等于把
    「这个词怎么用」压成一条，而搭配区存在的理由就是回答那个问题。

    `from_raw` 的容错形态与 `Synonym.from_raw` 同源：模型返回的键名不受控，
    没见过「容错」的解析器在真实数据上会丢掉大半内容，而且**不报错**。
    """

    phrase: str
    gloss_zh: str = ""
    example: str = ""
    example_zh: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> "Collocation | None":
        """容忍 LLM 返回的多种形态：纯字符串 / 字典。"""
        if isinstance(raw, str):
            phrase = raw.strip()
            return cls(phrase=phrase) if phrase else None
        if isinstance(raw, dict):
            phrase = str(
                raw.get("phrase") or raw.get("collocation") or raw.get("term") or ""
            ).strip()
            if not phrase:
                return None
            return cls(
                phrase=phrase,
                gloss_zh=str(
                    raw.get("gloss_zh") or raw.get("gloss") or raw.get("zh") or ""
                ).strip(),
                example=str(
                    raw.get("example") or raw.get("example_en") or raw.get("sentence") or ""
                ).strip(),
                example_zh=str(
                    raw.get("example_zh")
                    or raw.get("zh_example")
                    or raw.get("translation")
                    or ""
                ).strip(),
            )
        return None

    def to_dict(self) -> dict:
        return {
            "phrase": self.phrase,
            "gloss_zh": self.gloss_zh,
            "example": self.example,
            "example_zh": self.example_zh,
        }


@dataclass
class Phrase:
    """词组收藏里的一条。

    **为什么是独立的表与独立的数据类，而不是给 `entries` 加一个 `kind` 列**：
    加列能省下一份导出路径，代价是把 `count()` / `list_entries()` / `search()` /
    导出上限判据全部牵进来——那一片正是变异与消融重点守护的地方。词组与词条的
    相同之处只有「一行文字 + 一个释义」，其余（标签、提示词版本、收录变体、
    全文索引）词组一概没有。判据是**影响半径**，不是「代码写得更少」。

    `source_lemma` 刻意只是一段文本、**不是外键**：它记的是「从哪个词的搭配里
    收藏来的」，删掉那个词条不该连带删掉用户攒下来的词组。真正要按它去重或
    过滤时（见 `Repository.phrases_containing`），按空格切词逐词比较，
    不用 `LIKE '%词%'` —— 子串匹配会让 `trunk` 命中 `run`。
    """

    phrase: str
    gloss_zh: str = ""
    example: str = ""
    example_zh: str = ""
    source_lemma: str = ""
    id: int | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Phrase":
        return cls(
            id=row["id"],
            phrase=row["phrase"],
            gloss_zh=row["gloss_zh"],
            example=row["example"],
            example_zh=row["example_zh"],
            source_lemma=row["source_lemma"],
            created_at=row["created_at"],
        )

    @classmethod
    def from_payload(cls, raw: object, phrase: str) -> "Phrase":
        """从接口返回的 JSON 构造一条词组。

        键名容错与 `Collocation.from_raw` 同源：模型返回的键名不受控，只认
        一种写法会丢掉大半内容，而且**不报错**。

        `phrase` 是用户输入的那条。模型给了规范形态（输入 `took off`、回来
        `take off`）就用模型的：词组表按内容去重，存一个规范写法比存当时的
        输入更有用。模型没给、或给了空白，回落到用户输入——绝不写一条空词组，
        那是整表里唯一无法用内容定位的行。
        """
        payload = raw if isinstance(raw, dict) else {}

        def _pick(*keys: str) -> str:
            for key in keys:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return " ".join(value.split())
            return ""

        text = _pick("phrase", "collocation", "term") or " ".join((phrase or "").split())
        return cls(
            phrase=text,
            gloss_zh=_pick("gloss_zh", "gloss", "zh"),
            example=_pick("example", "example_en", "sentence"),
            example_zh=_pick("example_zh", "zh_example", "translation"),
        )

    def to_entry(self) -> Entry:
        """适配成 `Entry`，让列表卡片与六种导出**复用同一条路径**。

        「词组收藏能导出六种格式」不是额外写的六个分支，而是这条适配换来的：
        卡片、`_list_text`、导出、格式说明样例全都只认 `Entry` 一种形态，
        再为词组写一套，就是同一件事的第二份定义。

        `source="phrase"` 会被 `provider_label` 原样返回（它不在来源名表里），
        这是有意的：词组不是任何一种「查词来源」的产物，不该借用接口或离线
        词典的名义出现在界面上。
        """
        return Entry(
            lemma=self.phrase,
            definition_zh=self.gloss_zh,
            example=self.example,
            example_zh=self.example_zh,
            source="phrase",
            id=self.id,
            created_at=self.created_at,
            updated_at=self.created_at,
        )


@dataclass
class Entry:
    """一个生词词条。"""

    lemma: str
    phonetic: str = ""
    pos: str = ""
    definition_en: str = ""
    definition_zh: str = ""
    example: str = ""
    example_zh: str = ""
    synonyms: list[Synonym] = field(default_factory=list)
    collocations: list[Collocation] = field(default_factory=list)
    notes: str = ""
    tags: str = ""
    source: str = ""
    # 该词条由哪一版提示词生成。0 表示未知来源（不会出现在正常写入路径上）。
    # 与 PROMPT_VERSION 不一致即说明这条内容是旧提示词的产物，界面会提示重查。
    prompt_version: int = 0
    # 运行时填充：用户曾用于查到本词条的其它形态（如 runs / running）
    aliases: list[str] = field(default_factory=list)
    # 运行时填充：本词条的提示词版本落后于当前版本（由 DictionaryService 判定）。
    # 只用于提示，**不改变缓存是否命中**——离线时旧内容也远比报错有用。
    prompt_stale: bool = False
    # 运行时填充：本词条是「接口失败 → 离线词库兜底」的产物，这里存失败原因。
    # 刻意**不入库**（from_row 不读它）：下次打开时界面只需要知道来源是离线词库，
    # 而「当时为什么失败」属于历史信息，留着反而会让人以为故障还在。
    fallback_reason: str = ""
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    # 间隔重复复习（SM-2）。`review_due` 是 ISO 时间戳，空串＝从未复习；
    # 其余三项描述「学到什么程度」，由复习会话按评分更新。
    review_due: str = ""
    review_count: int = 0
    review_interval: float = 0.0
    review_ease: float = 2.5

    @staticmethod
    def now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def synonyms_text(self) -> str:
        return " ".join(s.word for s in self.synonyms)

    @property
    def tag_list(self) -> list[str]:
        """本词条的标签（从存储形态解析）。

        任何「这个词有哪些标签」的判断都必须经过它，而不是去 `self.tags` 上做
        字符串包含——外部导入进来的 tags 可能是逗号分隔的，而界面写入的是空格
        分隔的，两种形态在库里同时存在。
        """
        return split_tags(self.tags)

    def missing_parts(self, provided: frozenset[str] | None = None) -> list[str]:
        """返回缺失的必备要素（中文标签），顺序即界面提示顺序。

        必要性在于：详情面板对空字段是直接不渲染的，于是「接口没返回同义语」
        与「这个词本来就没有同义语」在界面上完全一样。不把缺项说出来，
        用户就会把不完整当成结论，最后连缺项一起打印出来背。

        `provided` 是该词条来源**结构上能提供**的要素集合（见
        `providers.base.PROVIDER_FIELDS`）。传 None 表示按「全都能给」判断，
        即保持旧行为。必须区分的原因是：离线词库根本不含同义语关系，
        若照喊，每条离线词条都会挂一句「缺少同义语」——提示恒定出现
        就等于没有提示，真正的缺项反而被淹没。
        """
        present = {
            "definition_en": bool(self.definition_en.strip()),
            "definition_zh": bool(self.definition_zh.strip()),
            "synonyms": bool(self.synonyms),
            "collocations": bool(self.collocations),
            "example": bool(self.example.strip()),
        }
        labels = {
            "definition_en": "英文释义",
            "definition_zh": "中文释义",
            "synonyms": "同义语",
            "collocations": "固定搭配",
            "example": "例句",
        }
        wanted = present.keys() if provided is None else present.keys() & provided
        return [labels[key] for key in ("definition_en", "definition_zh", "synonyms", "collocations", "example") if key in wanted and not present[key]]

    def has_definition(self) -> bool:
        """是否至少有一种语言的释义。都没有即空壳，视为请求失败。"""
        return bool(self.definition_en.strip() or self.definition_zh.strip())

    def synonyms_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.synonyms], ensure_ascii=False)

    def collocations_json(self) -> str:
        return json.dumps([c.to_dict() for c in self.collocations], ensure_ascii=False)

    @classmethod
    def from_row(cls, row) -> "Entry":
        keys = row.keys()
        raw = row["synonyms_json"] if "synonyms_json" in keys else "[]"
        syns = [s for s in (Synonym.from_raw(x) for x in _json_list(raw)) if s]
        # collocations_json 与 synonyms_json 同款：列存在性判断不是防御性冗余，
        # Entry.from_row 也会吃到旧库的行（补列迁移之前的 snapshot）。
        raw_col = row["collocations_json"] if "collocations_json" in keys else "[]"
        colls = [c for c in (Collocation.from_raw(x) for x in _json_list(raw_col)) if c]
        return cls(
            id=row["id"],
            lemma=row["lemma"],
            phonetic=row["phonetic"],
            pos=row["pos"],
            definition_en=row["definition_en"],
            definition_zh=row["definition_zh"],
            example=row["example"],
            example_zh=row["example_zh"],
            synonyms=syns,
            collocations=colls,
            notes=row["notes"],
            tags=row["tags"],
            source=row["source"],
            prompt_version=int(row["prompt_version"] or 0)
            if "prompt_version" in keys
            else 0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            review_due=row["review_due"] if "review_due" in keys else "",
            review_count=int(row["review_count"] or 0) if "review_count" in keys else 0,
            review_interval=float(row["review_interval"] or 0) if "review_interval" in keys else 0.0,
            review_ease=float(row["review_ease"] or 2.5) if "review_ease" in keys else 2.5,
        )

    @classmethod
    def from_payload(cls, payload: dict) -> "Entry":
        """从 LLM 返回的 JSON 构造词条，字段缺失时降级为空串。"""
        syns = [s for s in (Synonym.from_raw(x) for x in _json_list(payload.get("synonyms"))) if s]
        colls = [
            c for c in (Collocation.from_raw(x) for x in _json_list(payload.get("collocations"))) if c
        ]
        return cls(
            lemma=str(payload.get("lemma") or "").strip(),
            phonetic=str(payload.get("phonetic") or "").strip(),
            pos=str(payload.get("pos") or "").strip(),
            definition_en=str(payload.get("definition_en") or "").strip(),
            definition_zh=str(payload.get("definition_zh") or "").strip(),
            example=str(payload.get("example") or "").strip(),
            example_zh=str(payload.get("example_zh") or "").strip(),
            synonyms=syns[:6],
            # 与同义语同一个上限思路：多出来的不是「更全」，而是详情面板被撑爆。
            # 提示词要的是 3–5 条，模型给 12 条时按前 5 条收。
            collocations=colls[:5],
        )

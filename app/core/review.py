"""间隔重复复习（SM-2 的简化实现）。

状态推进是**纯函数**：不碰数据库、不碰时钟，于是每个分支都能用「喂一个状态、
断言一个结果」的方式测——这正是复习算法最容易写错的地方（答错却没重置、
记住了却没推进间隔、难度因子跌破下限）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 四档评分。前三档是「记不住 / 拿不准 / 记住了」，第四档专门给「太简单」：
# 熟词留在队列里天天出现，等于把复习时间浪费在已经会的词上。
FORGOT = 0
FUZZY = 1
REMEMBERED = 2
EASY = 3

# 难度因子（ease）的下限与初始值。ease 越低，间隔涨得越慢——越难记的词越常回来。
# 上限是给「太简单」设的：每次 +0.15 不封顶的话，连点十几次之后间隔会涨成
# 天文数字，那个词等于再也不复习了。
MIN_EASE = 1.3
MAX_EASE = 3.0
INITIAL_EASE = 2.5

# 「记住了」的前两次间隔（天），之后每次乘 ease。
FIRST_INTERVAL = 1.0
SECOND_INTERVAL = 6.0

# 「太简单」第一次就给的间隔。跳过 1 天那一步：用户已经明说这个不用练。
EASY_FIRST_INTERVAL = 4.0

# 「太简单」在已有间隔上再乘的倍数。比「记住了」多跳一步，但不至于让间隔爆炸。
EASY_BOOST = 1.3

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def sm2(ease: float, interval: float, count: int, grade: int) -> tuple[float, float, int]:
    """按一次评分推进复习状态，返回 `(ease, interval, count)`。

    规则（SM-2 的简化，去掉标准版里没人用的 repetition 复刻）：

    - 忘了（FORGOT）：ease 扣 0.3，间隔回到 1 天，次数清零。
    - 模糊（FUZZY）：ease 扣 0.15，间隔回到 1 天，次数不变。
    - 记住了（REMEMBERED）：第一次间隔 1 天、第二次 6 天、之后每次乘 ease，次数 +1。
    - 太简单（EASY）：ease 加 0.15（封顶 3.0），间隔在「记住了」的基础上再乘 1.3，
      第一次直接给 4 天。次数 +1。
    """
    if grade == FORGOT:
        return max(MIN_EASE, ease - 0.3), FIRST_INTERVAL, 0
    if grade == FUZZY:
        return max(MIN_EASE, ease - 0.15), FIRST_INTERVAL, count
    if grade == REMEMBERED:
        if count == 0:
            new_interval = FIRST_INTERVAL
        elif count == 1:
            new_interval = SECOND_INTERVAL
        else:
            new_interval = interval * ease
        return ease, new_interval, count + 1
    if grade == EASY:
        new_ease = min(MAX_EASE, ease + 0.15)
        if count == 0:
            new_interval = EASY_FIRST_INTERVAL
        elif count == 1:
            new_interval = SECOND_INTERVAL * EASY_BOOST
        else:
            new_interval = interval * new_ease * EASY_BOOST
        return new_ease, new_interval, count + 1
    raise ValueError(f"未知复习评分：{grade!r}")


def format_timestamp(dt: datetime) -> str:
    """把时刻格式化成与 `Entry.now()` 一致的字符串（可字典序比较）。"""
    return dt.strftime(_TIMESTAMP_FMT)


def due_timestamp(now: datetime, interval: float) -> str:
    """从当前时刻与间隔算出下一次复习的到期时间戳（同 `Entry.now()` 格式）。"""
    return format_timestamp(now + timedelta(days=interval))


def now_timestamp() -> str:
    return format_timestamp(datetime.now())

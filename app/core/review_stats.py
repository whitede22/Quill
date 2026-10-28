"""学习摘要：只算口径明确、现有数据真能算出来的指标。

**刻意不做的两个指标**，写在这里，免得以后被「顺手加上」：

- 首次答对率。需要每条词的复习历史（第几次评分、评了什么），而 `entries`
  只存最后一次的间隔与次数。硬算就得把「现在记住了」当成「第一次就记住了」，
  那是错的，而且错得很体面——数字看着正常，结论完全不成立。
- 长期记忆率（隔 N 天仍能答对的比例）。同上，需要历史。

要算这两个，得先加一张复习流水表并开始积累数据。在那之前，界面显示的是
「还没开始采集」，不是 0%——把「没数据」显示成 0%，会被读成「全忘了」。
"""

from __future__ import annotations

import sqlite3

# 「已掌握」的判定口径：复习过至少 3 次，且当前间隔 ≥ 21 天。
# 这是一条**被明确写出来的约定**，不是常识：换个口径（比如只看次数）结论会变，
# 所以它必须只有一份定义，注释也要说清两个条件都算。
MASTERED_MIN_COUNT = 3
MASTERED_MIN_INTERVAL = 21.0


def summary(conn: sqlite3.Connection) -> dict:
    """返回学习摘要。库是空的也能调，不会抛。"""
    try:
        row = conn.execute(
            "SELECT"
            " COUNT(*) FILTER (WHERE review_due <> '') AS planned,"
            " COUNT(*) FILTER (WHERE review_count > 0) AS reviewed"
            " FROM entries"
        ).fetchone()
        mastered = conn.execute(
            "SELECT COUNT(*) FROM entries"
            " WHERE review_count >= ? AND review_interval >= ?",
            (MASTERED_MIN_COUNT, MASTERED_MIN_INTERVAL),
        ).fetchone()[0]
        avg = conn.execute(
            "SELECT AVG(review_interval) FROM entries WHERE review_count > 0"
        ).fetchone()[0]
    except sqlite3.Error:
        return {"planned": 0, "reviewed": 0, "mastered": 0, "avg_interval": 0.0}

    planned = int(row[0] or 0) if row else 0
    reviewed = int(row[1] or 0) if row else 0
    return {
        "planned": planned,
        "reviewed": reviewed,
        "mastered": int(mastered or 0),
        "avg_interval": round(float(avg or 0.0), 1),
    }


def summary_text(conn: sqlite3.Connection) -> str:
    """一行中文摘要。数据不足时直接说不足，不给百分比。"""
    data = summary(conn)
    if not data["reviewed"]:
        return "复习计划里还没有评过分的词，等复习几轮之后这里才有数字。"
    parts = [
        f"复习计划 {data['planned']} 个词",
        f"已开始复习 {data['reviewed']} 个",
        f"间隔 ≥ {MASTERED_MIN_INTERVAL:.0f} 天 {data['mastered']} 个",
        f"平均间隔 {data['avg_interval']} 天",
    ]
    text = "，".join(parts) + "。"
    text += "首次答对率与长期记忆率需要复习历史，这一版还没开始采集。"
    return text

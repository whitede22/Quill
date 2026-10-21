# -*- coding: utf-8 -*-
"""自证：这一轮新增的断言**真的能失败**。

铁律第一条——「新校验必须证明自己能失败」。做法是逐条把源码改回**修复前**
的样子（每次只改一处），跑一遍 `selfcheck`，确认**那条断言**当场报红；跑完
立刻还原并逐字节核对。

两个必须做对的地方（第一次就是在这里翻的车）：

1. **读写一律走字节**。`Path.write_text` 在 Windows 上默认把 `\\n` 翻成
   `\\r\\n`，于是每次变异都会捎带把行尾改坏——`selfcheck` 里那条「行尾统一为
   LF」会连报 3 条红，整个脚本「退出码非零」达成，**看起来每次都抓住了**。
   第一次跑出来的 9/9 全是假的：抓到的根本不是我要验的那条断言。
2. **先跑基线，再逐条比**。判据不是「退出码非零」，而是「这条变异的预期断言名
   出现在失败列表里，且基线本来是干净的」。否则任何一条无关的红（比如行尾）
   都能把「没抓住」粉饰成「抓住了」。

只在前台跑；每条的还原写在 `finally` 里，并用**字节**核对。临时工具，用完即删。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES: list[tuple[str, str, str, str, str]] = [
    (
        "app/ui/theme.py",
        "    font-size: 12px; color: $text2; font-weight: 600;",
        "    font-size: 12px; color: $text3; font-weight: 600;",
        "分区小标题用回占位色",
        "$text3` 只用在占位与禁用上",
    ),
    (
        "app/ui/theme.py",
        '("QLabel#section", "text2_strong"),',
        '("QLabel#section", "text3_strong"),',
        "section 的加强色挂回 text3_strong",
        "#section` 在最透档的加强色",
    ),
    (
        "app/ui/theme.py",
        "QPushButton#chip {",
        "QLabel#chip {",
        "chip 选择器写回 QLabel（死规则）",
        "chip 的选择器前缀是 QPushButton",
    ),
    (
        "app/ui/theme.py",
        "    border-radius: $radius_pill;",
        "    border-radius: 9px;",
        "圆角写回字面数字",
        "圆角只走命名 token",
    ),
    (
        "app/ui/theme.py",
        '"text2": "#6E6E6A",',
        '"text2": "#8A8A86",',
        "次要文字色改回旧值（3.47:1）",
        "每一对文字/底色都达标",
    ),
    (
        "app/ui/theme.py",
        '"text3": "#91918C",',
        '"text3": "#B6B6B1",',
        "占位色改回旧值（2.04:1）",
        "占位/禁用文字守住 3:1",
    ),
    (
        "app/ui/theme.py",
        "QPushButton#mode:checked {",
        "QLabel#mode:checked {",
        "选中态规则写成一个匹配不到的控件类",
        "按下态与选中态各有规则",
    ),
    (
        "app/ui/theme.py",
        '("QPushButton#review", _rgb(pal["surface"])),\n',
        "",
        "「待复习」按钮从垫底表里摘掉",
        "「待复习」按钮已接进",
    ),
    (
        "app/ui/onboarding.py",
        "from .theme import stylesheet",
        'from .theme import stylesheet\n_INLINE = "font-size: 26px;"',
        "欢迎页写回内联样式串",
        "不再内联样式串",
    ),
    (
        "app/ui/onboarding.py",
        '"查词时右栏给出固定搭配，每条配一句例句；点「收藏」收进词组收藏。"',
        '"查词时右侧会给出这个单词的固定搭配，每一条搭配都配一句例句；'
        '看顺眼的搭配，点「收藏」把它收进词组收藏。"',
        "教程文案写长到折行",
        "每句说明都排得下一行",
    ),
    # ---- 2026-10-14 导入：Word 文档与「一行列开的长词表」 ----
    # 这五条对应本轮新写的断言。它们各自退回**修复前**的样子，
    # 逐条确认目标断言当场报红。
    (
        "app/core/importer.py",
        '_DOCX_SUFFIXES = frozenset({".docx", ".docm"})',
        "_DOCX_SUFFIXES = frozenset()",
        ".docx 不再走解包（退回按文本读）",
        ".docx 解包成按段落分行的纯文本",
    ),
    (
        "app/core/importer.py",
        '        elif node.tag == f"{_W_NS}tab":\n            parts.append("\\t")\n',
        "",
        ".docx 不再还原制表符（两列会粘成一个词）",
        ".docx 的制表符不把两个词粘成一个",
    ),
    (
        "app/core/importer.py",
        "    listed = len(segments) > 1 and all(_is_plain_segment(seg) for seg in segments)",
        "    listed = len(segments) > 1 and all(\n"
        "        _is_plain_segment(seg) and len(_tokens_in(seg)) == 1 for seg in segments\n"
        "    )",
        "长列表判据退回「每一段只能是一个词」",
        "一行逗号列开的词表整行收下",
    ),
    (
        "app/core/importer.py",
        "        if len(tokens) > 1 and _PLAIN_SEGMENT_RE.fullmatch(flat):",
        "        if False:",
        "整行是词组时退回「取第一个词」",
        "整行就是一个词组时如实报告",
    ),
    (
        "app/core/importer.py",
        "        if _valid_word(normalize(pick)):",
        "        if _valid_word(pick):",
        "判定前不归一化（大写行被判成「不是单词」）",
        "大小写不同算同一个词",
    ),
]


def _run_selfcheck() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "scripts/selfcheck.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def main() -> int:
    code, base_out = _run_selfcheck()
    base_fails = [line.strip() for line in base_out.splitlines() if "FAIL" in line]
    print(f"基线：退出码 {code}，失败 {len(base_fails)} 条")
    if base_fails:
        print("  基线就不干净，先修好再谈自证：")
        for line in base_fails[:5]:
            print(f"    {line[:100]}")
        return 1

    bad = 0
    for rel, old, new, label, expect in CASES:
        path = ROOT / rel
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if text.count(old) != 1:
            print(f"  [BAD] 锚点不唯一（{text.count(old)} 次）：{label}")
            bad += 1
            continue
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        try:
            code, out = _run_selfcheck()
            fails = [line.strip() for line in out.splitlines() if "FAIL" in line]
            hit = [line for line in fails if expect in line]
            caught = code != 0 and bool(hit)
            print(
                f"  {'PASS' if caught else 'MISSED'}  {label}"
                f"  （退出码 {code}，失败 {len(fails)} 条，命中预期 {len(hit)} 条）"
            )
            if caught:
                print(f"         └ {hit[0][:100]}")
            else:
                print(f"         期望断言「{expect}」没有报红；实际红：{fails[:3]}")
                bad += 1
        finally:
            path.write_bytes(raw)
            if path.read_bytes() != raw:
                print(f"  [BAD] 还原失败，源文件已污染：{rel}")
                bad += 1
    print(f"\n自证结束：{len(CASES) - bad} / {len(CASES)} 被抓住")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

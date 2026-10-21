"""生成界面预览图（离屏渲染）。

用法：.venv/Scripts/python.exe scripts/screenshot.py [输出路径]
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SHOT_HOME = tempfile.mkdtemp(prefix="quill_shot_")
os.environ["QUILL_HOME"] = _SHOT_HOME


def _clean_home() -> None:
    """退出时删掉本次预览图用的临时数据目录。

    原先写的是 `atexit.register(shutil.rmtree, _SHOT_HOME, True)`，那个 `True`
    是 `ignore_errors`——**它把「删干净了」和「静默没删」变成了同一件事**。
    Qt 与 SQLite 的句柄未必在解释器退出前释放（目录里带 `quill.db-wal` /
    `-shm` 的残留就是这么来的），首次 `rmtree` 会被挡住，于是每跑一次预览图
    就悄悄漏一个装着数据库的目录。本项目的铁律里有一条专门讲这个：曾经 6 处
    `mkdtemp` 不清理，累积到 3763 个目录 / 2.6 GB，把系统盘写满，症状是 Bash 报
    `ENOSPC`——**看起来像工具坏了**。

    所以这里重试 + `gc.collect()`，仍失败就**打印警告**，不装作已经删掉。
    （进程被外部强杀时 `atexit` 本就不会执行，那种情况由 `clean_temp.py`
    兜底——它已登记 `quill_shot_*` 前缀。）
    """
    for _ in range(3):
        gc.collect()
        try:
            shutil.rmtree(_SHOT_HOME)
            return
        except OSError:
            pass
    print(f"警告：预览图临时目录未能清理：{_SHOT_HOME}", flush=True)


atexit.register(_clean_home)
# 不使用 offscreen 平台：该平台不加载系统字体，文字会渲染成方框。
# 改为在真实平台上创建「不显示但仍参与渲染」的窗口。

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import DB_PATH, DEFAULT_CONFIG, load_config  # noqa: E402
from app.core.db import connect, init_db  # noqa: E402
from app.core.dictionary import DictionaryService  # noqa: E402
from app.core.models import Entry, Synonym  # noqa: E402
from app.core.repository import Repository  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.settings import SettingsDialog  # noqa: E402
from app.ui.theme import stylesheet  # noqa: E402


def _sample_background(path: Path) -> None:
    """生成一张示例背景图（渐变 + 三团柔光）。

    刻意**自己画**而不往仓库里塞一张照片：照片有版权，而仓库里多一个二进制
    大文件还会让人误以为它是程序的一部分。这张图只用来演示「有一张图铺在界面
    后面」这件事，低饱和是有意的——背景图的作用是氛围，不是把正文压下去。
    """
    width, height = 1600, 1000
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, QColor("#8FA3BF"))
    gradient.setColorAt(0.5, QColor("#C9D2DC"))
    gradient.setColorAt(1.0, QColor("#E7E2D8"))
    painter.fillRect(0, 0, width, height, QBrush(gradient))
    for cx, cy, radius, color in (
        (300, 220, 460, QColor(84, 116, 170, 110)),
        (1230, 800, 560, QColor(198, 156, 112, 95)),
        (1160, 170, 320, QColor(112, 168, 156, 85)),
    ):
        radial = QRadialGradient(cx, cy, radius)
        radial.setColorAt(0.0, color)
        radial.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.fillRect(0, 0, width, height, QBrush(radial))
    painter.end()
    image.save(str(path))


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[1] / "preview.png"
    )

    config = load_config()
    config["provider"] = "mock"
    # 主题换色预览：`QUILL_SHOT_THEME=<主题名>`，任何场景都受它影响。
    # 不设时沿用配置里的主题，不改变原有产物。
    if os.environ.get("QUILL_SHOT_THEME"):
        config["theme"] = os.environ["QUILL_SHOT_THEME"]

    # 演示用的壁纸。**只在这里定一次**：背景图场景与设置界面场景都要用它，
    # 各生成一次的话，两个场景里的图可能不是同一张，对比起来就没有意义。
    # 默认自绘（低饱和渐变，避免往仓库塞照片）；判读效果时用
    # `QUILL_SHOT_WALLPAPER` 指向一张真实照片——自绘样张没有中间调与结构，
    # 任何不透明度都只呈现一层淡色偏，会把人误导成「透明没生效」。
    wallpaper = Path(os.environ["QUILL_HOME"]) / "wallpaper.png"
    _real_wallpaper = os.environ.get("QUILL_SHOT_WALLPAPER")
    if _real_wallpaper:
        wallpaper = Path(_real_wallpaper)
    else:
        _sample_background(wallpaper)

    conn = connect(DB_PATH)
    init_db(conn)
    repo = Repository(conn)
    service = DictionaryService(repo, MockProvider())
    for word in ("ubiquitous", "ephemeral", "meticulous"):
        service.lookup(word)

    # 造一个带屈折归并的词条，用于展示「收录变体」的实际效果
    repo.upsert(
        Entry(
            lemma="run",
            phonetic="/rʌn/",
            pos="v.",
            definition_en="to move quickly on foot",
            definition_zh="跑；奔跑",
            example="She runs every morning before work.",
            example_zh="她每天上班前都去跑步。",
            synonyms=[
                Synonym(word="sprint", gloss_zh="冲刺", diff_note="强调短距离全速"),
                Synonym(word="jog", gloss_zh="慢跑", diff_note="速度较慢，多为健身目的"),
                Synonym(word="dash", gloss_zh="猛冲", diff_note="强调突然且急促"),
            ],
        )
    )
    service.lookup("running")
    service.lookup("runs")

    # 给主预览词条写一条备注，展示备注框与「重新查询不会覆盖」的语义
    shown = repo.get("ubiquitous")
    if shown is not None:
        shown.notes = "记忆法：ubi（哪里）+ quit（任何）→ 哪里都在"
        repo.upsert(shown)

    # 不完整词条：接口只给了英文释义与例句，中文释义/同义语/例句翻译都缺
    # 用于展示详情区顶部的缺项提示
    repo.upsert(
        Entry(
            lemma="garrulous",
            phonetic="/ˈɡærələs/",
            pos="adj.",
            definition_en="excessively talkative, especially about trivial matters",
            example="Our garrulous neighbour kept us on the doorstep for an hour.",
        )
    )

    app = QApplication(sys.argv)
    app.setStyleSheet(stylesheet(config.get("theme")))
    window = MainWindow(repo, service, config)
    window.resize(1080, 700)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()

    def _row_of(prefix: str) -> int:
        for i in range(window.list.count()):
            if window.list.item(i).text().startswith(prefix):
                return i
        return 0

    kind = os.environ.get("QUILL_SHOT_KIND", "main")

    def shoot() -> None:
        if kind == "settings":
            # 设置对话框的校验提示：填错地址时当场指出，并保留对话框
            # 模型名与出厂默认保持一致：写死一个别的服务商的名字，会让画面出现
            # 「地址是 DeepSeek、模型是 gpt-4o-mini」这种自相矛盾的组合。
            dialog = SettingsDialog(
                {
                    "provider": "openai-compat",
                    "base_url": "",
                    "model": DEFAULT_CONFIG["model"],
                    # 这一屏顺带展示主题选择与「背景预览条」。带上背景图与一条
                    # 最近使用记录，否则预览条只会显示「（没有背景图）」，
                    # 「最近使用」也永远是空的那句占位。
                    "theme": config.get("theme"),
                    "background_image": str(wallpaper),
                    "background_blur": True,
                    "background_veil": "medium",
                    "background_recent": [str(wallpaper)],
                },
                "",
                window,
                on_theme_preview=window._apply_theme,
            )
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dialog.show()
            dialog._on_save()
            QTimer.singleShot(180, lambda: (dialog.grab().save(str(out)), app.quit()))
            return
        if kind == "fallback":
            # 兜底预览：真实走一遍「接口失败 → 离线词库 → 界面标注」。
            # 刻意不手工构造 Entry 直接渲染——那样截出来的是渲染函数的样子，
            # 不是用户会看到的样子（曾经正因为少了这段链路，界面上那句提示
            # 被列表重载覆盖掉而无人察觉）。
            from app.providers.base import ProviderError
            from app.providers.fallback import FallbackProvider
            from app.providers.offline import OfflineDictProvider

            class _Down:
                name = "openai-compat"

                def lookup(self, word: str) -> Entry:
                    raise ProviderError(
                        "请求超时（30 秒）。可能是网络不稳定，或接口响应过慢。"
                    )

            config["provider"] = "openai-compat"
            config["model"] = config.get("model") or DEFAULT_CONFIG["model"]
            # 让常驻状态与画面自洽：窗口构造时 config 还是 mock，右下角会停在
            # 「离线示例模式」，与画面里的兜底内容自相矛盾。写一个假密钥到
            # 临时数据目录（本脚本用独立临时 HOME，不会碰到真实配置）。
            from app.config import KEY_PATH
            from app.security.keystore import save_api_key

            save_api_key(KEY_PATH, "sk-preview")
            window._refresh_mode_label()
            window.detail.set_networked(window._is_networked())
            service.set_provider(FallbackProvider(_Down(), OfflineDictProvider()))
            # 用 apple 而不是 serendipity：它的释义含真换行（源语料把换行存成
            # 字面量 `\n`，构建层解码后才是多行）。截图顺带就是「解码生效」的证据——
            # 若解码被改坏，这张图上会明晃晃地出现 `\n` 这坨字符。
            window._lookup_word("apple")
            QTimer.singleShot(1200, lambda: (window.grab().save(str(out)), app.quit()))
            return
        if kind == "import":
            # 导入前的核对框。文案取自**真实实现**——先打桩 QMessageBox 把
            # `_confirm_import` 拦下来收走那段文本，再摆成对话框截图。
            # 手工另写一份文案的话，截出来的是「我以为的文案」，不是用户
            # 会看到的那一句，图就成了摆设。
            import app.ui.main_window as mw
            from app.core.importer import parse_text

            src = Path(os.environ["QUILL_HOME"]) / "生词表.txt"
            src.write_text(
                "1. ubiquitous  /juːˈbɪkwɪtəs/  adj. 无处不在的\n"
                "2. meticulous  /məˈtɪkjələs/  adj. 一丝不苟的\n"
                "3. ephemeral   /ɪˈfemərəl/    adj. 短暂的\n"
                "perseverance, tenacity\n"
                "She went to the store to buy some milk\n",
                encoding="utf-8",
            )
            captured: list[str] = []
            real_box = mw.QMessageBox

            class _Box:
                StandardButton = real_box.StandardButton

                @staticmethod
                def question(*a, **_k):
                    captured.append(str(a[2]))
                    return real_box.StandardButton.No

                warning = staticmethod(lambda *a, **k: None)
                information = staticmethod(lambda *a, **k: None)

            mw.QMessageBox = _Box
            try:
                window._confirm_import(
                    src, parse_text(src.read_text(encoding="utf-8"))
                )
            finally:
                mw.QMessageBox = real_box

            box = real_box(
                real_box.Icon.Information,
                "导入生词表",
                captured[0] if captured else "(没有取到文案)",
                real_box.StandardButton.Yes | real_box.StandardButton.No,
                window,
            )
            # 这个对话框只为截图而生，**必须以「子控件」身份摆出来，不能是顶层窗口**。
            #
            # 若让它作为顶层窗口 `show()`（`QMessageBox` 默认就是顶层，且是应用级
            # 模态），进程会在**退出阶段**卡死：`box.grab()` 正常、图正常存盘、
            # `app.quit()` 正常返回、`main()` 也正常返回并打印「已保存预览图」，
            # 但进程就此不退。卡点在 Windows 的 `ExitProcess` → DLL 分离环节，
            # 因此连 `os._exit(0)` 都救不了（它同样走 `ExitProcess`），
            # `faulthandler` 也 dump 不出栈（C 层持着 GIL）。现象极具迷惑性——
            # 日志已经说「存好了」，看起来跑完了，实际整条截图链就此停住
            # （实测卡死 6 分钟以上，只能外部强杀）。
            #
            # 为什么别的场景没事：它们的顶层窗口只有主窗口一个，主窗口 `show()` 不
            # 触发这个问题；import 场景是唯一**额外**造出一个已显示顶层对话框的。
            #
            # `setParent(window, Widget)` 让它成为主窗口的子控件——照样能排版与
            # `grab()`（`WA_DontShowOnScreen` + `show()` 仍是必需的，少了 `show()`
            # 会 `grab()` 到空 pixmap），但不再创建第二个原生顶层窗口，退出即恢复
            # 正常。实测：顶层写法 2/2 超时；子控件写法 5/5 干净退出，出图与原来
            # 逐像素同构（尺寸差 4px，是顶层那圈窗口边框）。
            box.setParent(window, Qt.WindowType.Widget)
            box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            box.show()
            QTimer.singleShot(
                200,
                lambda: (box.grab().save(str(out)), box.close(), app.quit()),
            )
            return
        if kind in ("onboarding", "onboarding_tutorial"):
            # 首次启动的欢迎页 / 教程页。`start_at_tutorial=True` 出教程那一页。
            #
            # 与 import 场景同一条约束：**不能作为顶层窗口 show()**。`OnboardingDialog`
            # 默认是顶层且模态，多一个已显示的顶层窗口会让进程在退出阶段卡死
            # （`ExitProcess` → DLL 分离环节，日志已经打完「已保存」但进程不退）。
            # `setParent(window, Widget)` 让它成为主窗口的子控件，照样排版与
            # `grab()`，但不创建第二个原生顶层窗口。
            from app.ui.onboarding import OnboardingDialog

            dialog = OnboardingDialog(
                config.get("theme"),
                start_at_tutorial=(kind == "onboarding_tutorial"),
            )
            dialog.setParent(window, Qt.WindowType.Widget)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dialog.show()
            QTimer.singleShot(220, lambda: (dialog.grab().save(str(out)), app.quit()))
            return
        if kind == "background":
            # 自定义背景图。QUILL_SHOT_BLUR=0 出一张不模糊的，用来直观对比
            # 「模糊蒙版」这个开关到底做了什么。
            config["background_image"] = str(wallpaper)
            config["background_blur"] = os.environ.get("QUILL_SHOT_BLUR", "1") != "0"
            # 面板让位档位：QUILL_SHOT_VEIL=soft|medium|bold，用于出三档对比图。
            # 不设时沿用配置里的默认档，不改变原有产物。
            if os.environ.get("QUILL_SHOT_VEIL"):
                config["background_veil"] = os.environ["QUILL_SHOT_VEIL"]
            window._apply_background()
            # 选中一条词条再截图：留着空详情区的话，画面里看不出「半透明面板
            # 让背景透上来」这件事。
            window.list.setCurrentRow(_row_of("ubiquitous"))
            QTimer.singleShot(260, lambda: (window.grab().save(str(out)), app.quit()))
            return
        window.list.setCurrentRow(
            _row_of("garrulous") if kind == "incomplete" else _row_of("ubiquitous")
        )
        QTimer.singleShot(180, lambda: (window.grab().save(str(out)), app.quit()))

    QTimer.singleShot(500, shoot)
    app.exec()
    conn.close()
    print(f"已保存预览图：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

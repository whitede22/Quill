"""启动脚本。

用法：
    python run.py               启动图形界面
    python run.py --selfcheck   无界面自检

自检模式用于验证打包产物是否完整：打包为无控制台程序后 stdout 不可见，
因此结果会同时写入 exe 同级目录的 selfcheck_result.txt，并以退出码反映成败。
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _report_path() -> Path:
    return _BASE / "selfcheck_result.txt"


def _cleanup_home(path: Path) -> None:
    """删掉自检用的临时数据目录。

    **不能写 `shutil.rmtree(path, ignore_errors=True)`。** 那个 `True` 会把
    「删干净了」和「静默没删」变成同一件事——本项目实测踩过一次同款：截图脚本
    每跑一次就静默漏掉一个装着 `lexicon.db` 的目录，堆了 8 个才被发现。

    所以这里重试 + `gc.collect()`（首次 `rmtree` 常被 Qt / SQLite 尚未释放的
    句柄挡住），仍失败就**打印警告**。用户看到一行字，总好过系统盘悄悄涨。
    """
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            gc.collect()
            time.sleep(0.05)
    print(f"警告：自检临时目录未能删除，可手动清理：{path}")


def run_selfcheck() -> int:
    # 使用临时数据目录，避免自检污染用户生词本
    home = Path(tempfile.mkdtemp(prefix="quill_selfcheck_"))
    os.environ["QUILL_HOME"] = str(home)
    # **建了就要登记清理。** 这个目录里装着 6 个 SQLite 文件（约 1.5 MB），而这段
    # 代码在**打包产物**里——用户每跑一次 `Quill.exe --selfcheck` 就多留一个。
    # 它不报错、不影响任何断言，只是让系统盘悄悄涨；本项目踩过同款（6 处
    # `mkdtemp` 不清理 → 3763 个目录 / 2.6 GB，最后以 `ENOSPC` 的形式爆发，
    # 表现得像「工具坏了」，而不是「有个目录没删」）。
    atexit.register(_cleanup_home, home)

    lines: list[str] = []
    failed = 0

    def note(name: str, passed: bool, detail: str = "") -> None:
        nonlocal failed
        if not passed:
            failed += 1
        lines.append(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. SQLite 与 FTS5
    try:
        import sqlite3

        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        probe.close()
        note("SQLite FTS5 可用", True)
    except Exception as exc:  # noqa: BLE001
        note("SQLite FTS5 可用", False, str(exc))

    # 2. 数据层：建表、写入、中文检索
    try:
        from app.core.db import connect, init_db
        from app.core.models import Entry, Synonym
        from app.core.repository import Repository

        conn = connect(Path(os.environ["QUILL_HOME"]) / "selfcheck.db")
        init_db(conn)
        repo = Repository(conn)
        repo.upsert(
            Entry(
                lemma="run",
                definition_en="to move quickly on foot",
                definition_zh="跑；奔跑",
                synonyms=[Synonym(word="sprint", gloss_zh="冲刺")],
            )
        )
        note("数据层建表与写入", repo.count() == 1)

        hits = repo.search("奔跑")
        note("中文子串检索", any(e.lemma == "run" for e in hits), f"命中 {len(hits)} 条")

        hits = repo.search("sprint")
        note("同义语列检索", any(e.lemma == "run" for e in hits), f"命中 {len(hits)} 条")

        from app.core.exporter import export_text

        text = export_text(repo.list_entries())
        note("文本导出", "run" in text and "跑；奔跑" in text)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        note("数据层建表与写入", False, str(exc))
        note("中文子串检索", False, "前置步骤失败")
        note("同义语列检索", False, "前置步骤失败")
        note("文本导出", False, "前置步骤失败")

    # 3. 词形归并
    try:
        from app.core.lemmatizer import candidates

        note("词形还原候选", "run" in candidates("running") and "go" in candidates("went"))
    except Exception as exc:  # noqa: BLE001
        note("词形还原候选", False, str(exc))

    # 4. 密钥加密（Windows DPAPI）
    try:
        from app.security.keystore import decrypt_text, encrypt_text

        token = encrypt_text("probe-secret")
        note("密钥加密往返", decrypt_text(token) == "probe-secret")
    except Exception as exc:  # noqa: BLE001
        note("密钥加密往返", False, str(exc))

    # 5. Python SSL：httpx 走 HTTPS 依赖它，裁剪依赖时容易误删 OpenSSL
    try:
        import ssl

        ssl.create_default_context()
        note("Python SSL 可用", True)
    except Exception as exc:  # noqa: BLE001
        note("Python SSL 可用", False, str(exc))

    # 6. Qt 运行时：这是打包最容易缺件的地方
    try:
        from PySide6.QtCore import qVersion

        note("Qt 运行时加载", True, f"Qt {qVersion()}")
    except Exception as exc:  # noqa: BLE001
        note("Qt 运行时加载", False, str(exc))

    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        note("QApplication 可创建", app is not None)
    except Exception as exc:  # noqa: BLE001
        note("QApplication 可创建", False, str(exc))

    # 7. 完整界面构建（覆盖全部 Qt 依赖与样式表）
    try:
        from app.config import load_config
        from app.core.db import connect, init_db
        from app.core.dictionary import DictionaryService
        from app.core.repository import Repository
        from app.providers.mock import MockProvider
        from app.ui.main_window import MainWindow
        from app.ui.theme import stylesheet

        conn = connect(Path(os.environ["QUILL_HOME"]) / "ui.db")
        init_db(conn)
        repo = Repository(conn)
        service = DictionaryService(repo, MockProvider())
        service.lookup("ubiquitous")

        application = QApplication.instance() or QApplication([])
        application.setStyleSheet(stylesheet())
        window = MainWindow(repo, service, load_config())
        note("主窗口构建", window.list.count() == 1, f"列表 {window.list.count()} 行")
        window.deleteLater()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        note("主窗口构建", False, str(exc))

    # 8. 图标资源：验证 Quill.spec 的 datas 是否带上，且 Qt 能解析 ICO。
    #    ICO 解码依赖 imageformats 插件，裁剪 DLL 时很容易连带删掉。
    try:
        from PySide6.QtGui import QIcon

        from app.main import icon_path

        icon_file = icon_path()
        note("图标文件存在", icon_file is not None, str(icon_file))
        if icon_file is None:
            note("图标可被 Qt 解析", False, "无图标文件")
        else:
            icon = QIcon(str(icon_file))
            note(
                "图标可被 Qt 解析",
                not icon.isNull() and not icon.pixmap(32, 32).isNull(),
                f"{icon_file.stat().st_size} 字节",
            )
    except Exception as exc:  # noqa: BLE001
        note("图标文件存在", False, str(exc))
        note("图标可被 Qt 解析", False, "前置步骤失败")

    # 9. 数据库备份：数据安全的最后一道防线，打包产物里也要真跑一遍
    try:
        from app.core.db import backup_to, connect, init_db
        from app.core.models import Entry
        from app.core.repository import Repository

        home = Path(os.environ["QUILL_HOME"])
        src = connect(home / "backup_src.db")
        init_db(src)
        Repository(src).upsert(Entry(lemma="backup", definition_zh="备份"))
        saved = backup_to(src, home / "nested" / "backup_dest.db")
        probe = connect(saved)
        integrity_ok = probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        note(
            "数据库备份可用",
            integrity_ok and Repository(probe).count() == 1,
            f"{saved.stat().st_size} 字节",
        )
        probe.close()
        src.close()
    except Exception as exc:  # noqa: BLE001
        note("数据库备份可用", False, str(exc))

    # 10. 全文索引列集：列集不完整会静默削弱检索（例如搜不到备注与例句），
    #     界面上不报任何错，只有用户发现「搜不到」才会暴露，必须主动守住。
    try:
        from app.core.db import FTS_COLUMNS, connect, init_db
        from app.core.models import Entry
        from app.core.repository import Repository

        fconn = connect(Path(os.environ["QUILL_HOME"]) / "fts.db")
        init_db(fconn)
        frepo = Repository(fconn)
        frepo.upsert(
            Entry(
                lemma="probe",
                definition_zh="探针",
                example_zh="例句探针",
                notes="备注探针",
                tags="标签探针",
            )
        )
        present = tuple(r["name"] for r in fconn.execute("PRAGMA table_info(entries_fts)"))
        hit_all = all(
            frepo.search(q) for q in ("探针", "例句探针", "备注探针", "标签探针")
        )
        note(
            "全文索引覆盖完整",
            present == FTS_COLUMNS and hit_all,
            f"{len(present)} 列，检索命中={'是' if hit_all else '否'}",
        )
        fconn.close()
    except Exception as exc:  # noqa: BLE001
        note("全文索引覆盖完整", False, str(exc))

    # 11. 接口回填不得抹掉用户自有字段。「重新查询」整条覆盖内容字段，
    #     若回填时不保留备注，用户写下的东西会被静默清空且无法恢复——
    #     这类丢失只有用户事后自己发现，必须主动守住。
    try:
        from app.core.db import connect, init_db
        from app.core.dictionary import DictionaryService
        from app.core.models import Entry
        from app.core.repository import Repository

        class _Stub:
            name = "stub"

            def lookup(self, word: str) -> Entry:
                return Entry(lemma=word, definition_en="refreshed", definition_zh="刷新后")

        uconn = connect(Path(os.environ["QUILL_HOME"]) / "userfield.db")
        init_db(uconn)
        urepo = Repository(uconn)
        urepo.upsert(
            Entry(
                lemma="keep",
                definition_en="old",
                definition_zh="旧",
                notes="用户备注",
                tags="考研",
            )
        )
        DictionaryService(urepo, _Stub()).refresh("keep")
        after = urepo.get("keep")
        keep_ok = bool(
            after
            and after.notes == "用户备注"
            and after.tags == "考研"
            and after.definition_en == "refreshed"
        )
        note(
            "重新查询保留用户备注",
            keep_ok,
            f"备注={after.notes!r} 释义={after.definition_en!r}" if after else "词条缺失",
        )
        uconn.close()
    except Exception as exc:  # noqa: BLE001
        note("重新查询保留用户备注", False, str(exc))

    # 12. Markdown 导出的转义。Markdown 会把内容原样交给渲染器，用户备注里的
    #     行首结构字符会被重新解释：`-` 变列表项、`#` 变标题、`1.` 被渲染器
    #     重新编号（用户自己写的序号消失）。这类改动静默发生，必须守住。
    try:
        from app.core.exporter import export_markdown, export_text
        from app.core.models import Entry

        sample = Entry(
            lemma="x",
            definition_en="found *everywhere*",
            notes="第一行\n- 第二行\n> 第三行",
        )
        md_out = export_markdown([sample])
        escaped = (
            "found \\*everywhere\\*" in md_out
            and "\n  \\- 第二行" in md_out
            and "\n  \\> 第三行" in md_out
        )
        # 纯文本是打印路径，不能被反斜杠污染
        clean_text = "\\" not in export_text([sample])
        note(
            "Markdown 导出转义完整",
            escaped and clean_text,
            f"转义={'是' if escaped else '否'} 纯文本干净={'是' if clean_text else '否'}",
        )
    except Exception as exc:  # noqa: BLE001
        note("Markdown 导出转义完整", False, str(exc))

    # 13. 导出不得被列表显示上限截断。列表设上限是为界面性能，打印稿若沿用
    #     同一上限，文件头会自称「共 N 条」而 N 恰是被砍掉之后的数量。
    try:
        from app.core.db import connect, init_db
        from app.core.models import Entry
        from app.core.repository import Repository

        xconn = connect(Path(os.environ["QUILL_HOME"]) / "exportcap.db")
        init_db(xconn)
        xrepo = Repository(xconn)
        for i in range(4):
            xrepo.upsert(Entry(lemma=f"exportprobe{i}", definition_zh=f"导出{i}"))
        capped = len(xrepo.search("exportprobe", limit=2))
        full = len(xrepo.search("exportprobe", limit=None))
        note(
            "导出检索不受显示上限截断",
            capped == 2 and full == 4,
            f"limit=2 → {capped} 条，limit=None → {full} 条",
        )
        xconn.close()
    except Exception as exc:  # noqa: BLE001
        note("导出检索不受显示上限截断", False, str(exc))

    # 14. 后台任务的结果投递必须可靠。QThreadPool.start() 不持有 Python 包装
    #     对象的引用，包装对象一旦被回收，其 signals 随之销毁，仍排在主线程
    #     队列里的投递会全部作废——现象是「任务确实跑完了（库里都写了），
    #     界面却毫无反应」，且取决于 GC 时机，属间歇性故障。
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui.workers import submit

        qapp = QApplication.instance() or QApplication([])
        delivered: list[int] = []
        total = 80
        for i in range(total):
            submit(lambda: "ok", on_success=lambda _r, i=i: delivered.append(i))
        deadline = time.monotonic() + 6.0
        while len(delivered) < total and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        note("后台任务回调不丢失", len(delivered) == total, f"{len(delivered)}/{total}")
    except Exception as exc:  # noqa: BLE001
        note("后台任务回调不丢失", False, str(exc))

    # 15. 配置文件的容错。配置被写坏不该让程序起不来——软件打不开，
    #     用户也就没有界面可以把设置改回来了。关键点：**合法 JSON 不等于
    #     期望的对象**，`5` / `true` / `null` / `[1,2]` 会让 dict.update()
    #     抛 TypeError，而它并不被 except ValueError 覆盖。
    try:
        from app import config as config_module

        cfg_dir = Path(os.environ["QUILL_HOME"]) / "cfgprobe"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        saved_dir, saved_path = config_module.APP_DIR, config_module.CONFIG_PATH
        config_module.APP_DIR = cfg_dir
        config_module.CONFIG_PATH = cfg_dir / "config.json"
        try:
            defaults = dict(config_module.DEFAULT_CONFIG)
            ok = True
            for raw in ("5", "true", "null", "[1,2]", "{bad", ""):
                config_module.CONFIG_PATH.write_text(raw, encoding="utf-8")
                try:
                    if config_module.load_config() != defaults:
                        ok = False
                except Exception:  # noqa: BLE001
                    ok = False
            config_module.CONFIG_PATH.write_text('{"timeout": "abc"}', encoding="utf-8")
            ok = ok and config_module.load_config()["timeout"] == 30.0
        finally:
            config_module.APP_DIR, config_module.CONFIG_PATH = saved_dir, saved_path
        note("配置损坏不崩溃", ok, "非对象 JSON 与类型错误均退回默认")
    except Exception as exc:  # noqa: BLE001
        note("配置损坏不崩溃", False, str(exc))

    # 16. 既有库的补列迁移。DDL 里的 CREATE TABLE IF NOT EXISTS 对**已存在**
    #     的表不做任何事，所以「给 entries 加一列」对老用户的库不会发生。
    #     这条不变量拿 `LEGACY_ENTRIES_DDL`（迁移之前的旧表，写死的快照）造一个
    #     真·旧库，验证重新打开后**当前 DDL 的每一列**都在。
    #
    #     旧表**不能**从当前 DDL 里剔掉已登记的列来倒推——那样倒推出来的旧表仍然
    #     含有漏登记的列，于是「加了列却忘了登记」这件最要命的事判据看不见
    #     （实测：删掉或写错登记项，倒推法都报「缺列=无」，全绿）。判据方向必须
    #     是「当前 DDL 有、旧库补完还没有」——那个差集才是这一项要守的东西。
    try:
        from app.core.db import (
            LEGACY_ENTRIES_DDL,
            LEGACY_PROMPT_VERSION,
            PROMPT_VERSION_COLUMN,
            connect as _connect,
            init_db as _init,
        )
        from app.core.models import Entry

        import sqlite3 as _sqlite3

        legacy_dir = Path(os.environ["QUILL_HOME"]) / "legacycol"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "old.db"
        raw = _sqlite3.connect(str(legacy_path))
        raw.executescript(LEGACY_ENTRIES_DDL)
        raw.execute(
            "INSERT INTO entries(lemma, created_at, updated_at) VALUES('old','t','t')"
        )
        raw.commit()
        raw.close()

        # 「当前 DDL 的列集」从**一个全新库**上取，不读 ENTRIES_ADDED_COLUMNS——
        # 读登记表就等于拿被测对象当基准，漏登记又会变回看不见。
        fresh = _connect(legacy_dir / "fresh.db")
        _init(fresh)
        want = {r["name"] for r in fresh.execute("PRAGMA table_info(entries)")}
        fresh.close()

        lconn = _connect(legacy_path)
        _init(lconn)
        cols = {r["name"] for r in lconn.execute("PRAGMA table_info(entries)")}
        missing = sorted(want - cols)
        value = lconn.execute(
            f"SELECT {PROMPT_VERSION_COLUMN} AS v FROM entries WHERE lemma='old'"
        ).fetchone()["v"]
        # 「列补出来了」还不够：补出来的默认值必须能被真实的读取路径读懂。
        legacy_entry = Entry.from_row(
            lconn.execute("SELECT * FROM entries WHERE lemma='old'").fetchone()
        )
        lconn.close()
        note(
            "旧库自动补列并回填",
            (
                not missing
                and value == LEGACY_PROMPT_VERSION
                and legacy_entry.collocations == []
            ),
            f"旧库经 init_db 后仍缺 {missing or '无'}（共 {len(want)} 列），"
            f"v={value}，旧行搭配 {len(legacy_entry.collocations)} 条",
        )
    except Exception as exc:  # noqa: BLE001
        note("旧库自动补列并回填", False, str(exc))

    # 17. 提示词版本必须真的参与缓存有效性判定，否则文档里的「改了提示词
    #     必须递增版本号」就是一句无法兑现的承诺——没有任何代码读取它。
    try:
        from app.core.dictionary import DictionaryService
        from app.core.repository import Repository as _Repo
        from app.providers.mock import MockProvider
        from app.providers.prompts import PROMPT_VERSION

        vconn = _connect(Path(os.environ["QUILL_HOME"]) / "verprobe.db")
        _init(vconn)
        svc = DictionaryService(_Repo(vconn), MockProvider())
        fresh, cached = svc.lookup("meticulous")
        same, cached_again = svc.lookup("meticulous")
        vconn.execute(
            f"UPDATE entries SET {PROMPT_VERSION_COLUMN}=? WHERE id=?",
            (PROMPT_VERSION - 1, fresh.id),
        )
        vconn.commit()
        stale, _ = svc.lookup("meticulous")
        refreshed = svc.refresh("meticulous")
        vconn.close()
        note(
            "提示词版本参与缓存判定",
            cached is False
            and cached_again is True
            and not same.prompt_stale
            and stale.prompt_stale
            and bool(stale.definition_en or stale.definition_zh)
            and refreshed.prompt_version == PROMPT_VERSION
            and not refreshed.prompt_stale,
            f"stale={stale.prompt_stale} 仍可见={bool(stale.definition_en)}",
        )
    except Exception as exc:  # noqa: BLE001
        note("提示词版本参与缓存判定", False, str(exc))

    # 18. 离线词库必须随包分发。漏掉它程序不会报错，只会表现成「离线模式
    #     查什么都说未收录」——看起来像词库太小，实际是文件没被打进去。
    try:
        from app.providers.offline import offline_dict_status, resolve_offline_dict

        _offline_path = resolve_offline_dict()
        _label = offline_dict_status(_offline_path)
        note("离线词库随包分发", _offline_path is not None and "缺失" not in _label, _label)
    except Exception as exc:  # noqa: BLE001
        note("离线词库随包分发", False, str(exc))

    # 19. 兜底开关必须能随配置保存。它挂在 _COERCERS 之外时不会报错，
    #     只会表现成「关掉兜底、重启后又自己打开」——开关形同虚设。
    try:
        import app.config as _cfg_mod
        from app.providers.fallback import FallbackProvider
        from app.providers.openai_compat import build_provider

        _saved = (_cfg_mod.APP_DIR, _cfg_mod.CONFIG_PATH)
        _rt = Path(os.environ["QUILL_HOME"]) / "fallback_cfg"
        _rt.mkdir(parents=True, exist_ok=True)
        _cfg_mod.APP_DIR = _rt
        _cfg_mod.CONFIG_PATH = _rt / "config.json"
        _cfg_mod.save_config({"provider": "openai-compat", "offline_fallback": False})
        _rt_ok = _cfg_mod.load_config()["offline_fallback"] is False
        _cfg_mod.APP_DIR, _cfg_mod.CONFIG_PATH = _saved

        _base = {
            "provider": "openai-compat",
            "base_url": "https://api.example.com/v1",
            "model": "m",
        }
        _on = build_provider(dict(_base, offline_fallback=True), "sk-test")
        _off = build_provider(dict(_base, offline_fallback=False), "sk-test")
        _wired = isinstance(_on, FallbackProvider) and not isinstance(
            _off, FallbackProvider
        )
        note("兜底开关可保存且真正生效", _rt_ok and _wired, f"落盘={_rt_ok} 接线={_wired}")
    except Exception as exc:  # noqa: BLE001
        note("兜底开关可保存且真正生效", False, str(exc))

    # 20. 标签：归一 + 按标签筛选精确匹配。
    #     **刻意不写成「upsert 之后 tags 一定是单空格形态」**——那是库里并不存在
    #     的保证。归一发生在**界面写入那一步**（用户敲的值先过 `join_tags` 再落库），
    #     而导入路径允许库里存在逗号形态的标签，`Entry.tag_list` 之所以要自己解析，
    #     正是因为两种形态会共存。第一版就是这么写的，当场 FAIL 并打印出
    #     `存储形态='考研,  高频'`——抓的是断言本身写错了，不是实现有问题。
    #     真正要守的是下面这条与存储形态无关的性质：筛「考研」不把「考研英语」
    #     带出来。子串匹配就会带出来，而它不报错、只是多几行。
    try:
        from app.core.db import connect as _conn20, init_db as _init20
        from app.core.models import Entry as _Entry20, join_tags
        from app.core.repository import Repository as _Repo20

        tconn = _conn20(Path(os.environ["QUILL_HOME"]) / "tagprobe.db")
        _init20(tconn)
        trepo = _Repo20(tconn)
        # 界面写入路径：用户敲「考研,  高频」→ 归一 → 落库
        trepo.upsert(
            _Entry20(
                lemma="tagprobe1",
                definition_zh="标签探针一",
                tags=join_tags("考研,  高频"),
            )
        )
        # 导入路径：外部文件的标签怎么分隔不可控，这里就是逗号形态
        trepo.upsert(
            _Entry20(lemma="tagprobe2", definition_zh="标签探针二", tags="考研英语,高频")
        )
        stored = trepo.get("tagprobe1")
        exact = sorted(e.lemma for e in trepo.list_by_tag("考研"))
        tconn.close()
        note(
            "标签归一且按标签筛选精确匹配",
            # ① 归一：界面写入的形态是确定的单空格
            # ② 精确：LIKE 粗筛会把两条都捞上来，必须靠精判只留下带「考研」的那条
            getattr(stored, "tags", None) == "考研 高频" and exact == ["tagprobe1"],
            # 用 getattr 而不是 `stored.tags`：词条缺失时不让断言自己抛异常，
            # 否则自检会在这一项上炸掉，同时把后面所有项一起带走。
            f"界面写入={getattr(stored, 'tags', '词条缺失')!r} 筛「考研」→ {exact}",
        )
    except Exception as exc:  # noqa: BLE001
        note("标签归一且按标签筛选精确匹配", False, str(exc))

    # 21. Anki 制表符文本导出。它是最容易「看着导出成功、其实列错位」的一种格式：
    #     Anki 不校验列数、也不报错，用户要到背卡时才发觉串列。期望值按**外部
    #     契约**硬写在这里——若从被测代码里取（例如读它的列名常量），改了实现
    #     断言会自动跟着改，等于没守。
    try:
        from app.core.exporter import export_anki
        from app.core.models import Entry as _Entry21

        aout = export_anki(
            [
                _Entry21(
                    lemma="read",
                    definition_en="read < write",
                    definition_zh="读",
                    tags="考研",
                ),
                # 正面全是空白的词条必须被跳过，否则会产出一张没有正面的卡片。
                _Entry21(lemma="   ", definition_zh="没有正面"),
            ]
        )
        alines = aout.splitlines()
        adata = [ln for ln in alines if ln and not ln.startswith("#")]
        cols_ok = all(len(ln.split("\t")) == 3 for ln in adata)
        note(
            "Anki 导出列数与转义正确",
            (
                # 不带 BOM：Anki 会把它当成第一个字段的第一个字符。
                not aout.encode("utf-8").startswith(b"\xef\xbb\xbf")
                and alines[:3] == ["#separator:tab", "#html:true", "#tags column:3"]
                and len(adata) == 1
                and cols_ok
                and "read &lt; write" in adata[0]
            ),
            f"{len(adata)} 张卡片，列数={'对' if cols_ok else '错'}",
        )
    except Exception as exc:  # noqa: BLE001
        note("Anki 导出列数与转义正确", False, str(exc))

    # 22. 词组收藏：第二张表。它要守三件事，三件都是「不做也不会报错」的：
    #     ① 表真的建出来了——漏掉建表语句只表现成「点了收藏没反应」；
    #     ② 两张表的行号**各自从 1 发号**——同一个数字在两边毫不相干，
    #        任何「拿词条的 id 去删词组」（或反过来）都会安静地删掉另一样东西；
    #     ③ 重复收藏是幂等的，且**不覆盖**已经收下的那份内容（用户手动改过的
    #        那条不能被下一次收藏静默改写，而两次操作在界面上都显示「已收藏」）。
    try:
        from app.core.db import connect as _connect, init_db as _init
        from app.core.models import Entry as _Entry22, Phrase as _Phrase22
        from app.core.repository import Repository as _Repo22

        pconn = _connect(Path(os.environ["QUILL_HOME"]) / "phrases.db")
        _init(pconn)
        prepo = _Repo22(pconn)
        word = prepo.upsert(_Entry22(lemma="charge", definition_zh="收费"))
        first = prepo.add_phrase(_Phrase22(phrase="be in charge of", gloss_zh="负责"))
        prepo.add_phrase(_Phrase22(phrase="take charge of", gloss_zh="接管"))
        twice = prepo.add_phrase(
            _Phrase22(phrase="be in charge of", gloss_zh="本该被丢掉的那份")
        )
        cols = {r["name"] for r in pconn.execute("PRAGMA table_info(phrases)")}
        # 删一条词组，词条必须还在——两张表之间没有任何连带。
        prepo.delete_phrase(first.id)
        word_survived = prepo.get("charge") is not None
        word_count = prepo.count()
        phrase_count = prepo.count_phrases()
        pconn.close()
        note(
            "词组收藏表独立且幂等",
            (
                {"phrase", "gloss_zh", "example", "example_zh", "source_lemma"} <= cols
                # 词条 1 号与词组 1 号并存：两张表各自从 1 发号
                and word.id == 1
                and first.id == 1
                # 幂等：条数不涨；不覆盖：留下的仍是先收的那份简释
                and twice.id == first.id
                and twice.gloss_zh == "负责"
                # 删词组不动词条
                and word_survived
                and word_count == 1
                and phrase_count == 1
            ),
            f"词条 id={word.id} 词组 id={first.id}、重复收藏后简释="
            f"{twice.gloss_zh!r}，删词组后词条={word_count} 条、词组={phrase_count} 条",
        )
    except Exception as exc:  # noqa: BLE001
        note("词组收藏表独立且幂等", False, str(exc))

    summary = f"自检结果：{len(lines) - failed} 项通过，{failed} 项失败"
    lines.append("")
    lines.append(summary)

    payload = "\n".join(lines)
    try:
        # 按字节写，不用 write_text：Windows 上 write_text 会把 \n 翻译成
        # \r\n，于是一份本该全 LF 的仓库里凭空多出一个 CRLF 文件——
        # selfcheck 的行尾扫描会如实把它报出来。生成物也不该例外。
        _report_path().write_bytes(payload.encode("utf-8"))
    except OSError:
        pass
    print(payload)
    print(f"\n结果已写入 {_report_path()}")
    return 1 if failed else 0


def main() -> int:
    if "--selfcheck" in sys.argv:
        return run_selfcheck()

    from app.main import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())

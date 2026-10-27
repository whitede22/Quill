# -*- mode: python ; coding: utf-8 -*-
# spec 由 PyInstaller 以 exec() 执行，命名空间里只注入 DISTPATH / SPECPATH /
# workpath 等少数变量，标准库需自行导入。
import json
import os
"""PyInstaller 打包配置。

体积优化集中在 DLL 裁剪：PySide6 的 hook 会把整套 Qt 动态库收进来，
但本应用只用到 QtCore / QtGui / QtWidgets。

两条用实测换来的规则，改这份清单前务必先读：

一、删库必须连同「孤立插件」一起删。
    Qt 插件（iconengines / imageformats / virtualkeyboard 等）会引用对应的
    Qt 库。只删库不删插件，就会留下断裂的依赖——程序不会报错退出，而是
    直接起不来窗口，而 --selfcheck 依然全绿（自检不进入渲染路径，测不出来）。
    例如 qsvgicon.dll 依赖 qt6svg.dll、qpdf.dll 依赖 qt6pdf.dll。
    改动本清单后，请重新打包并运行：
        .venv/Scripts/python.exe scripts/check_trim.py dist/Quill
    校验依据是打包时写出的 dist/trim_manifest.json（见文件末尾说明）。

二、以下几项经实测不可移除：
    - libcrypto-3.dll / libssl-3.dll
      看似只服务 Qt WebEngine，但 Python 3.12+ 的 Windows 官方构建已改为
      动态链接 OpenSSL，_ssl.pyd 依赖它们。移除后 httpx 的 HTTPS 全部失败。
    - qt6network.dll
      generic 目录下的触摸输入插件依赖它，移除会导致启动异常。

    --selfcheck 中的「Python SSL 可用」一项用于守住 OpenSSL 这条底线。
"""

EXCLUDED_MODULES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    # QtNetwork 不能排除：单实例唤醒走 QLocalServer / QLocalSocket（见
    # app/main.py 的 notify_running_instance）。托盘常驻之后窗口可能藏在托盘里，
    # 用户再双击图标时需要由第二个进程把第一个叫到前台——没有这条通道，
    # 双击只会得到一句「已在运行」然后什么都不发生。
    # 底层的 qt6network.dll 本来就在保留清单里（generic 触摸插件依赖它），
    # 这里放开的只是 Python 绑定。
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer",
    "tkinter",
    "unittest",
    "doctest",
    "pydoc",
    "pdb",
    "pip",
    "setuptools",
    "wheel",
    "PIL",
    "numpy",
]

EXCLUDED_DLLS = (
    # 软件 OpenGL 回退实现，单文件 20.6MB。已实测：移除后本机可正常启动。
    "opengl32sw.dll",
    # QML / Quick 运行时（本应用全部使用 QWidget）
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6qmlmeta.dll",
    "qt6qmlcompiler.dll",
    "qt6qmldebug.dll",
    "qt6qmllocalstorage.dll",
    "qt6quick.dll",
    "qt6quick3d.dll",
    "qt6quickcontrols2.dll",
    "qt6quicktemplates2.dll",
    "qt6quickwidgets.dll",
    # 其余未使用的 Qt 模块（qt6network 与 OpenSSL 相关库不在此列，见文件头说明）
    "qt6pdf.dll",
    "qt6opengl.dll",
    "qt6openglwidgets.dll",
    "qt6svg.dll",
    "qt6sql.dll",
    "qt6test.dll",
    "qt6designer.dll",
    "qt6help.dll",
    "qt6uitools.dll",
    "qt6multimedia.dll",
    "qt6charts.dll",
    "qt6datavisualization.dll",
    "qt6graphs.dll",
    "qt6bluetooth.dll",
    "qt6nfc.dll",
    "qt6positioning.dll",
    "qt6location.dll",
    "qt6sensors.dll",
    "qt6serialport.dll",
    "qt6websockets.dll",
    "qt6webenginecore.dll",
    "qt6webenginewidgets.dll",
    "qt6webchannel.dll",
    "qt6texttospeech.dll",
    "qt6remoteobjects.dll",
    "qt6scxml.dll",
    "qt6statemachine.dll",
    "qt6httpserver.dll",
    "qt6spatialaudio.dll",
    "qt6virtualkeyboard.dll",
    # 上述库对应的孤立插件，必须连同删除，否则会留下断裂依赖（见文件头说明）
    "qsvg.dll",
    "qsvgicon.dll",
    "qpdf.dll",
    "qtvirtualkeyboardplugin.dll",
)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    # 图标既是 exe 的资源（EXE 的 icon 参数），也是窗口图标（main.icon_path 在
    # 打包后按 assets/quill.ico 查找）。漏掉 datas 的话 exe 有图标、窗口没有。
    #
    # 离线词库同理，而且更隐蔽：漏掉它程序不会报错，只会表现成
    # 「离线模式查什么都说未收录」——看起来像词库太小，实际是文件没被打进去。
    datas=[
        ("assets/quill.ico", "assets"),
        ("assets/offline_dict.db", "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    noarchive=False,
)

# 剔除确定用不到的 Qt 库与孤立插件。name 形如
# "PySide6\\Qt6Quick.dll" 或 "PySide6\\plugins\\imageformats\\qsvg.dll"，
# 用文件名后缀匹配即可覆盖不同目录层级。
_pre_trim = list(a.binaries)
a.binaries = [
    item
    for item in _pre_trim
    if not item[0].lower().endswith(EXCLUDED_DLLS)
    and "qt6labs" not in item[0].lower()
]

# 把「本次删掉了什么」写成清单。这不是调试信息，而是裁剪校验的**唯一依据**：
# 裁剪发生在 COLLECT 内部，磁盘上从来不存在「未裁剪产物」，所以
# scripts/check_trim.py 无法靠对比两个目录得出「删了什么」——这正是它长期
# 只能输出「两者没有差异，无需校验」的原因，一个永远通过的校验等于没有校验。
#
# 清单**只记被删库名**，刻意不记「保留了多少个」：本处能看到的是 a.binaries，
# 而产物里还有 COLLECT 另加的 Quill.exe，且不同目录下存在同名文件——任何
# 「数量相等」的判据都要在两处各写一遍定义，必然对不齐（初版就是这样误报的）。
# 改用的判据是定义无关的：**记录为已删除的名字，不得出现在产物中**。
#
# 落在 DISTPATH 而非 DISTPATH/Quill：后者才是可拷贝分发的单元，不混入构建产物。
try:
    _kept = {item[0] for item in a.binaries}
    _removed = sorted(
        {
            item[0].replace("\\", "/").split("/")[-1].lower()
            for item in _pre_trim
            if item[0] not in _kept
        }
    )
    if not os.path.isdir(DISTPATH):
        os.makedirs(DISTPATH, exist_ok=True)
    with open(
        os.path.join(DISTPATH, "trim_manifest.json"), "w", encoding="utf-8"
    ) as _fh:
        json.dump({"removed": _removed}, _fh, ensure_ascii=False, indent=2)
except Exception as _exc:  # noqa: BLE001
    # 记录失败不该让打包失败，但必须喊出来——静默缺清单会让校验退化成走过场
    print(f"[Quill.spec] 警告：裁剪清单写入失败，check_trim 将无依据可校：{_exc}")


pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Quill",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/quill.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Quill",
)

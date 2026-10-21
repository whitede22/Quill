"""构建免安装程序。

用法：
    .venv/Scripts/python.exe build.py

产物：dist/Quill/ —— 整个目录可直接拷贝分发，双击 Quill.exe 运行。
体积裁剪规则集中在 Quill.spec 中维护。

关于旧产物清理：本脚本不使用递归删除。运行环境对批量删除设有保护阈值，
而 PyInstaller 需要清空的文件数远超该阈值，直接删除会被拦截导致构建失败。
因此改为「让路」策略——把已存在的 dist / build 重命名为带时间戳的备份，
让 PyInstaller 在干净路径上工作。构建完成后可自行删除这些 *_old_* 目录。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "Quill.spec"
NAME = "Quill"


def directory_size(path: Path) -> float:
    """返回目录体积（MB）。"""
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / 1024 / 1024


def park(path: Path) -> Path | None:
    """把已存在的产物目录改名让路，避免触发批量删除保护。

    目录**本身**可能改不动：Windows 报 `WinError 5 拒绝访问`，而那并不是权限
    问题（提权后一样失败），是别的进程持着句柄造成的共享冲突。让路一旦失败，
    PyInstaller 的 COLLECT 阶段就会去删掉已存在的产物目录，而「一次删掉上百个
    文件」正好撞上本环境的批量删除保护——构建最后以一个与真正原因毫不相干的
    错误收场，排查方向被带偏。

    因此让路失败时退一步：**把里面的条目逐个挪进旧产物目录**，让目标目录空出来。
    空目录不需要删任何东西，COLLECT 直接往里写就行。
    """
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}_old_{stamp}")
    try:
        path.rename(target)
    except OSError as exc:
        print(f"无法为 {path.name} 让路（{exc.strerror}），改为逐个挪出内容")
        try:
            target.mkdir(parents=True, exist_ok=True)
            for child in list(path.iterdir()):
                child.rename(target / child.name)
        except OSError as inner:
            print(f"挪出内容也失败：{inner}")
            return None
        leftovers = list(path.iterdir()) if path.exists() else []
        if leftovers:
            print(f"{path.name} 内仍有 {len(leftovers)} 个条目未挪出")
            return None
        print(f"旧产物已让路（内容已挪出）：{target.name}")
        return target
    print(f"旧产物已让路：{target.name}")
    return target


def ensure_assets() -> None:
    """图标是构建输入之一。缺失时先按脚本生成，避免打出无图标的 exe。

    图标同时被 Quill.spec 的 icon 参数与 datas 引用，缺文件时 PyInstaller
    会构建失败或产出无图标程序，因此这里先行补齐。
    """
    if (ROOT / "assets" / "quill.ico").exists():
        return
    print("未找到 assets/quill.ico，正在生成…")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_icon.py")], cwd=ROOT
    )
    if result.returncode != 0:
        print("图标生成失败，请手动运行 scripts/make_icon.py")
        raise SystemExit(1)


def main() -> int:
    if not SPEC.exists():
        print(f"未找到打包配置：{SPEC}")
        return 1

    ensure_assets()

    parked: list[Path] = []
    for stale in (DIST, BUILD):
        moved = park(stale)
        if moved is not None:
            parked.append(moved)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--distpath={DIST}",
        f"--workpath={BUILD}",
        str(SPEC),
    ]

    print("开始构建…")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n构建失败，退出码 {result.returncode}")
        return result.returncode

    target = DIST / NAME
    exe = target / f"{NAME}.exe"
    if not exe.exists():
        print(f"未找到产物：{exe}")
        return 1

    print(f"\n构建完成：{target}")
    print(f"目录体积：{directory_size(target):.1f} MB")
    print(f"可执行文件：{exe}")
    print("\n请务必执行下一步，确认裁剪后未缺件：")
    print(f'  "{exe}" --selfcheck')
    # 裁剪校验必须真的能跑：清单由 Quill.spec 在打包时写出，因此这里只需要
    # 一个目录参数。若清单缺失，check_trim 会明确报错而不是输出「通过」。
    print("\n并校验裁剪依赖（清单已由本次打包写出）：")
    print(f'  "{sys.executable}" scripts/check_trim.py "{target}"')

    if parked:
        print("\n以下旧产物已保留，确认新版本可用后可自行删除：")
        for item in parked:
            print(f"  {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""裁剪校验工具：找出「已被移除但仍被引用」的动态库。

这类缺失极其隐蔽：程序可能仍能启动，只在触发某个功能时才静默崩溃；
反过来也可能像本项目遇到的那样，--selfcheck 全部通过却无法显示窗口
（因为自检不进入渲染路径）。因此每次调整裁剪规则后都必须运行本工具。

用法：
    .venv/Scripts/python.exe scripts/check_trim.py dist/Quill
    .venv/Scripts/python.exe scripts/check_trim.py dist/Quill --manifest 路径
    .venv/Scripts/python.exe scripts/check_trim.py <未裁剪目录> <裁剪目录>

**为什么需要一份裁剪清单**
本流程的裁剪规则内联在 Quill.spec 的 `a.binaries` 筛选中，执行于 COLLECT
阶段——磁盘上任何时候都不存在「未裁剪产物」。所以早期那种「对比两个目录求出
删了什么」的做法在本项目里根本无法使用：无论传哪两个目录，算出的差异集都是空，
工具永远输出「两者没有差异，无需校验」。**一个不可能失败的校验，比没有校验更糟**，
因为它会让人以为这一步已经做过了。

现在 Quill.spec 会在裁剪的同时把被删库名写进 DISTPATH/trim_manifest.json，
本工具据此校验。用法从「两个目录」变成了「一个目录 + 它自己的清单」。

**清单刻意只记录被删的名字，不记录任何数量。**
「保留了多少个」在两个地方各有各的口径：spec 只看得见 `a.binaries`，而产物里还有
COLLECT 另加的 `Quill.exe`，且不同目录下存在同名文件。初版就是因为拿这两个数
直接比大小而误报。现在使用的判据是**定义无关**的：

  记录为「已删除」的名字，不得出现在产物中。

本工具每次运行都会做三项检查，且**每一项都真的可能失败**：
  1. 清单与产物是否对得上（已删名字若仍存在，说明清单不是这份产物的）
  2. 已删名字是否仍被产物中的文件引用（这才是原本要防的故障）
  3. **检测器自证**：在产物内部取一对真实依赖（A 导入 B），把 B 假扮成「已移除」
     再跑一遍，必须报出 A；报不出来说明检测器是瞎的，直接判失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pefile

BINARY_SUFFIXES = {".dll", ".pyd", ".exe"}

DEFAULT_MANIFEST = "trim_manifest.json"


def collect(root: Path) -> dict[str, list[Path]]:
    """收集产物中的二进制文件，按文件名归类。

    刻意返回「名字 -> 路径列表」而不是「名字 -> 路径」：不同目录下存在同名文件
    （PySide6 的插件与库），用字典直接覆盖会**静默漏掉若干个文件**，而产物文件数
    随即变成一个说不清口径的数字——初版就是这样把 56 个数成了 51，并据此误报
    「清单与产物不匹配」。数量要么按真实文件数报，要么不报。
    """
    grouped: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in BINARY_SUFFIXES:
            grouped.setdefault(path.name.lower(), []).append(path)
    return grouped


def file_count(grouped: dict[str, list[Path]]) -> int:
    return sum(len(paths) for paths in grouped.values())


def imports_of(path: Path) -> set[str]:
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        names = {
            entry.dll.decode("ascii", "ignore").lower()
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
            if getattr(entry, "dll", None)
        }
        pe.close()
        return names
    except Exception:  # noqa: BLE001 - 非 PE 或解析失败一律忽略
        return set()


def dependents(
    grouped: dict[str, list[Path]], candidates: set[str]
) -> dict[str, set[str]]:
    """找出仍引用 candidates 里任何一项的文件。

    返回 {被引用者: {引用者文件名, ...}}，只包含真正出现引用的项。
    """
    found: dict[str, set[str]] = {}
    for name, paths in grouped.items():
        for path in paths:
            for dep in imports_of(path):
                if dep in candidates:
                    found.setdefault(dep, set()).add(name)
    return found


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到裁剪清单：{path}\n"
            "清单由 Quill.spec 在打包时写出。若你是手工运行 pyinstaller，"
            "请改用「两个目录」模式并自行准备未裁剪产物作基线。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def self_proof(grouped: dict[str, list[Path]]) -> tuple[bool, str]:
    """证明检测器不是瞎的：拿产物内部一对真实依赖做注入测试。

    选一对「A 导入 B 且两者都在产物里」，把 B 当作「已移除」喂给检测逻辑，
    必须报出 A。找不到这样的依赖对时返回失败——此时本工具什么都证明不了，
    不该给出「通过」的假信号。
    """
    for name, paths in sorted(grouped.items()):
        for path in paths:
            for dep in sorted(imports_of(path)):
                if dep in grouped and dep != name:
                    found = dependents(grouped, {dep})
                    if name in found.get(dep, set()):
                        return True, f"注入「已移除」={dep} → 正确报出引用者 {name}"
    return False, "产物内部找不到任何「A 导入 B」的真实依赖，检测器无从自证"


def manifest_mode(dist_root: Path, manifest_path: Path) -> int:
    grouped = collect(dist_root)
    manifest = load_manifest(manifest_path)
    removed = {str(n).lower() for n in manifest.get("removed", [])}
    names_in_product = set(grouped)

    print(f"裁剪产物：{dist_root}")
    print(f"裁剪清单：{manifest_path}")
    print(
        f"产物内二进制：{file_count(grouped)} 个文件 / {len(names_in_product)} 个不同文件名"
    )
    print(f"清单记录已移除：{len(removed)} 个\n")

    ok = True

    # 检查 1：清单描述的是不是这份产物。
    # 判据定义无关——「已删除」的名字不可能同时存在于产物里，一旦存在就说明
    # 这份清单不是为它生成的（例如手工复制了上一轮的清单）。
    still_there = sorted(removed & names_in_product)
    if still_there:
        print("!! 清单记录为已删除、却仍存在于产物中的文件：")
        for name in still_there:
            print(f"  {name}")
        print("   这份清单多半不是为当前产物生成的，请重新打包。\n")
        ok = False

    # 检查 2：真正要防的故障——删了库，但还有文件引用它
    print("扫描导入表…\n")
    referenced = dependents(grouped, removed)
    if referenced:
        print("!! 以下动态库已被移除，但仍被引用 —— 必须从 spec 的排除清单中移除：")
        for dep, users in sorted(referenced.items()):
            print(f"  {dep}")
            for user in sorted(users):
                print(f"      <- {user}")
        print()
        ok = False
    else:
        print("未发现「已移除但仍被引用」的库。\n")

    # 检查 3：自证检测能力，避免以「一个瞎掉的检测器」宣布通过
    verifiable, detail = self_proof(grouped)
    print(f"检测器自证：{'通过' if verifiable else '失败'} — {detail}")
    if not verifiable:
        ok = False

    return 0 if ok else 1


def diff_mode(old_root: Path, new_root: Path) -> int:
    """兼容模式：手工对比「未裁剪」与「裁剪后」两个目录。"""
    old_names = set(collect(old_root))
    new_grouped = collect(new_root)
    removed = sorted(old_names - set(new_grouped))

    print(f"未裁剪产物：{file_count(collect(old_root))} 个二进制文件")
    print(f"裁剪后产物：{file_count(new_grouped)} 个二进制文件")
    print(f"本次共移除：{len(removed)} 个不同文件名\n")

    if not removed:
        print("两者没有差异，无需校验")
        return 0

    print("扫描导入表…\n")
    referenced = dependents(new_grouped, set(removed))
    if referenced:
        print("!! 以下动态库已被移除，但仍被引用 —— 必须从 spec 的排除清单中移除：")
        for dep, users in sorted(referenced.items()):
            print(f"  {dep}")
        return 1

    print("未发现「已移除但仍被引用」的库。")
    return 0


def main() -> int:
    args = list(sys.argv[1:])
    manifest_arg = None
    if "--manifest" in args:
        i = args.index("--manifest")
        if i + 1 >= len(args):
            print("--manifest 需要一个路径参数")
            return 2
        manifest_arg = args[i + 1]
        del args[i : i + 2]

    if len(args) == 1:
        dist_root = Path(args[0])
        if not dist_root.is_dir():
            print(f"目录不存在：{dist_root}")
            return 2
        manifest_path = (
            Path(manifest_arg) if manifest_arg else dist_root.parent / DEFAULT_MANIFEST
        )
        try:
            return manifest_mode(dist_root, manifest_path)
        except FileNotFoundError as exc:
            print(f"!! {exc}")
            return 2

    if len(args) == 2:
        old_root, new_root = Path(args[0]), Path(args[1])
        if not old_root.is_dir() or not new_root.is_dir():
            print("目录不存在")
            return 2
        return diff_mode(old_root, new_root)

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

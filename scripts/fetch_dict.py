"""下载随包分发的离线词库。

词库 64.8 MB，不进版本库（GitHub 对单文件建议上限 50 MB），改由 Release
资产托管。克隆仓库之后、运行或打包之前先跑一次本脚本。

    python scripts/fetch_dict.py          下载并校验
    python scripts/fetch_dict.py --check  只校验本地文件，不下载（CI 用）

校验用的是 sha256，与发布时算出的值一致；不匹配就删除并报错，不留半份
可用但内容不对的词库——那种文件最坏，程序照常打开、查出来的释义是错的。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.providers.offline import OFFLINE_DB_NAME  # noqa: E402

# 词库放在独立 tag 的 Release 里，与版本号解耦：换词库不必发版，
# 发版也不必重传这 64.8 MB。
DICT_TAG = "dict-v2"
DICT_URL = f"https://github.com/whitede22/Quill/releases/download/{DICT_TAG}/{OFFLINE_DB_NAME}"

# 全量档（769,435 词）的指纹。换成新词库时同步改这里和 DICT_TAG。
DICT_SHA256 = "f58112a18cdce77a86ea00e2d50acee8da4ae8b767e5a65d72e3d2708d1fe4f2"

DEST = ROOT / "assets" / OFFLINE_DB_NAME
CHUNK = 1 << 20
ATTEMPTS = 3


def digest_of(path: Path) -> str:
    """按块算 sha256。整份读进内存要 64 MB，没必要。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def local_ready() -> bool:
    return DEST.exists() and digest_of(DEST) == DICT_SHA256


def open_url(request: urllib.request.Request, use_proxy: bool):
    """按是否走系统代理取一个响应。

    **代理不是可选项，是故障源。** 环境里配了代理而代理本身打不通时，
    urllib 会老老实实走它，然后拿到 502——看起来像 GitHub 挂了，其实是
    本地代理坏了。所以重试到第二轮起强制直连。
    """
    handlers = [] if use_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=60)


def download_once(target: Path, use_proxy: bool = True) -> None:
    """下载一次到 target。失败直接抛出，由调用方决定是否重试。"""
    request = urllib.request.Request(
        DICT_URL, headers={"User-Agent": "quill-fetch-dict"}
    )
    with open_url(request, use_proxy) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        step = max(total // 10, CHUNK)
        next_mark = step
        with target.open("wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if done >= next_mark:
                    share = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
                    print(f"  已下载 {share}", flush=True)
                    next_mark += step
    if total and done != total:
        raise OSError(f"下载不完整：收到 {done} 字节，应为 {total} 字节")


def fetch() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    partial = DEST.with_name(DEST.name + ".part")
    problems: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        use_proxy = attempt == 1
        try:
            route = "系统代理" if use_proxy else "直连"
            print(f"第 {attempt}/{ATTEMPTS} 次下载（{route}）{DICT_URL}")
            download_once(partial, use_proxy)
            actual = digest_of(partial)
            if actual != DICT_SHA256:
                partial.unlink(missing_ok=True)
                problems.append(f"校验不符：实得 {actual}，应为 {DICT_SHA256}")
                continue
            partial.replace(DEST)
            print(f"已就绪：{DEST}（{DEST.stat().st_size:,} 字节）")
            return 0
        except (urllib.error.URLError, OSError) as exc:
            partial.unlink(missing_ok=True)
            problems.append(f"{type(exc).__name__}: {exc}")
    print("下载失败，三次都没成：", file=sys.stderr)
    for line in problems:
        print("  " + line, file=sys.stderr)
    print(
        "\n网络不通时可以自己构建：.venv/Scripts/python.exe "
        "scripts/build_offline_dict.py --tier full",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并校验离线词库")
    parser.add_argument(
        "--check", action="store_true", help="只校验本地文件，缺失就报错，不下载"
    )
    args = parser.parse_args()

    if local_ready():
        print(f"词库已就绪：{DEST}（{DEST.stat().st_size:,} 字节）")
        return 0
    if DEST.exists():
        print(f"本地词库校验不符，将重新下载：{DEST}")
    if args.check:
        print(f"缺少可用的离线词库：{DEST}", file=sys.stderr)
        print("先运行 python scripts/fetch_dict.py", file=sys.stderr)
        return 1
    return fetch()


if __name__ == "__main__":
    raise SystemExit(main())

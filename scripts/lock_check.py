"""单实例锁验证。

只能用两个真实进程来测：文件锁的语义是「另一个进程拿不到」，
在同一个进程里反复调用无法证明这一点。

用法：.venv/Scripts/python.exe scripts/lock_check.py
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHILD_CODE = (
    "import sys, time;"
    "sys.path.insert(0, sys.argv[1]);"
    "from app.main import acquire_instance_lock;"
    "h = acquire_instance_lock();"
    "print('LOCKED' if h else 'BUSY', flush=True);"
    "time.sleep(float(sys.argv[2]))"
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  {detail}")


def main() -> int:
    home = tempfile.mkdtemp(prefix="quill_lock_")
    # 用完删掉：见 selfcheck.py 里那段关于 3763 个残留目录的说明。
    os.environ["QUILL_HOME"] = home  # 必须在导入 app.config 之前设置

    from app.main import acquire_instance_lock, LOCK_PATH  # noqa: E402

    env = {**os.environ, "QUILL_HOME": home}
    holder = subprocess.Popen(
        [sys.executable, "-c", CHILD_CODE, str(ROOT), "6.0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        state = holder.stdout.readline().strip()
        check("子进程取得锁", state == "LOCKED", f"子进程返回 {state!r}")

        second = acquire_instance_lock()
        check("第二个实例被拒绝", second is None, f"返回 {second!r}")
        if second is not None:
            second.close()

        lock_file = LOCK_PATH
        check("锁文件位于数据目录", lock_file.exists())
        # 回归守卫：曾经因为往锁文件里写字，导致第二个实例写入时撞上
        # 第一个实例持有的字节区间，启动直接崩在 PermissionError。
        check("锁文件内容为空", lock_file.stat().st_size == 0, f"{lock_file.stat().st_size} 字节")

        holder.wait(timeout=20)

        # 进程退出后操作系统释放字节锁，但强杀场景下释放有极短延迟，
        # 因此重试若干次而不是一次断言。
        third = None
        for _ in range(10):
            third = acquire_instance_lock()
            if third is not None:
                break
            time.sleep(0.3)
        check("前一个实例退出后可再启动", third is not None, f"返回 {third!r}")
        if third is not None:
            third.close()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        # **不要 ignore_errors=True**：那个子进程刚退出，它的锁文件句柄可能
        # 还差一点点没释放，一次 rmtree 会失败。而 ignore_errors 会让「删干净了」
        # 与「静默没删」长得一模一样——实测就是这么攒下 440 个 quill_lock_*
        # 目录的（每个 1.8 MB 的目录项占用，看着不多，但架不住每次跑都留一个）。
        # 重试几次，仍失败就打印出来。
        for _attempt in range(5):
            try:
                shutil.rmtree(home)
                break
            except OSError:
                gc.collect()
                time.sleep(0.1 * (_attempt + 1))
        else:
            print(f"  WARN  锁目录未能删除（不影响结论）：{home}", flush=True)

    print("=" * 56)
    for name in PASSED:
        print(f"  PASS  {name}")
    for name in FAILED:
        print(f"  FAIL  {name}")
    print("=" * 56)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

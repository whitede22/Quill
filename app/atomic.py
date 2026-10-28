"""原子落盘：同目录临时文件 → `os.replace`。

为什么要单独成一个模块：配置文件和密钥文件都必须做到「要么是旧的完整内容，
要么是新的完整内容」，绝不能留下半截。两处各写一遍的话，早晚有一处忘了清理
临时文件，或者忘了 `os.replace` 只替换目录项这一点。

`os.replace` 在同一个文件系统内是原子的；临时文件必须与目标同目录，跨目录
替换可能退化成「复制 + 删除」，那就不再是原子的了。
"""

from __future__ import annotations

import os
from pathlib import Path


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """把 `text` 原子写入 `path`。

    失败时删掉临时文件并原样抛出 `OSError`——调用方需要知道这次没写成，
    静默失败会让用户以为设置已经保存。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

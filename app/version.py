"""版本号的唯一来源。

应用内此前没有版本常量，版本只活在 CHANGELOG、git tag 与包名里。使用报告要
写 `app_version`，而报告是拿来横向对比的数据文件，版本号写错会让整份数据失去
对照价值，所以这里落一个常量。

两处定义必然有一天会不一致，因此 `selfcheck` 里有一条交叉校验：本文件与
`CHANGELOG.md` 首条版本必须相同，不一致就报红。发版顺序固定为
改 CHANGELOG → 改这里 → 打 tag。
"""

from __future__ import annotations

APP_VERSION = "1.3.1"

# 仓库地址。报告提交、README 链接、Issue 模板都从这里取，避免同一串地址
# 在代码里出现多份——换仓库时只改一处。
REPO_URL = "https://github.com/whitede22/Quill"

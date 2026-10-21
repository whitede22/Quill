"""生成应用图标。

用 QPainter 绘制图标并组装成 ICO。纯 Qt 实现，不引入 Pillow：
ICO 自 Vista 起支持内嵌 PNG，因此只需手写 6 字节文件头 + 每个尺寸
16 字节目录项，再拼接 PNG 数据即可。

用法：.venv/Scripts/python.exe scripts/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICO_PATH = ASSETS / "quill.ico"

# 覆盖任务栏、资源管理器、标题栏等场景所需的最小尺寸集合
SIZES = (256, 64, 48, 32, 16)

ACCENT = "#4A6FA5"


def render(size: int) -> QImage:
    """绘制单尺寸图标：主色圆角方块 + 白色字母 Q。"""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    radius = size * 0.23
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    font = QFont("Segoe UI")
    # Q 是圆的，同样字号下视觉面积比 L 小一圈；放大到 0.66 才与原来「L」
    # 在方块里的分量相当。上移量也跟着减小：Q 的尾巴向下伸出，再压上去
    # 会让它贴着下边缘。
    font.setPixelSize(int(size * 0.66))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    # 略微上移以补偿字母的视觉重心
    painter.drawText(
        QRectF(0, -size * 0.02, size, size),
        Qt.AlignmentFlag.AlignCenter,
        "Q",
    )
    painter.end()
    return image


def to_png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    payload = bytes(buffer.data())
    buffer.close()
    return payload


def write_ico(path: Path, frames: list[tuple[int, bytes]]) -> None:
    """组装 ICO：文件头 + 目录项 + PNG 数据。"""
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)

    entries = bytearray()
    blob = bytearray()
    for size, png in frames:
        # 宽高字段为单字节，256 用 0 表示
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blob)
        )
        blob += png

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + bytes(blob))


def main() -> int:
    # 需要字体来绘制字母，因此使用真实平台而非 offscreen（后者不加载系统字体）
    app = QGuiApplication(sys.argv)
    assert app is not None

    frames = [(size, to_png(render(size))) for size in SIZES]
    write_ico(ICO_PATH, frames)

    # 同时导出一张 256px PNG，便于在文档中引用
    png_path = ASSETS / "quill.png"
    render(256).save(str(png_path))

    print(f"已生成 {ICO_PATH}")
    print(f"  尺寸：{', '.join(str(s) for s in SIZES)}")
    print(f"  体积：{ICO_PATH.stat().st_size / 1024:.1f} KB")
    print(f"已生成 {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""窗口背景图：加载、等比裁剪缩放，以及可选的模糊。

## 三条设计约定

**一、失败必须安静。** 背景图是装饰。路径失效、文件损坏、格式不支持，都只
表现成「没有背景」——绝不抛异常、绝不拦住窗口启动。用户把图片挪个位置，不该
让程序打不开；软件打不开，他也就没有界面可以把设置改回来了。（同一考虑见
`app/config.py` 的容错原则：装饰性字段不值得拿可用性去换。）

**二、模糊只算一次。** 拖动窗口边角会不停换尺寸，若每帧都重跑一遍卷积，拖动
会明显发卡。所以模糊在固定的**工作尺寸**上做一次并缓存，之后各尺寸只做缩放。

**三、缩放以「覆盖」为准**（cover），不是「装下」（contain）。contain 在长宽比
不匹配时会留白边，看起来像图片没加载完；cover 至少保证整幅铺满。

**四、模糊只依赖 QtGui，不碰 QtWidgets。** 常见的那套配方是
`QGraphicsScene` + `QGraphicsBlurEffect`，但 `QGraphicsScene` 属于 QtWidgets，
而本模块只需要一个 `QGuiApplication` 就能干活（自检就是这种环境）。少了
`QApplication` 时构造它会**直接段错误**——不是异常，是进程当场消失，连一行
错误都打不出来。实测踩过。下面用「降采样再上采样」的近似：一次缩放对
≈ 一次盒式模糊，两轮叠加已足够接近高斯，而且全程走 Qt 的 C++ 实现，
比在 Python 里逐像素卷积快几个数量级。附带好处是它不会取样到画布之外，
所以没有「四条边向内扩散出一圈暗边」的问题（那套配方必须靠外扩画布再裁回来
才能绕开）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap

# 模糊的工作尺寸。模糊在它上面做一次，之后按窗口尺寸缩放——所以这个值决定了
# 模糊「看起来有多糊」，与窗口大小无关（同一张图在大小窗口里观感一致）。
WORK_SIZE = QSize(1600, 1000)

# 源图长边上限。再大的图先缩进来：一张 8000px 的图既占内存，也会让唯一那次
# 卷积变成秒级操作，而它在屏幕上不可能比窗口更清楚。
MAX_SOURCE_EDGE = 8000

# ---------------------------------------------------------------------------
# 图案太碎就自动模糊
#
# 「加模糊蒙版」是用户开关，但关掉之后照片的细节会直接压在正文下面：面板再
# 不透明，透上来的那一层也是花纹。实测一张打印稿截图做背景，关掉模糊后在 88%
# 的面板上仍能读出原文的笔画，正文就被搅成一团。
#
# 判据取三个数，都在 64×64 灰度缩略图上算（一次几毫秒，只在加载时跑）：
#   - 相邻像素平均差：花纹有多碎。一张渐变壁纸在 2 上下，照片在 20 以上。
#   - 亮度标准差：整体明暗有多散。
#   - 平均亮度：只作诊断，**不进判定**。模糊改不了平均亮度——一张纯白图糊完
#     还是白的，拿它去治「图太亮」只是白费一次卷积。亮度那一半由面板保底负责
#     （见 `theme._VEIL_FLOOR_*`）。
# 判据要能在**原图**上算，不能算模糊之后的结果——模糊是它要决定的事。
# ---------------------------------------------------------------------------
STATS_SIZE = QSize(64, 64)
BUSY_EDGE = 12.0
BUSY_STD = 34.0


def texture_stats(src: QPixmap) -> tuple[float, float, float]:
    """返回 `(平均亮度, 亮度标准差, 相邻像素平均差)`，取值都在 0–255。

    源图为空时返回 `(0, 0, 0)`。三个数都在灰度缩略图上算，不看原分辨率。
    """
    if src.isNull():
        return (0.0, 0.0, 0.0)
    small = scale_cover(src, STATS_SIZE)
    if small.isNull():
        return (0.0, 0.0, 0.0)
    image = small.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    width, height, stride = image.width(), image.height(), image.bytesPerLine()
    # 走 `constBits` 而不是逐点 `pixelColor`：后者每个像素一次 C++ 边界调用，
    # 4096 个点要几十毫秒。这里一次性拿走连续内存，pad 字节用 stride 跳掉。
    data = bytes(image.constBits())
    values = [
        value
        for y in range(height)
        for value in data[y * stride : y * stride + width]
    ]
    count = len(values) or 1
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    edge_total = 0.0
    edge_count = 0
    for y in range(height):
        row = data[y * stride : y * stride + width]
        for x in range(width - 1):
            edge_total += abs(row[x] - row[x + 1])
            edge_count += 1
    return (mean, variance**0.5, edge_total / (edge_count or 1))


def needs_auto_blur(src: QPixmap) -> bool:
    """图案太碎时返回 `True`。

    这一条**只往上加不往下减**：用户勾了模糊，结果就是模糊；用户没勾，这里
    仍然可能替他勾上。反过来（替他关掉）没有理由——模糊从不会让字更难读。

    只看结构，不看明暗，理由见上面的注释：模糊能抹平花纹，抹不平亮度。纯色图
    （无论黑白）在这里是 `False`，因为把一张纯色图糊一遍，屏幕上不会有任何变化。
    """
    _mean, spread, edge = texture_stats(src)
    return edge > BUSY_EDGE or spread > BUSY_STD


def scale_cover(src: QPixmap, size: QSize) -> QPixmap:
    """等比放大到**恰好覆盖** `size`，多出来的部分从中心对称裁掉。

    返回的位图尺寸严格等于 `size`（调用方可以直接 `drawPixmap(0, 0, ...)`）。
    源图为空或目标尺寸非法时返回空 `QPixmap`——调用方据此判断「这次没得画」。
    """
    if src.isNull() or size.width() <= 0 or size.height() <= 0:
        return QPixmap()
    sw, sh = src.width(), src.height()
    # max 而不是 min：取较大的那个比例才能保证两个方向都不小于目标。
    # 用 min 就成了 contain，短边方向会留白。
    ratio = max(size.width() / sw, size.height() / sh)
    # round 之后仍可能比目标小 1 像素（浮点取整），兜一下，否则下面的 copy
    # 会越界，Qt 会静默返回一张比请求更小的图——尺寸断言才报得出来。
    tw = max(size.width(), round(sw * ratio))
    th = max(size.height(), round(sh * ratio))
    scaled = src.scaled(
        tw, th, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    return scaled.copy(
        max(0, (scaled.width() - size.width()) // 2),
        max(0, (scaled.height() - size.height()) // 2),
        size.width(),
        size.height(),
    )


def blur_factors(radius: int) -> tuple[int, ...]:
    """把「模糊半径」翻译成两轮的降采样倍数。

    一轮「缩到 1/f 再放回原尺寸」把每个像素摊成约 f 像素宽的一段过渡，
    两轮叠加的观感约等于半径 f 的高斯。取 f = radius / 2、两轮相同，
    合起来的过渡宽度就接近调用方说的那个半径。

    下限卡在 2：倍数取 1 等于什么都没做，而 `radius <= 0` 已经在上面被挡掉了。
    """
    factor = max(2, int(radius) // 2)
    return (factor, factor)


def apply_blur(src: QPixmap, radius: int) -> QPixmap:
    """近似高斯模糊，返回与输入**同尺寸**的位图。

    不用 `QGraphicsBlurEffect`——它属于 QtWidgets，而本模块只该依赖 QtGui：
    在只有 `QGuiApplication` 的环境里构造 `QGraphicsScene` 会段错误（见模块
    docstring 第四条）。降采样/上采样走的是 Qt 自己的缩放实现，不取样画布之外，
    因此没有边缘暗圈。
    """
    if src.isNull() or radius <= 0:
        return src

    image = src.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width, height = image.width(), image.height()
    for factor in blur_factors(radius):
        small_w = max(1, width // factor)
        small_h = max(1, height // factor)
        small = image.scaled(
            small_w,
            small_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        image = small.scaled(
            width,
            height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return QPixmap.fromImage(image)


def blur_radius_for(size: QSize) -> int:
    """按图片尺寸推模糊半径。

    写死一个像素值会随窗口大小变味：同一个半径在 4K 图上几乎看不见，在缩略图
    上又把画面全糊掉。按高度的固定比例来，观感才与尺寸无关。
    """
    return max(8, min(48, round(size.height() / 70)))


class BackgroundRenderer:
    """背景图的加载与出图。**所有失败都只表现为「没有背景」。**

    缓存分两层：
    - `_blurred`：模糊后的工作尺寸图（同一张图只算一次）
    - `_cache`：上一次按窗口尺寸缩放出来的结果（拖动时同尺寸会反复重绘）
    """

    def __init__(self) -> None:
        self._path = ""
        self._source: QPixmap | None = None
        self._blurred: QPixmap | None = None
        self._cache_key: tuple[int, int, bool] | None = None
        self._cache: QPixmap | None = None
        self._auto_blur = False

    @property
    def source_path(self) -> str:
        return self._path

    @property
    def has_image(self) -> bool:
        return self._source is not None

    @property
    def auto_blur(self) -> bool:
        """当前这张图是不是因为「太碎」被自动加了模糊。

        `pixmap_for(..., blur=False)` 仍可能输出模糊图——调用方要把这件事告诉
        用户，否则开关明明关着、画面却在糊，看起来像开关坏了。
        """
        return self._auto_blur

    def set_source(self, path: object) -> bool:
        """设置背景图。返回是否真的拿到了一张可用的图。

        这里把「文件不存在」「不是图片」「读不出来」都归成 `False` 而不是异常，
        调用方只需要照着 `False` 走「没有背景」这条路。
        """
        text = str(path or "").strip()
        self._path = text
        self._source = None
        self._blurred = None
        self._cache_key = None
        self._cache = None
        self._auto_blur = False
        if not text:
            return False
        try:
            file = Path(text)
            if not file.is_file():
                return False
            pixmap = QPixmap(str(file))
        except (OSError, ValueError):
            return False
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return False
        if max(pixmap.width(), pixmap.height()) > MAX_SOURCE_EDGE:
            pixmap = pixmap.scaled(
                MAX_SOURCE_EDGE,
                MAX_SOURCE_EDGE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._source = pixmap
        self._auto_blur = needs_auto_blur(pixmap)
        return True

    def clear(self) -> None:
        self.set_source("")

    def pixmap_for(self, size: QSize, blur: bool) -> QPixmap | None:
        """取出恰好 `size` 大小的背景图；没有图或尺寸非法时返回 `None`。

        `blur` 传的是**用户开关**的意愿，不是最终结论：图案太碎时这里会自己
        补上模糊（见 `needs_auto_blur`），`auto_blur` 属性说明有没有补过。
        """
        if self._source is None or size.width() <= 0 or size.height() <= 0:
            return None
        blur = bool(blur) or self._auto_blur
        key = (size.width(), size.height(), bool(blur))
        if key == self._cache_key and self._cache is not None:
            return self._cache
        if blur and self._blurred is None:
            working = scale_cover(self._source, WORK_SIZE)
            self._blurred = apply_blur(working, blur_radius_for(working.size()))
        base = self._blurred if blur else self._source
        if base is None or base.isNull():
            return None
        out = scale_cover(base, size)
        if out.isNull():
            return None
        self._cache_key = key
        self._cache = out
        return out

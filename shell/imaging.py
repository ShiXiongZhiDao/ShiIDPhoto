"""输出的编码与体积控制。

为什么不直接调上游的 `save_image`：它写死了 `pil_image.save(filename, dpi=...)`，
没有 quality 参数。JPEG 的默认 quality 是 75，用户既不能提高也不能按体积压。
这个模块接管"把数组变成文件"这一步，只做两件事——按质量编码、按目标体积找质量。

放在 shell 而不是改上游：上游是外部仓库，改它就要在构建时打补丁，
下次同步上游又会漂移。保存是壳层自己的职责。

依赖 PIL，不依赖 cv2：调用方已经转成 RGB。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

LOSSLESS_FORMATS = ("png",)


@dataclass(frozen=True)
class Encoded:
    """一次编码的结果。"""

    data: bytes
    quality: int | None  # PNG 无质量概念，为 None
    hit_target: bool  # 有没有达到体积上限；未设上限时恒为 True

    @property
    def size(self) -> int:
        return len(self.data)


def _is_lossy(fmt: str) -> bool:
    return fmt not in LOSSLESS_FORMATS


def encode(image: Image.Image, fmt: str, dpi: int, quality: int | None = None) -> bytes:
    """把 RGB 图编码成字节。

    JPEG 用 optimize=True：只重排 Huffman 表，不损失画质，体积通常小 3–8%。
    PNG 用 compress_level=9：同样无损，只是压得更久。
    dpi 必须显式写入——证件照的 300dpi 是输出契约，丢了打印尺寸就错。
    """
    buffer = io.BytesIO()
    if _is_lossy(fmt):
        if quality is None:
            raise ValueError("JPEG 编码必须指定质量")
        image.save(buffer, "JPEG", quality=int(quality), dpi=(dpi, dpi), optimize=True)
    else:
        image.save(buffer, "PNG", dpi=(dpi, dpi), optimize=True, compress_level=9)
    return buffer.getvalue()


def fit_to_size(
    image: Image.Image,
    fmt: str,
    dpi: int,
    quality: int,
    max_kb: int | None,
) -> Encoded:
    """按体积上限找可用的最高质量。

    策略：先用用户给的质量试一次，达标就直接返回——不做无谓的降质。
    不达标才在 1..quality-1 里二分，取"体积不超标"的最大质量。

    达不到时必须如实返回 hit_target=False，由调用方告诉用户实际最小体积。
    悄悄存一个超标的文件，用户上传时才发现，这才是最坏的结果。
    """
    if not _is_lossy(fmt):
        # PNG 是无损的：质量参数对它无效，能调的只有压缩级别，而级别已在
        # encode 里固定为 9。指望它把几 MB 压到 100 KB 是不现实的，
        # 与其给一个装作在努力的开关，不如直接说清。
        if max_kb is not None:
            raise ValueError("PNG 是无损格式，无法按体积压缩；需要限制大小时请改用 JPG")
        return Encoded(encode(image, fmt, dpi), None, True)

    if max_kb is None:
        return Encoded(encode(image, fmt, dpi, quality), quality, True)

    limit = max_kb * 1024
    cache: dict[int, bytes] = {}

    def attempt(q: int) -> bytes:
        if q not in cache:
            cache[q] = encode(image, fmt, dpi, q)
        return cache[q]

    first = attempt(quality)
    if len(first) <= limit:
        return Encoded(first, quality, True)

    # 先确认下限能不能达标，避免"二分到底还是超标"时白跑一遍
    floor_bytes = attempt(1)
    if len(floor_bytes) > limit:
        return Encoded(floor_bytes, 1, False)

    lo, hi = 1, quality - 1
    best = Encoded(floor_bytes, 1, True)
    while lo <= hi:
        mid = (lo + hi) // 2
        data = attempt(mid)
        if len(data) <= limit:
            best = Encoded(data, mid, True)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def save(encoded: Encoded, path: Path) -> None:
    """原子落盘：先写 .tmp 再替换。

    处理失败时不能留下半个文件——用户看到目录里有图，打开却是坏的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(encoded.data)
    tmp.replace(path)

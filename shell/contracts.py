"""预设与用户提示的契约层（纯标准库，不碰图像）。

为什么单独拆出来：`shell/server.py` 顶部要 import cv2，而开发机的系统 Python
通常没装它。把「配置怎么解释」「警告怎么翻译」这类纯逻辑放在这里，测试就能直接
覆盖，不用先把整个服务跑起来。

本模块只接收普通 dict / list，不读文件、不联网、不 import 第三方包。
"""
from __future__ import annotations

from typing import Iterable

#: `presets.json` 里 defaults 缺省时使用的值，与既有约定定下的界面行为一致。
DEFAULT_OUTPUT = {
    "layout_position": 4,
    "photos_spacing": 0,
    "add_crop_lines": True,
    "compress": False,
}

# 上游在没有检测到人时所抛的 warning 原文（ImageProcessor.crop_and_correct_image）。
_FACE_MISSING = "No human face detected"
_OBJECT_FOUND = "Object detected"
_OBJECT_MISSING = "No object detected"


def _as_int(value, default: int, low: int, high: int) -> tuple[int, bool]:
    if isinstance(value, bool):
        return default, False
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, False
    if number < low or number > high:
        return default, False
    return number, True


def _as_bool(value, default: bool) -> tuple[bool, bool]:
    if isinstance(value, bool):
        return value, True
    if isinstance(value, (int, float)):
        return bool(value), True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True, True
        if text in ("0", "false", "no", "off"):
            return False, True
    return default, False


def find_preset(presets: dict, preset_id: str | None) -> dict | None:
    """按 id 找预设；找不到返回 None（不抛异常，调用方负责回退）。"""
    if not preset_id:
        return None
    for item in presets.get("presets") or []:
        if isinstance(item, dict) and item.get("id") == preset_id:
            return item
    return None


def resolve_preset(presets: dict, requested: str | None) -> tuple[dict | None, str | None]:
    """解析「打开页面时停在哪套预设」。

    优先级：启动参数/环境变量 > `defaults.id` > 列表第一条。
    请求的 id 不存在时**不静默吞掉**——返回一句能贴进日志的说明再回退，
    否则用户只会看到"设了没反应"。
    """
    fallback = find_preset(presets, (presets.get("defaults") or {}).get("id"))
    if fallback is None:
        items = [item for item in (presets.get("presets") or []) if isinstance(item, dict)]
        fallback = items[0] if items else None

    if not requested:
        return fallback, None

    chosen = find_preset(presets, requested)
    if chosen is not None:
        return chosen, None

    name = fallback.get("id") if fallback else "无"
    return fallback, f"预设「{requested}」不存在，回退到「{name}」"


def output_defaults(presets: dict) -> tuple[dict, list[str]]:
    """把 `defaults` 里与出图相关的四个字段解释成 process_image 的参数。

    这四项以前在 server.py 里是硬编码的：改 JSON 不生效，数据契约等于半死。
    现在照配置走；类型或范围不对就回退硬编码值，并把原因交给调用方记日志——
    配置写错不该让用户拿到一张布局诡异的相纸，也不该阻止服务启动。
    """
    raw = presets.get("defaults") or {}
    notes: list[str] = []

    layout_position, ok = _as_int(
        raw.get("layout_position", DEFAULT_OUTPUT["layout_position"]),
        DEFAULT_OUTPUT["layout_position"],
        0,
        8,
    )
    if not ok:
        notes.append("defaults.layout_position 不是 0–8 的整数，已回退 4（居中）")

    photos_spacing, ok = _as_int(
        raw.get("photos_spacing", DEFAULT_OUTPUT["photos_spacing"]),
        DEFAULT_OUTPUT["photos_spacing"],
        0,
        200,
    )
    if not ok:
        notes.append("defaults.photos_spacing 不是 0–200 的整数，已回退 0")

    add_crop_lines, ok = _as_bool(
        raw.get("add_crop_lines", DEFAULT_OUTPUT["add_crop_lines"]),
        DEFAULT_OUTPUT["add_crop_lines"],
    )
    if not ok:
        notes.append("defaults.add_crop_lines 不是布尔值，已回退 True")

    compress, ok = _as_bool(raw.get("compress", DEFAULT_OUTPUT["compress"]), DEFAULT_OUTPUT["compress"])
    if not ok:
        notes.append("defaults.compress 不是布尔值，已回退 False")

    return {
        "layout_position": layout_position,
        "photos_spacing": photos_spacing,
        "add_crop_lines": add_crop_lines,
        "compress": compress,
    }, notes


# 上游抛的是英文异常，直接回显给用户等于没解释。
# 保留原文附在后面：出问题时还能拿去搜，也能让我在日志里对上。
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "exceeds the size of the photo sheet",
        "这张相纸放不下这么多张：把行列数调小，或改用更大的相纸；也可以先切成单张。",
    ),
    (
        "does not exist in size configurations",
        "规格没有找到，可能是自定义尺寸没有正确传入。",
    ),
)


def translate_error(message: object) -> str:
    """把上游异常翻成人话，原文附后。"""
    text = str(message or "").strip()
    lowered = text.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in lowered:
            return f"{hint}（原始信息：{text}）"
    return text


def translate_warnings(messages: Iterable[object]) -> list[str]:
    """把上游的 warnings 翻成用户能懂、且能据此行动的一句话。

    上游在"没检测到人"时只用 `warnings.warn` 说一声，然后悄无声息地退化成
    YOLO 物体检测 / 整图裁切，接口照样返回成功。不接住它，用户看到的就是
    "处理完成"配一张不可用的图——最坏的一类失败。

    没检测到人时会连着抛几条（无脸 → 有物体 → 无物体），所以按最终走到的
    分支合成一条，而不是原样透传三行英文。
    """
    text = " ".join(str(item) for item in messages)
    if _FACE_MISSING not in text:
        return []
    if _OBJECT_MISSING in text:
        return ["没有检测到人脸或主体，已按整张原图构图，结果很可能不合格。请换一张单人正面照。"]
    if _OBJECT_FOUND in text:
        return ["没有检测到人脸，已按画面中的物体构图，结果很可能不合格。请换一张单人正面照。"]
    return ["没有检测到人脸，构图可能不准确，结果很可能不合格。请换一张单人正面照。"]


# 不提供 webp：实测 PIL 存 webp 不保留 DPI（读回来 info["dpi"] 为空），
# 而证件照的 300dpi 是输出契约的一部分。宁可不支持，也不给一个元数据残缺的格式。
OUTPUT_FORMATS = ("jpg", "jpeg", "png")


def normalize_format(value: object) -> str:
    """只接受声明过的输出格式，默认 jpg。

    不做"猜扩展名"：`fmt=webp` 这类请求必须显式失败，而不是悄悄存成 jpg。
    jpg 与 jpeg 是同一个编码器，区别只在扩展名——有些报名系统只认 .jpeg。
    """
    fmt = str(value or "jpg").strip().lower()
    if fmt not in OUTPUT_FORMATS:
        raise ValueError(f"不支持的输出格式：{fmt}（可选：{', '.join(OUTPUT_FORMATS)}）")
    return fmt


# 质量档位。数值直接是 JPEG quality；档位名给人看，数字给编码器。
#
# 默认 high(92)：证件照是要交出去的，画质优先。报名系统卡体积的场合，
# 用「体积上限」去压——二分从 92 往下找，既能装进限制，又保留能保留的最高质量。
# 反过来（默认 75、想清晰自己往上调）等于让图省事的人拿到更差的照片。
#
# standard(75) 曾是早期默认，因为那是 Pillow 的默认值、与旧输出一致。改掉它
# 是一次有意的取舍：一致性的价值，低于"默认给最好的结果"。见既有约定。
QUALITY_PRESETS: dict[str, int] = {
    "high": 92,
    "standard": 75,
    "small": 60,
    "tiny": 45,
}
DEFAULT_QUALITY = QUALITY_PRESETS["high"]

# 低于这个质量，证件照会出现肉眼可见的块状伪影。为达到体积上限而被压到这里，
# 必须提示用户——"达标"不等于"能用"。
LOW_QUALITY_WARN = 40
QUALITY_MIN, QUALITY_MAX = 1, 100

# 目标体积的上限：20 KB 是常见报名系统的最低门槛，8 MB 超过任何报名系统的上限，
# 再大就该用原图而不是这里压。
MAX_KB_MIN, MAX_KB_MAX = 20, 8192


def normalize_quality(value: object) -> int:
    """把档位名或数字解释成 1–100 的 JPEG 质量。

    不静默纠正：给 0 或 200 直接报错，而不是夹到 1 或 100——用户以为设成了
    "极限压缩"，实际是别的东西，这种错最难查。
    """
    if value is None or str(value).strip() == "":
        return DEFAULT_QUALITY

    text = str(value).strip().lower()
    if text in QUALITY_PRESETS:
        return QUALITY_PRESETS[text]

    try:
        quality = int(text)
    except ValueError as exc:
        names = "/".join(QUALITY_PRESETS)
        raise ValueError(f"未知的质量档位：{text}（可选：{names} 或 {QUALITY_MIN}–{QUALITY_MAX}）") from exc

    if not QUALITY_MIN <= quality <= QUALITY_MAX:
        raise ValueError(f"质量超出范围（{QUALITY_MIN}–{QUALITY_MAX}）：{quality}")
    return quality


def normalize_max_kb(value: object) -> int | None:
    """目标体积上限（KB）。空值表示不限制。"""
    if value is None or str(value).strip() == "":
        return None

    text = str(value).strip().lower().replace("kb", "").strip()
    try:
        # 允许 99.5 这种带小数一点的输入，四舍五入到整 KB
        kb = int(round(float(text)))
    except ValueError as exc:
        raise ValueError(f"体积上限必须是数字（KB）：{value}") from exc

    if not MAX_KB_MIN <= kb <= MAX_KB_MAX:
        raise ValueError(f"体积上限超出范围（{MAX_KB_MIN}–{MAX_KB_MAX} KB）：{kb}")
    return kb

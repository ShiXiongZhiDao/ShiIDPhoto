"""自定义规格：存储、校验、换算，以及向上游注入临时规格。

为什么不改 `data/size_zh.csv`：
1. 那是上游仓库的文件，改它就要在构建期打补丁，下次同步上游必然漂移；
2. CSV 是发行包内容，用户的自定义规格属于用户数据，两者混在一起，
   覆盖安装时会互相踩。

为什么换算用四舍五入而不是 Python 的 `int()`：
上游 `get_resize_image_list` 里是 `int(print_inch * resolution)`（截断），
但 `data/size_zh.csv` 里 35 行有 34 行的像素值是四舍五入算的。两者本来会打架，
只是被同一个函数里的 `max(打印换算, 电子像素)` 兜住了。
自定义规格既然是我们自己换算，就跟 CSV 的做法对齐——那才是人手工核对出来的数字。

依赖标准库。上游对象只在这里被"注入/恢复"，不做任何算法改动。
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
MAX_SIZES = 20

# 100 像素以下是缩略图，2000 像素以上已经超过任何证件照的电子规格。
# 上限不是技术限制，是"再大就不该叫证件照了"。
PX_MIN, PX_MAX = 100, 2000

# 宽高比（高/宽）。低于 0.5 是横条，高于 1.5 是竖条，两种都会让裁剪框退化：
# 人脸框按比例裁完只剩一条，出来是废图。挡在这里比让用户拿到废图强。
RATIO_MIN, RATIO_MAX = 0.5, 1.5

DPI_MIN, DPI_MAX = 72, 1200
NAME_MAX = 24

# 注入到上游 ConfigManager 里用的键。以双下划线开头，不可能与 CSV 的 Name 冲突
# （CSV 里全是中文名）。固定一个键而不是每次生成唯一名，是为了不在单例 dict 里
# 留下无数条临时数据——处理有全局锁，同一时刻只有一个规格在用。
CUSTOM_KEY = "__custom__"


def mm_to_px(mm: float, dpi: int) -> int:
    """毫米转像素。四舍五入，与 size_zh.csv 的既有算法一致。"""
    return int(round(float(mm) / 25.4 * int(dpi)))


def px_to_mm(px: int, dpi: int) -> float:
    return round(float(px) / int(dpi) * 25.4, 1)


def _clean_name(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:NAME_MAX]


def _as_optional_float(value: object, label: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label}必须是数字") from exc


def auto_name(width: int, height: int, dpi: int, mm_w: float | None, mm_h: float | None) -> str:
    if mm_w and mm_h:
        return f"{mm_w:g}×{mm_h:g}mm"
    return f"{width}×{height}px @{dpi}"


def normalize(
    *,
    width: object,
    height: object,
    dpi: object = 300,
    mm_w: object = None,
    mm_h: object = None,
    name: object = "",
    size_id: str | None = None,
) -> dict:
    """校验并规范成一条可存储的规格。

    不静默夹取：越界就报错，并说清楚边界。把 3000 夹成 2000 再告诉用户"成功"，
    等于让他以为自己在用 3000 的规格。
    """
    try:
        w = int(str(width).strip())
        h = int(str(height).strip())
        d = int(str(dpi).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("宽、高、DPI 必须是整数") from exc

    if not PX_MIN <= w <= PX_MAX or not PX_MIN <= h <= PX_MAX:
        raise ValueError(f"宽高需在 {PX_MIN}–{PX_MAX} 像素之间（当前 {w}×{h}）")
    if not DPI_MIN <= d <= DPI_MAX:
        raise ValueError(f"DPI 需在 {DPI_MIN}–{DPI_MAX} 之间（当前 {d}）")

    ratio = h / w
    if not RATIO_MIN <= ratio <= RATIO_MAX:
        raise ValueError(
            f"宽高比超出范围：高/宽 = {ratio:.2f}，需在 {RATIO_MIN}–{RATIO_MAX} 之间"
        )

    mm_w_value = _as_optional_float(mm_w, "毫米宽度")
    mm_h_value = _as_optional_float(mm_h, "毫米高度")
    if (mm_w_value is not None and mm_w_value <= 0) or (mm_h_value is not None and mm_h_value <= 0):
        raise ValueError("毫米尺寸必须为正数")

    return {
        "id": size_id or uuid.uuid4().hex[:12],
        "name": _clean_name(name) or auto_name(w, h, d, mm_w_value, mm_h_value),
        "width": w,
        "height": h,
        "dpi": d,
        "mm_w": mm_w_value,
        "mm_h": mm_h_value,
    }


def load(path: Path) -> tuple[list[dict], str | None]:
    """读取已保存的规格。

    返回 (规格列表, 需要告诉用户的问题)。文件坏了不崩、不清空——
    用户手改坏了一个括号，不该导致他之前存的 10 条全没了。
    """
    if not path.is_file():
        return [], None

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"自定义规格文件无法读取（{exc}），本次按空列表处理，原文件未改动"

    raw = doc.get("sizes") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return [], "自定义规格文件结构异常（缺少 sizes 数组），本次按空列表处理"

    items: list[dict] = []
    skipped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        try:
            items.append(
                normalize(
                    width=entry.get("width"),
                    height=entry.get("height"),
                    dpi=entry.get("dpi", 300),
                    mm_w=entry.get("mm_w"),
                    mm_h=entry.get("mm_h"),
                    name=entry.get("name", ""),
                    size_id=str(entry.get("id") or "") or None,
                )
            )
        except ValueError:
            skipped += 1

    note = f"自定义规格里有 {skipped} 条不合法，已跳过" if skipped else None
    return items[:MAX_SIZES], note


def save(path: Path, items: list[dict]) -> None:
    """原子写入。写一半断电不能留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": SCHEMA_VERSION, "sizes": items[:MAX_SIZES]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def for_upstream(width: int, height: int, dpi: int, mm_w: float | None, mm_h: float | None) -> dict:
    """构造一条和 CSV 行同构的 dict，供上游按名字查询。

    `PrintWidth/PrintHeight` 的单位是**厘米**（CSV 用 cm，不是 mm），
    所以毫米要除以 10。只给像素时不填这两项，上游会直接用电子尺寸。
    """
    return {
        "PrintWidth": (mm_w / 10) if mm_w else None,
        "PrintHeight": (mm_h / 10) if mm_h else None,
        "ElectronicWidth": int(width),
        "ElectronicHeight": int(height),
        "Resolution": int(dpi),
        "FileFormat": "jpg",
        "FileSizeMin": None,
        "FileSizeMax": None,
        "Type": "photo",
        "Notes": "custom size",
    }


@contextmanager
def injected(config_manager, entry: dict) -> Iterator[str]:
    """往上游的规格表里临时塞一条，退出时恢复原样。

    上游的 `ConfigManager` 是单例，`size_config` 是普通 dict，`get_size_config`
    直接查它——所以注入一条就能被 `process_image` 认到，不用碰 CSV 文件。

    必须 try/finally：处理过程中抛异常（缺文件、模型报错）时，若不清掉，
    这条临时规格会留在单例里，下一次查询可能拿到上一次的尺寸。
    调用方需保证同一时刻只有一个注入在用（server 有全局处理锁）。
    """
    previous = config_manager.size_config.get(CUSTOM_KEY)
    config_manager.size_config[CUSTOM_KEY] = entry
    try:
        yield CUSTOM_KEY
    finally:
        if previous is None:
            config_manager.size_config.pop(CUSTOM_KEY, None)
        else:
            config_manager.size_config[CUSTOM_KEY] = previous

"""用户偏好（规格 / 底色 / 输出模式 / 相纸 / 自定义尺寸 / 输出参数 / 界面语言 / 界面外观）的读写与校验。

为什么存文件而不是浏览器：`localStorage` 的 origin **含端口**，而启动器在端口被占用时
会自动往后找（7860 → 7861/7862，见 `launcher.py::choose_port`），端口一换那份记忆
就等于没有，而且界面不会告诉你。`user-data/prefs.json` 跟包走，
覆盖安装也不动它——和 `custom-sizes.json`、模型同一个待遇。

校验只有这一处（既有约定 A）：前端不自己判第二遍，否则两边的规则一定会漂移，
失效原因会被说成两句不同的话。现成的反面教材是旧的 localStorage 路径：
存进 `fmt=webp` 时下拉变成空选中、底栏 chip 空白、请求带着 `fmt=''` 发出去，
`contracts.normalize_format` 把它悄悄当 jpg——没人报错，但界面说的和做的不是一件事。
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1

# 白名单：不在这里的东西不入库，也不报错（既有约定：字段不认识就忽略，不猜）
FIELDS = (
    "size",
    "background",
    "mode",
    "sheet",
    "custom_size_id",
    "format",
    "quality",
    "max_kb",
    "max_kb_on",
    "language",
    "theme",
)

MODES = ("single", "sheet")
LANGUAGES = ("auto", "zh", "en")
# 界面外观只有深浅两档。这三个名字是三处同源的契约：
# prefs.THEMES ↔ style.css 的 html[data-theme="…"] 块 ↔ app.js 的 THEMES，
# 对不上不会报错，只会"点了开关屏幕闪一下又回来"。
THEMES = ("dark", "light")
THEME_DEFAULT = "dark"
# 与 shell/server.py 的 CUSTOM_SIZE_KEY 一致：前端用它表示"这次用自定义尺寸"
CUSTOM_SIZE_KEY = "__custom__"


def load(path: Path) -> tuple[dict, str | None]:
    """读出原始存值。文件坏了不崩、不删——回退到默认，但要告诉用户为什么。"""
    if not path.is_file():
        return {}, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"上次的设置文件读不出来（{exc.__class__.__name__}），这次从默认值开始，原文件未改动"
    if not isinstance(doc, dict):
        return {}, "上次的设置文件结构异常，这次从默认值开始，原文件未改动"
    if int(doc.get("schema_version") or 0) > SCHEMA_VERSION:
        return {}, f"设置文件是更新版本写的（schema {doc.get('schema_version')}），本次不猜它的含义"
    return {k: doc[k] for k in FIELDS if k in doc}, None


def save(path: Path, prefs: dict) -> None:
    """原子写入。写一半断电不能留下半个 JSON——调用方负责把 OSError 转成看得见的提示。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": SCHEMA_VERSION}
    doc.update({k: prefs[k] for k in FIELDS if k in prefs})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize(raw: dict, catalog: dict, custom_ids: set[str], defaults: dict) -> tuple[dict, list[str]]:
    """把任意来源的存值收敛成"这次真正生效的值"，并逐条给出回退原因。

    匹配一律**逐字**：规格与底色名要和上游 CSV 的 Name 完全一致，
    不做"差不多"的模糊匹配——CSV 里本来就存在同名不同尺寸（大一寸 / 小二寸都是 3.3×4.8）。
    """
    notes: list[str] = []

    size_names = {p["name"] for p in catalog.get("photos", [])}
    color_names = {c["name"] for c in catalog.get("colors", [])}
    sheet_names = {s["name"] for s in catalog.get("sheets", [])}

    size = _text(raw.get("size")) or defaults.get("size")
    background = _text(raw.get("background")) or defaults.get("background")
    mode = _text(raw.get("mode")) or defaults.get("mode")
    sheet = _text(raw.get("sheet")) or defaults.get("sheet")
    custom_id = _text(raw.get("custom_size_id"))

    if custom_id and custom_id not in custom_ids:
        notes.append("上次记住的自定义尺寸已经被删掉了")
        custom_id = ""

    if size == CUSTOM_SIZE_KEY:
        if not custom_id:
            notes.append("上次是自定义尺寸但没记下是哪一条，这次回到默认规格")
            size = defaults.get("size")
    elif size not in size_names:
        notes.append(f"上次选的规格「{size}」在这次的规格里找不到了，已回退")
        size = defaults.get("size")

    if background not in color_names:
        notes.append(f"上次选的底色「{background}」不在这次的底色里，已回退")
        background = defaults.get("background")

    if mode not in MODES:
        notes.append(f"上次记住的输出模式「{mode}」不认识，已回退")
        mode = defaults.get("mode")

    if sheet not in sheet_names:
        notes.append(f"上次选的相纸「{sheet}」不在这次的相纸里，已回退")
        sheet = defaults.get("sheet")

    # 输出三项沿用既有解释规则（contracts），不在这里重写一遍；
    # 规则会抛错，所以每一项都要单独接住并给出一条人话的回退原因。
    from contracts import QUALITY_PRESETS, normalize_format, normalize_max_kb, normalize_quality

    try:
        fmt = normalize_format(raw.get("format") or defaults.get("format"))
    except ValueError as exc:
        notes.append(f"上次记住的输出格式用不了（{exc}），已回退")
        fmt = normalize_format(defaults.get("format"))

    quality = _quality_name(raw.get("quality"), defaults.get("quality"), notes)
    try:
        max_kb = normalize_max_kb(raw.get("max_kb") if raw.get("max_kb") is not None
                                  else defaults.get("max_kb"))
    except ValueError as exc:
        notes.append(f"上次记住的体积上限用不了（{exc}），已回退")
        max_kb = normalize_max_kb(defaults.get("max_kb"))
    # 没有上限数值时，勾选状态不能单独活着——那会让界面显示一个不起作用的勾
    max_kb_on = bool(raw.get("max_kb_on")) and max_kb is not None

    # 界面语言：`auto` 是"跟随系统"这个真状态，不是"没存过"。
    # 存 auto 就永远按启动时的系统语言走，存 zh/en 才和用户的手选绑定。
    language = _text(raw.get("language")).lower() or defaults.get("language") or "auto"
    if language not in LANGUAGES:
        notes.append(f"上次记住的界面语言「{language}」不认识，已回到跟随系统")
        language = "auto"

    # 界面外观：只有深浅两档，没有"跟随系统"——那会把"我点了开关"这件事变成一次猜测。
    theme = _text(raw.get("theme")).lower() or defaults.get("theme") or THEME_DEFAULT
    if theme not in THEMES:
        notes.append(f"上次记住的界面外观「{theme}」不认识，已回到深色")
        theme = THEME_DEFAULT

    return (
        {
            "size": size,
            "background": background,
            "mode": mode,
            "sheet": sheet,
            "custom_size_id": custom_id,
            "format": fmt,
            "quality": quality,
            "max_kb": max_kb,
            "max_kb_on": bool(max_kb_on),
            "language": language,
            "theme": theme,
        },
        notes,
    )


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""

def _quality_name(value, default: str, notes: list[str]) -> str:
    """质量存的是界面那四个档位名。也接受数字（92），但要能唯一回到档位名。"""
    from contracts import QUALITY_PRESETS, normalize_quality

    if value is None or str(value).strip() == "":
        return default
    by_value = {num: name for name, num in QUALITY_PRESETS.items()}
    try:
        quality = normalize_quality(value)
    except ValueError as exc:
        notes.append(f"上次记住的质量档位用不了（{exc}），已回退")
        return default
    if quality not in by_value:
        notes.append(f"上次的质量 {quality} 不是这四档之一，已回退")
        return default
    return by_value[quality]


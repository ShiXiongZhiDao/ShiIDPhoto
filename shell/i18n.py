"""界面语言：词条查表、语言判定、上游目录的双语标签。

形状（既有约定的修订）：**中文原串本身就是键**。中文态完全不查表，
所以"中文侧逐字相等"是结构保证，不是要靠测试守着的约定；
代价由闸来付——源码里出现没有英文词条的新串，`--check` 就红（既有约定.2）。

三条不能踩的线：
1. **canonical 值不本地化**（决策 8）。进 `prefs.json`、进 `/api/process` 请求参数、
   进 `presets.json` 的字段的，永远是上游 CSV 的中文 `Name`。这里只产出**显示名**。
2. **不碰上游那个 `language=` 参数**（决策 8 的前提）：`src/tool/ConfigManager.py:19-24` 是单例、
   `:27-29` 被 `initialized` 罩住，第二次传 `language='en'` 会被静默吞掉。所以英文目录名
   由本模块直接读 `data/size_en.csv` / `color_en.csv`，与中文表逐行对齐。
3. **前端零依赖**。词条是一份静态 JSON，不引构建链、不装 i18n 库。
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

LANGS = ("auto", "zh", "en")

HERE = Path(__file__).resolve().parent          # <root>/shell
ROOT = HERE.parent
LEXICON_PATH = HERE / "static" / "i18n.json"
# 上游目录：包里在 <root>/data，开发树里在 <root>/build-assets/upstream/data
DATA_DIRS = (ROOT / "data", ROOT / "build-assets" / "upstream" / "data")

_lexicon: dict | None = None


def load_lexicon(path: Path | None = None) -> dict:
    """读词条表。读不到就退成"原样返回中文"，绝不因为语言层挂掉而让出片失败。"""
    global _lexicon
    if _lexicon is not None and path is None:
        return _lexicon
    target = path or LEXICON_PATH
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if path is not None:
        return doc
    _lexicon = doc
    return doc


def forget_lexicon() -> None:
    """给测试用：换一份词条表再读，不吃模块缓存。"""
    global _lexicon
    _lexicon = None


#: 当前生效语言。服务端与启动器都在**启动时定一次**，
#: 消息生成点只调 `msg()`，不把 lang 一路传下去——传参链一长就一定会有人漏传，
#: 漏了就是"按钮英文、弹窗中文"那种劈开态。
LANG = "zh"


def set_lang(lang: str) -> None:
    global LANG
    LANG = lang if lang in ("zh", "en") else "zh"


def msg(template: str, *args, lang: str | None = None) -> str:
    """按当前语言出这句话。`template` 必须就是源码里的那条中文原串（词条表的键）。"""
    effective = lang or LANG
    text = t(template, effective, *args)
    return localize_values(text) if effective == "en" else text


_PATTERNS: list[tuple[re.Pattern, str]] | None = None


def _templates() -> list[tuple[re.Pattern, str]]:
    """把词条表编译成"成品句 → 英文"的匹配表。

    为什么在出口翻译而不是在 110 个调用点包 `msg()`：后端消息有三条出口通道
    （`prefs_notes`、JSON 里的 `error`/`stage`、控制台 `log`），在出口一处翻译
    ①不用把 lang 一路传下去（传参链一长必漏，漏了就是劈开态），
    ②顺带覆盖 `f"设置存不下来（HTTP {code}）"` 这种拼接出来的整句，
    ③源码里的中文原串一个字没动，中文态逐字不变是结构性的。
    代价：匹配不上就原样返回中文——所以词条表缺条目时界面露中文，这正是要的失败形状。
    """
    global _PATTERNS
    if _PATTERNS is None:
        table = load_lexicon().get("en", {})
        pairs = []
        for zh, en in table.items():
            if "{x}" not in zh:
                continue
            parts = [re.escape(p) for p in zh.split("{x}")]
            body = parts[0] + "".join("(.*?)" + p for p in parts[1:])
            pairs.append((re.compile("(?s)" + body + "$"), en))
        # 占位符少的先试，同长度按字面更长者先，避免短模板把长句吞掉
        pairs.sort(key=lambda p: (p[0].pattern.count("(.*?)"), -len(p[0].pattern)))
        _PATTERNS = pairs
    return _PATTERNS


#: 插值进来的**值**也要跟着换显示名，否则英文句子里会夹着 `(一寸)` 这种中文。
#: canonical 值本身没变（存盘、请求参数还是中文），这里只动最终给用户看的那串字（决策 8 的边界）。
STAGE_TERMS = {"抠图": "cutout", "人脸": "face", "姿态": "pose"}
_VALUE_PAIRS: list[tuple[str, str]] | None = None


def _value_pairs() -> list[tuple[str, str]]:
    """(中文 canonical → 英文显示名)，按长度从大到小，避免 `一寸` 抢在 `大一寸` 前面替。"""
    global _VALUE_PAIRS
    if _VALUE_PAIRS is None:
        pairs: list[tuple[str, str]] = []
        for kind, names in (("photos", read_names("size_zh.csv")),
                            ("colors", read_names("color_zh.csv"))):
            for name in names:
                label = display_label(kind, name, "en")
                if label != name:
                    pairs.append((name, label))
        pairs.extend(STAGE_TERMS.items())
        pairs.sort(key=lambda p: -len(p[0]))
        _VALUE_PAIRS = pairs
    return _VALUE_PAIRS


def localize_values(text: str) -> str:
    out_text = text
    for zh, en in _value_pairs():
        if zh in out_text:
            out_text = out_text.replace(zh, en)
    return out_text


def out(text: str, lang: str | None = None, _depth: int = 0) -> str:
    """把一条**已经拼好**的中文消息换成英文；换不动就原样返回。

    每个捕获组也要再过一遍 `out()`：`提示：{x}` 里插进去的本身又是一条拼接出来的消息
    （`f"提示：{note}"`，note 自己来自 `f"可选模型「{id}」未安装…"`）。
    不递归的话就出现"前缀英文、正文中文"那种最难看的半截态。
    """
    effective = lang or LANG
    if effective != "en" or not text:
        return text
    table = load_lexicon().get("en", {})
    if text in table:
        return localize_values(table[text])
    for pattern, en in _templates():
        m = pattern.fullmatch(text)
        if m:
            groups = [g if _depth > 3 else out(g, effective, _depth + 1) for g in m.groups()]
            out_text = en
            for group in groups:
                out_text = out_text.replace("{x}", group, 1)
            return localize_values(out_text)
    return text


def forget_patterns() -> None:
    """给测试用：词条表换了就重建匹配表。"""
    global _PATTERNS
    _PATTERNS = None


def t(text: str, lang: str, *args, lexicon: dict | None = None) -> str:
    """按原串查英文词条，再按顺序填 `{x}`。中文态直接返回原串。

    缺词条时**返回中文原串**而不是崩或返回空——界面上露一个中文词是可读的故障，
    空白是不可读的故障。缺词条由 `scripts/i18n_extract.py --check` 在构建期拦。
    """
    if lang != "en":
        return text
    table = (lexicon if lexicon is not None else load_lexicon()).get("en", {})
    out = table.get(text, text)
    for value in args:
        out = out.replace("{x}", str(value), 1)
    return out


#: `LANG=C.UTF-8` / `LC_ALL=c` 是 POSIX 兜底值，不是语言，别拿它判（判出来会把中文系统当英文）。
NON_LOCALES = {"c", "posix", "default", "x-none", ""}
LOCALE_SENGLANGUAGE = 0x1001
LANG_CHINESE = 0x04  # PRIMARYLANGID(0x0804) == 0x04：简中繁中都算中文


def _win_ui_lang():
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetUserDefaultUILanguage.restype = ctypes.c_ulong
    langid = kernel32.GetUserDefaultUILanguage()
    primary = langid & 0x3FF
    buf = ctypes.create_unicode_buffer(85)
    kernel32.GetLocaleInfoW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_wchar_p,
                                        ctypes.c_int]
    n = kernel32.GetLocaleInfoW(langid, LOCALE_SENGLANGUAGE, buf, 85)
    if n and buf.value:
        # LOCALE_SENGLANGUAGE 恒返回英文名，不受界面语言影响
        value = buf.value.lower()
        return "zh" if value.startswith("chi") else value
    return "zh" if primary == LANG_CHINESE else None


def os_ui_lang() -> str:
    """操作系统的界面语言。取不到就回 zh——本仓的出厂默认是中文，宁可不切也别切错。

    Windows 上必须用 **UI language**（`GetUserDefaultUILanguage`）而不是 locale：
    中文系统把界面切成英文时 `locale.getdefaultlocale()` 还是回 `zh_CN`。
    `LocaleNameFromLCID` 不能用在打包版里：它是 Vista+ 的导出，实测这台机器的
    kernel32 句子上就取不到，判错语言比不判更糟。
    """
    import os as _os
    import sys as _sys

    if _sys.platform == "win32":
        try:
            found = _win_ui_lang()
            if found:
                return found
        except Exception:  # noqa: BLE001 - 判不出来就往下走环境变量
            pass
    for key in ("IDPHOTO_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (_os.environ.get(key) or "").split(".")[0].strip().lower()
        if value and value not in NON_LOCALES:
            return value
    return "zh"


def lang_from_os(code: str) -> str:
    """判定口径：以 zh 开头走中文，其余一律英文——本仓只有这两个语种。"""
    return "zh" if (code or "").lower().startswith("zh") else "en"


def resolve(pref_value: str | None, cfg_default: str | None = None,
            os_code: str | None = None) -> tuple[str, str]:
    """优先级链（既有约定的修订）：**prefs 显式值 > OS 判定**。

    `cfg_default`（`portable.json:webui.lang`，出厂写死 `"zh"`）**不参与 auto 判定**：
    它一参与，"跟随系统"就永远是中文，用户那句「默认随系统」根本落不了地——
    这条是我原先把链排错了。该字段只服务 `--classic` 的上游 Gradio 路径（`launcher.py:229`）。
    返回 `(intent, effective)`：intent 是存的 `auto|zh|en`，effective 是这次真正生效的语种。
    """
    intent = pref_value if pref_value in LANGS else "auto"
    if intent != "auto":
        return intent, intent
    return "auto", lang_from_os(os_code if os_code is not None else os_ui_lang())


def _zh_en_map(kind: str) -> dict[str, str]:
    """把上游中文目录 Name 映射到英文 Name。两份表逐行对齐，对不上就整体放弃（宁可显中文）。"""
    zh = read_names(f"{kind}_zh.csv")
    en = read_names(f"{kind}_en.csv")
    if not zh or len(zh) != len(en):
        return {}
    return {z: e for z, e in zip(zh, en) if z and e}


def read_names(filename: str) -> list[str]:
    for d in DATA_DIRS:
        path = d / filename
        if path.is_file():
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                return []
            header = [h.strip().lower() for h in rows[0]]
            try:
                col = header.index("name")
            except ValueError:
                col = 0
            return [r[col].strip() for r in rows[1:] if len(r) > col and r[col].strip()]
    return []


def catalogs_aligned() -> tuple[bool, str]:
    """两份 CSV 是不是同一批规格（既有约定的构建期闸）。"""
    for kind in ("size", "color"):
        zh, en = read_names(f"{kind}_zh.csv"), read_names(f"{kind}_en.csv")
        if not zh or not en:
            return False, f"{kind}: 找不到 CSV"
        if len(zh) != len(en):
            return False, f"{kind}: 行数不等 zh={len(zh)} en={len(en)}"
    return True, ""


_LABELS: dict[str, dict[str, str]] | None = None


def label_maps() -> dict[str, dict[str, str]]:
    global _LABELS
    if _LABELS is None:
        _LABELS = {"photos": _zh_en_map("size"), "colors": _zh_en_map("color"),
                   "sheets": _zh_en_map("size")}
    return _LABELS


def pretty_en(name: str) -> str:
    """上游英文名的可读化：`light_blue` → `Light blue`，剥掉括号尾巴。

    只做机械变换，不查表——规格名有 35 条，搬进词条表就多一处会和上游漂移的地方。
    """
    out = name.replace("_", " ").strip()
    out = re.sub(r"\s*\((?:ID Photo|Standard|Standard2寸|证件照)\)", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def display_label(kind: str, canonical: str, lang: str) -> str:
    """当前语言的显示名；canonical 值永不变（决策 8）。英文态缺映射就退回中文原名。"""
    if lang != "en":
        return canonical
    mapped = label_maps().get(kind, {}).get(canonical)
    return pretty_en(mapped) if mapped else canonical


def localize_catalog(catalog: dict, lang: str) -> dict:
    """给目录三项都补上 `label`。`name` 保持 canonical，请求与 prefs 继续用它。"""
    kinds = {"photos": "photos", "colors": "colors", "sheets": "sheets"}
    out: dict = {}
    for key, items in catalog.items():
        kind = kinds.get(key)
        if not kind or not isinstance(items, list):
            out[key] = items
            continue
        rows = []
        for item in items:
            row = dict(item)
            row["label"] = display_label(kind, item.get("name", ""), lang)
            rows.append(row)
        out[key] = rows
    return out

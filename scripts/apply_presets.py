"""构建期把「预设」注入上游 WebUI。

这是整个项目唯一修改上游源码的地方，因此约束很硬：

1. 每个锚点必须**恰好命中一次**，命中 0 次或多次都直接让构建失败，绝不做模糊替换；
2. 幂等：检测到标记就跳过，重复构建不会叠加补丁；
3. 注入逻辑自带兜底：配置文件坏了、规格名对不上，一律静默回退到上游默认值，
   不能让"预设"这种体验优化把程序拖到起不来。

用法（由 build_portable.py 调用）：
    python scripts/apply_presets.py --package dist/staging
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

MARKER = "# --- idphoto preset patch ---"

HELPER = '''# --- idphoto preset patch ---
# 由构建期注入，详见既有约定。
# 目的是让界面打开时就已经停在某个预设上，用户不需要自己去翻十几个控件。
def _idphoto_preset_defaults(language):
    fallback = {
        "size": None, "sheet": None, "color": None,
        "background_hex": "#FFFFFF", "rows": 3, "cols": 3, "layout": True,
    }
    try:
        import csv
        import json
        import os

        config_path = os.path.join(PROJECT_ROOT, "config", "presets.json")
        if not os.path.isfile(config_path):
            return fallback
        with open(config_path, "r", encoding="utf-8") as stream:
            data = json.load(stream)

        wanted = os.environ.get("IDPHOTO_PRESET") or data.get("defaults", {}).get("id")
        preset = None
        for item in data.get("presets", []):
            if item.get("id") == wanted:
                preset = item
                break
        if preset is None:
            return fallback

        result = dict(fallback)
        result["size"] = preset.get("size")
        result["sheet"] = preset.get("sheet")
        result["color"] = preset.get("background")

        layout = preset.get("layout") or {}
        if layout:
            result["rows"] = int(layout.get("rows", 3))
            result["cols"] = int(layout.get("columns", 3))
            result["layout"] = True
        else:
            result["layout"] = False

        color_file = os.path.join(PROJECT_ROOT, "data", "color_%s.csv" % language)
        if result["color"] and os.path.isfile(color_file):
            with open(color_file, "r", encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    if row["Name"] == result["color"]:
                        result["background_hex"] = "#%02X%02X%02X" % (
                            int(row["R"]), int(row["G"]), int(row["B"])
                        )
                        break
        return result
    except Exception:
        return fallback


'''

REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "create_demo 定义前插入辅助函数",
        "def create_demo(initial_language, deployment_mode):",
        HELPER + "def create_demo(initial_language, deployment_mode):",
    ),
    (
        "计算选项后解析预设",
        "    color_choices = [t('custom_color', initial_language)] + list(color_configs.keys())\n"
        "\n"
        "    with gr.Blocks(theme=gr.themes.Soft()) as demo:",
        "    color_choices = [t('custom_color', initial_language)] + list(color_configs.keys())\n"
        "\n"
        "    # --- idphoto preset patch ---\n"
        "    _preset = _idphoto_preset_defaults(initial_language)\n"
        "    _p_size = _preset['size'] if _preset['size'] in photo_size_choices else None\n"
        "    _p_sheet = _preset['sheet'] if _preset['sheet'] in sheet_size_choices else None\n"
        "    _p_color = _preset['color'] if _preset['color'] in color_choices else None\n"
        "\n"
        "    with gr.Blocks(theme=gr.themes.Soft()) as demo:",
    ),
    (
        "规格默认值",
        "                            value=photo_size_choices[0] if photo_size_choices else None",
        "                            value=_p_size or (photo_size_choices[0] if photo_size_choices else None)",
    ),
    (
        "相纸默认值",
        "                            value=sheet_size_choices[0] if sheet_size_choices else None",
        "                            value=_p_sheet or (sheet_size_choices[0] if sheet_size_choices else None)",
    ),
    (
        "底色默认值",
        "                            preset_color = gr.Dropdown(choices=color_choices, "
        "label=t('preset_color', initial_language), value=t('custom_color', initial_language))\n"
        "                            background_color = gr.ColorPicker("
        "label=t('background_color', initial_language), value=\"#FFFFFF\")",
        "                            preset_color = gr.Dropdown(choices=color_choices, "
        "label=t('preset_color', initial_language), "
        "value=_p_color or t('custom_color', initial_language))\n"
        "                            background_color = gr.ColorPicker("
        "label=t('background_color', initial_language), value=_preset['background_hex'])",
    ),
    (
        "是否排版",
        "                        layout = gr.Checkbox(label=t('layout', initial_language), value=True)",
        "                        layout = gr.Checkbox(label=t('layout', initial_language), value=_preset['layout'])",
    ),
    (
        "排版行数",
        "                        sheet_rows = gr.Slider(minimum=1, maximum=10, step=1, value=3, "
        "label=t('sheet_rows', initial_language))",
        "                        sheet_rows = gr.Slider(minimum=1, maximum=10, step=1, "
        "value=_preset['rows'], label=t('sheet_rows', initial_language))",
    ),
    (
        "排版列数",
        "                        sheet_cols = gr.Slider(minimum=1, maximum=10, step=1, value=3, "
        "label=t('sheet_cols', initial_language))",
        "                        sheet_cols = gr.Slider(minimum=1, maximum=10, step=1, "
        "value=_preset['cols'], label=t('sheet_cols', initial_language))",
    ),
]


class PatchError(RuntimeError):
    pass


def apply(package: Path) -> str:
    app_py = package / "src" / "webui" / "app.py"
    if not app_py.is_file():
        raise PatchError(f"找不到上游入口：{app_py}")

    text = app_py.read_text(encoding="utf-8")
    if MARKER in text:
        return "已应用过，跳过"

    for label, old, new in REPLACEMENTS:
        hits = text.count(old)
        if hits != 1:
            raise PatchError(
                f"锚点未唯一命中（{label}）：期望 1 处，实际 {hits} 处。\n"
                f"上游源码可能已变更，请重新核对后再构建。\n锚点片段：\n{old[:160]}"
            )
        text = text.replace(old, new)

    app_py.write_text(text, encoding="utf-8")
    return f"已注入 {len(REPLACEMENTS)} 处"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把预设注入上游 WebUI")
    parser.add_argument("--package", default="dist/staging")
    args = parser.parse_args(argv)

    package = Path(args.package).resolve()
    try:
        print(apply(package))
    except PatchError as exc:
        print(f"[失败] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

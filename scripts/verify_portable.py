"""便携包验收检查（自动化第一层）。

对构建产物做不需要人工介入的检查：结构、预设与规格表一致性、许可文档、运行时隔离。
人工验收覆盖断网、非管理员、中文/空格路径、只读目录等脚本替不了的场景。

用法：
    python scripts/verify_portable.py                      # 检查 dist/staging
    python scripts/verify_portable.py --root <某个解压目录>
    python scripts/verify_portable.py --skip-smoke         # 跳过启动冒烟（无运行时也能跑静态检查）
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shell"))

import assets  # noqa: E402
from portable_checks import (  # noqa: E402
    LAUNCHER_BAT,
    CheckError,
    load_json,
    validate_models,
)

REQUIRED_ENTRIES = [
    LAUNCHER_BAT,
    "launcher.py",
    "portable_checks.py",
    "config/portable.json",
    "config/model-manifest.json",
    "config/presets.json",
    "config/assets.json",
    "licenses/THIRD_PARTY_NOTICES.md",
    "shell/server.py",
    "shell/contracts.py",
    "shell/assets.py",
    "shell/static/index.html",
    "shell/static/app.js",
    "shell/static/style.css",
    "src/webui/app.py",
    "data/size_zh.csv",
    "data/color_zh.csv",
    "python-embed/python.exe",
    "user-data/input",
    "user-data/output",
    "user-data/logs",
    "user-data/config",
]


def ok(message: str) -> None:
    print(f"  [通过] {message}")


def bad(message: str) -> None:
    print(f"  [失败] {message}")


def check_structure(package: Path) -> list[str]:
    problems = []
    for rel in REQUIRED_ENTRIES:
        if not (package / rel).exists():
            problems.append(f"缺少必需文件：{rel}")
    if problems:
        return problems

    marker = "# --- idphoto preset patch ---"
    app_py = package / "src" / "webui" / "app.py"
    if marker not in app_py.read_text(encoding="utf-8"):
        problems.append("预设补丁未注入（src/webui/app.py 缺少标记），界面不会停在预设上")
        return problems

    ok(f"必需文件齐全（{len(REQUIRED_ENTRIES)} 项），预设补丁已注入")
    return []


def load_csv_rows(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return {row["Name"]: row for row in csv.DictReader(stream)}


def check_presets(package: Path) -> list[str]:
    presets = load_json(package / "config" / "presets.json")
    size_rows = load_csv_rows(package / "data" / "size_zh.csv")
    color_rows = load_csv_rows(package / "data" / "color_zh.csv")
    sizes = set(size_rows)
    colors = set(color_rows)

    problems: list[str] = []
    ids = set()
    for preset in presets.get("presets", []):
        pid = preset.get("id")
        if not pid:
            problems.append("存在没有 id 的预设")
            continue
        if pid in ids:
            problems.append(f"预设 id 重复：{pid}")
        ids.add(pid)

        if preset.get("size") not in sizes:
            problems.append(f"预设 {pid} 的尺寸「{preset.get('size')}」不在 size_zh.csv 中")
        if preset.get("background") not in colors:
            problems.append(f"预设 {pid} 的底色「{preset.get('background')}」不在 color_zh.csv 中")

        sheet = preset.get("sheet")
        if sheet is not None and sheet not in sizes:
            problems.append(f"预设 {pid} 的相纸「{sheet}」不在 size_zh.csv 中")

        # 输出模式属于预设层的数据契约：不合法或自相矛盾都要在这里挡住，
        # 而不是等页面把排版按钮点成灰色让用户猜。
        mode = preset.get("mode")
        if mode is not None and mode not in ("single", "sheet"):
            problems.append(f"预设 {pid} 的输出模式「{mode}」不是 single/sheet")
        if mode == "sheet" and not sheet:
            problems.append(f"预设 {pid} 声明 sheet 却没有相纸，切到排版会没有纸可选")

        layout = preset.get("layout")
        if not layout:
            continue
        columns = int(layout.get("columns", 0))
        rows = int(layout.get("rows", 0))
        if columns < 1 or rows < 1:
            problems.append(f"预设 {pid} 的排版行列数非法")
            continue

        # 排版必须真的放得下：这一条能挡住「照抄示例行列数」导致的半张纸空白或裁切
        if preset.get("size") in size_rows and sheet in size_rows:
            photo = size_rows[preset["size"]]
            paper = size_rows[sheet]
            pw, ph = int(photo["ElectronicWidth"]), int(photo["ElectronicHeight"])
            ptw, pth = int(paper["ElectronicWidth"]), int(paper["ElectronicHeight"])
            if columns * pw > ptw:
                problems.append(
                    f"预设 {pid} 排版超宽：{columns} 列 × {pw}px = {columns * pw}px > 相纸 {ptw}px"
                )
            if rows * ph > pth:
                problems.append(
                    f"预设 {pid} 排版超高：{rows} 行 × {ph}px = {rows * ph}px > 相纸 {pth}px"
                )
            if columns * pw <= ptw and rows * ph <= pth:
                util = (columns * pw * rows * ph) / (ptw * pth)
                if util < 0.6:
                    print(f"  [提示] 预设 {pid} 相纸利用率仅 {util:.0%}，可能浪费纸")

    default_id = presets.get("defaults", {}).get("id")
    if default_id not in ids:
        problems.append(f"默认预设 {default_id} 不在预设列表中")

    if problems:
        return problems
    ok(f"预设全部有效（{len(ids)} 个，规格名与排版尺寸均已校验）")
    return []


def check_models(package: Path) -> list[str]:
    config = load_json(package / "config" / "portable.json")
    tier = config["distribution"]["tier"]
    bundled = bool((config.get("models") or {}).get("bundled", True))
    try:
        issues = validate_models(
            package,
            package / config["paths"]["model_manifest"],
            tier,
            build_mode=False,
            require_files=bundled,
        )
    except CheckError as exc:
        return [str(exc)]

    problems = [f"{i.severity}: {i.message}" for i in issues if i.severity == "error"]
    for issue in issues:
        if issue.severity == "warning":
            print(f"  [提示] {issue.message}")
    if problems:
        return problems
    shape = "模型随包" if bundled else "瘦包，运行期下载"
    ok(f"模型完整性与许可状态通过（{tier} 档，{shape}）")
    return []


def check_setup_sources(package: Path, tier: str) -> list[str]:
    """瘦包必须能真的下到模型：manifest 里每个必需模型都要有地址、大小与哈希。"""
    config = load_json(package / "config" / "portable.json")
    manifest = load_json(package / config["paths"]["model_manifest"])
    assets_doc = load_json(package / "config" / "assets.json")
    try:
        plan = assets.build_plan(package, package / "user-data" / "models", manifest, assets_doc, tier)
    except assets.AssetError as exc:
        return [str(exc)]
    if plan.ready:
        return []  # 模型意外随包，不算错
    ok(f"瘦包下载来源完整（{len(plan.missing)} 个模型，约 {plan.total_bytes // 1024 // 1024} MB）")
    return []


def check_licenses(package: Path) -> list[str]:
    notice = package / "licenses" / "THIRD_PARTY_NOTICES.md"
    if not notice.is_file():
        return ["缺少 licenses/THIRD_PARTY_NOTICES.md"]
    text = notice.read_text(encoding="utf-8")
    lowered = text.lower()
    if "replace_with" in lowered or "[review]" in lowered:
        return ["THIRD_PARTY_NOTICES.md 仍含未填写的占位内容"]
    if "agpl" not in lowered:
        return ["THIRD_PARTY_NOTICES.md 未声明上游 AGPL-3.0 义务"]
    if "restricted" not in lowered:
        return ["THIRD_PARTY_NOTICES.md 未标注受限模型，许可状态可能被隐去"]
    ok("第三方声明存在且非模板状态")
    return []


def check_launcher(package: Path) -> list[str]:
    python = package / "python-embed" / "python.exe"
    result = subprocess.run(
        [str(python), str(package / "launcher.py"), "--check-only"],
        cwd=str(package),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return [f"启动器自检未通过：\n{result.stdout}\n{result.stderr}"]
    ok("启动器自检通过")
    return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="便携包验收检查")
    parser.add_argument("--root", default=str(ROOT / "dist" / "staging"))
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--skip-launcher", action="store_true")
    args = parser.parse_args(argv)

    package = Path(args.root).resolve()
    print(f"检查目标：{package}")
    if not package.is_dir():
        bad("目录不存在")
        return 1

    failures: list[str] = []

    print("\n[1] 结构完整性")
    problems = check_structure(package)
    if problems:
        failures += problems
        for item in problems:
            bad(item)

    if not failures:
        print("\n[2] 预设与上游规格表一致性")
        problems = check_presets(package)
        if problems:
            failures += problems
            for item in problems:
                bad(item)

        print("\n[3] 模型完整性与许可状态")
        problems = check_models(package)
        if problems:
            failures += problems
            for item in problems:
                bad(item)

        package_config = load_json(package / "config" / "portable.json")
        if not (package_config.get("models") or {}).get("bundled", True):
            print("\n[3b] 瘦包下载来源")
            problems = check_setup_sources(package, package_config["distribution"]["tier"])
            if problems:
                failures += problems
                for item in problems:
                    bad(item)

        print("\n[4] 第三方许可声明")
        problems = check_licenses(package)
        if problems:
            failures += problems
            for item in problems:
                bad(item)

        if not args.skip_launcher:
            print("\n[5] 启动器自检（空环境）")
            problems = check_launcher(package)
            if problems:
                failures += problems
                for item in problems:
                    bad(item)

    print()
    if failures:
        print(f"结论：FAIL，共 {len(failures)} 项问题")
        return 1
    print("结论：PASS")
    print("注意：断网、非管理员、中文/空格路径、只读目录等场景仍需人工确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""构建 ShiIDPhoto（师兄证照）便携包。

流程：取资产 → 展开 python-embed → 装 wheel → 拷源码/模型/外壳 → 许可门禁 → 冒烟 → 打 ZIP64。
任一步失败即中止，不产出"看起来成功"的包。

用法：
    python scripts/build_portable.py --tier personal
    python scripts/build_portable.py --tier personal --skip-wheels   # 复用已有 wheelhouse
    python scripts/build_portable.py --tier personal --no-models     # 瘦包：首次启动由页面下载模型
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from portable_checks import (  # noqa: E402
    LAUNCHER_BATS,
    CheckError,
    format_issues,
    has_error,
    load_json,
    validate_models,
)
from apply_presets import PatchError, apply as apply_presets  # noqa: E402

PORTABLE_CONFIG = ROOT / "config" / "portable.json"
ASSETS_CONFIG = ROOT / "config" / "assets.json"

# 冒烟测试的模块清单：缺一个就说明依赖没装全
SMOKE_MODULES = ["gradio", "cv2", "onnxruntime", "numpy", "PIL", "pandas", "orjson", "piexif"]


class BuildError(RuntimeError):
    pass


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def _decode(raw) -> str:
    """子进程输出可能是 GBK（Windows 控制台），统一容错解码，避免整条构建崩在解码上。"""
    if isinstance(raw, bytes):
        for encoding in ("utf-8", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    return raw or ""


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(part) for part in command[:6]) + (" …" if len(command) > 6 else ""))
    result = subprocess.run(command, capture_output=True, **kwargs)
    if result.returncode != 0:
        tail = _decode(result.stdout)[-1500:] + _decode(result.stderr)[-1500:]
        raise BuildError(f"命令失败（退出码 {result.returncode}）：\n{tail}")
    return result


def clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 1. 资产
MODEL_ASSET_IDS = {"model_pose", "model_yunet", "model_rmbg"}


def check_assets(assets: dict, tier: str, include_models: bool = True) -> dict[str, Path]:
    cache = ROOT / assets.get("cache_dir", "build-assets")
    resolved: dict[str, Path] = {}
    missing = []
    for asset in assets["assets"]:
        if not include_models and asset["id"] in MODEL_ASSET_IDS:
            continue
        tiers = asset.get("tiers")
        if tiers and tier not in tiers:
            continue
        path = cache / asset["filename"]
        if not path.is_file():
            missing.append(f"{asset['id']}（{path.relative_to(ROOT)}）")
            continue
        expected = asset.get("size_bytes") or 0
        if expected and path.stat().st_size != expected:
            raise BuildError(f"资产 {asset['id']} 大小不符，请重新运行 fetch_assets.py")
        resolved[asset["id"]] = path
    if missing:
        raise BuildError(
            "缺少构建资产：\n  " + "\n  ".join(missing) + "\n请先运行 python scripts/fetch_assets.py"
        )
    return resolved


# ---------------------------------------------------------------- 2. Python
def stage_python(staging: Path, embed_zip: Path) -> Path:
    python_dir = staging / "python-embed"
    python_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(python_dir)

    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    pth_files = list(python_dir.glob("python*._pth"))
    if not pth_files:
        raise BuildError("python-embed 中找不到 python3xx._pth，无法启用 site-packages")
    pth = pth_files[0]
    pth.write_text(
        "\n".join(["python311.zip", ".", "Lib", "Lib\\site-packages", "import site", ""]),
        encoding="utf-8",
    )
    print(f"  已写入 {pth.name}")

    exe = python_dir / "python.exe"
    if not exe.is_file():
        raise BuildError("python-embed 中找不到 python.exe")
    return exe


def build_wheelhouse(assets: dict, wheelhouse: Path, skip: bool) -> None:
    cfg = assets["wheelhouse"]
    if skip and any(wheelhouse.glob("*.whl")):
        print("  复用已有 wheelhouse")
        return
    clean_dir(wheelhouse)
    req_file = wheelhouse / "requirements-portable.txt"
    req_file.write_text("\n".join(cfg["requirements"]) + "\n", encoding="utf-8")

    indexes = cfg.get("index_urls") or [cfg.get("index_url") or "https://pypi.org/simple"]
    last_error: str | None = None
    for index in indexes:
        print(f"  索引：{index}")
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "-r",
            str(req_file),
            "-d",
            str(wheelhouse),
            "--only-binary=:all:",
            "--platform",
            cfg["platform"],
            "--python-version",
            cfg["python_version"],
            "--implementation",
            "cp",
            "--abi",
            "cp311",
            "--index-url",
            index,
            "--timeout",
            "30",
        ]
        try:
            run(command)
        except BuildError as exc:
            last_error = str(exc)
            print("    该索引不可用，尝试下一个")
            clean_dir(wheelhouse)
            req_file.write_text("\n".join(cfg["requirements"]) + "\n", encoding="utf-8")
            continue
        if any(wheelhouse.glob("*.whl")):
            break

    count = len(list(wheelhouse.glob("*.whl")))
    if count == 0:
        raise BuildError("wheelhouse 为空，所有索引均失败：\n" + (last_error or ""))
    print(f"  已下载 {count} 个 wheel")


def install_wheels(wheelhouse: Path, site_packages: Path) -> None:
    installed = 0
    for wheel in sorted(wheelhouse.glob("*.whl")):
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(site_packages)
        installed += 1
    if installed == 0:
        raise BuildError("没有可安装的 wheel")
    print(f"  已展开 {installed} 个 wheel 到 site-packages")


# ---------------------------------------------------------------- 3. 内容
def copy_source(staging: Path, upstream_dir: Path) -> None:
    if not upstream_dir.is_dir():
        raise BuildError(f"缺少上游源码目录：{upstream_dir}")
    for name in ("src", "data"):
        source = upstream_dir / name
        if not source.is_dir():
            raise BuildError(f"上游源码缺少 {name}/，无法构建")
        shutil.copytree(source, staging / name)
    for name in ("LICENSE",):
        source = upstream_dir / name
        if source.is_file():
            shutil.copy2(source, staging / name)
    print("  已复制 src/ data/ LICENSE")


def copy_models(staging: Path, manifest: dict, resolved: dict[str, Path], tier: str) -> None:
    id_to_asset = {
        "pose": "model_pose",
        "face_yunet": "model_yunet",
        "background_removal": "model_rmbg",
    }
    copied = 0
    for entry in manifest["models"]:
        asset_id = entry.get("asset_id") or id_to_asset.get(entry["id"])
        if not asset_id:
            continue
        source = resolved.get(asset_id)
        if source is None:
            continue  # 该档位不包含此资产（例如 public 档排除 RMBG）
        target = staging / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    print(f"  已放入 {copied} 个模型（{tier} 档）")


def copy_shell(staging: Path) -> None:
    for name in ("launcher.py", "portable_checks.py", *LAUNCHER_BATS):
        shutil.copy2(ROOT / name, staging / name)
    for name in ("config", "licenses", "shell"):
        shutil.copytree(ROOT / name, staging / name)
    # docs/ 只放用户手册：既有约定、词汇表、验收手册是开发资料，不进任何发行包。
    # 公开发行包会直接挂到 GitHub Release，整目录复制等于把内部决策一起发出去。
    docs = staging / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    guide = ROOT / "docs" / "PORTABLE_USER_GUIDE.md"
    if not guide.is_file():
        raise BuildError(f"缺少用户手册：{guide}")
    shutil.copy2(guide, docs / "PORTABLE_USER_GUIDE.md")
    print("  已复制启动器、界面、配置、许可与用户手册")


def stamp_version(staging: Path, version: str) -> None:
    """把 --version 落到包内配置。

    否则压缩包名和 release-manifest 是新版本，包内 portable.json（界面顶栏、
    /api/state 的 version）还是旧的——两个版本号迟早对不上。
    """
    path = staging / "config" / "portable.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("version") == version:
        return
    config["version"] = version
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  版本号写入包内配置：{version}")


def stamp_models(staging: Path, bundled: bool) -> None:
    """标记模型是否随包。

    启动器据此决定「模型缺失」是该报错，还是该交给页面引导下载。
    这个标记必须由构建写入，不能让运行期去猜。
    """
    path = staging / "config" / "portable.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    models["bundled"] = bundled
    config["models"] = models
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  模型随包：{'是' if bundled else '否（首次启动由页面下载）'}")


def make_output_link(staging: Path, config: dict) -> None:
    """上游把结果写到 <root>/output，这里用目录联接把它指到用户数据区。"""
    user_data = staging / config["paths"]["user_data"]
    # zip 不保留空目录，必须在构建时把四个子目录建出来，否则用户打开只看到 output
    for name in ("input", "output", "logs", "config", "models"):
        (user_data / name).mkdir(parents=True, exist_ok=True)
    target = (user_data / "output").resolve()
    link = staging / "output"
    if link.exists() or link.is_symlink():
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    if result.returncode != 0:
        # 非 NTFS 等情况退化为普通目录，不阻断构建
        link.mkdir(exist_ok=True)
        print("  提示：无法创建 output 目录联接，已退化为普通目录")
    else:
        print("  已把 output/ 指向 user-data/output/")


# ---------------------------------------------------------------- 4. 门禁与冒烟
def gate(staging: Path, config: dict, tier: str, require_files: bool = True) -> None:
    manifest_path = staging / config["paths"]["model_manifest"]
    issues = validate_models(staging, manifest_path, tier, build_mode=True, require_files=require_files)
    if has_error(issues):
        raise BuildError("许可/完整性门禁未通过：\n" + format_issues(issues))
    warnings = [i for i in issues if i.severity == "warning"]
    if warnings:
        print(format_issues(warnings))
    print(f"  门禁通过（{tier} 档）")


def smoke_test(python_exe: Path, staging: Path) -> None:
    env = {
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PATH": str(python_exe.parent),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
    }
    code = (
        "import importlib, sys, json\n"
        f"mods = {SMOKE_MODULES!r}\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as exc:\n"
        "        missing.append(f'{m}: {exc}')\n"
        "import onnxruntime\n"
        "providers = onnxruntime.get_available_providers()\n"
        "print(json.dumps({'missing': missing, 'providers': providers}, ensure_ascii=False))\n"
        "sys.exit(1 if missing else 0)\n"
    )
    result = subprocess.run(
        [str(python_exe), "-c", code],
        cwd=str(staging),
        env=env,
        capture_output=True,
    )
    stdout = _decode(result.stdout)
    stderr = _decode(result.stderr)
    if result.returncode != 0:
        raise BuildError("依赖冒烟测试失败：\n" + stdout[-2000:] + stderr[-2000:])
    payload = json.loads(stdout.strip().splitlines()[-1])
    providers = payload["providers"]
    if "CPUExecutionProvider" not in providers:
        raise BuildError(f"onnxruntime 未提供 CPUExecutionProvider：{providers}")
    if "CUDAExecutionProvider" in providers:
        raise BuildError("包内是 GPU 版 onnxruntime，发行包必须使用 CPU 版")
    print(f"  import 全部通过，providers={providers}")


# ---------------------------------------------------------------- 5. 打包
def write_manifest(staging: Path, config: dict, tier: str, version: str, models_included: bool = True) -> dict:
    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(staging)).replace("\\", "/"), "size": path.stat().st_size})
    payload = {
        "product": config["product_name"],
        "version": version,
        "tier": tier,
        "platform": config["platform"],
        "models_included": models_included,
        "upstream": config["upstream"],
        "python": config["runtime"]["python"],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(files),
        "files": files,
    }
    (staging / "release-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def write_checksums(staging: Path) -> Path:
    from portable_checks import sha256_file

    lines = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS.txt":
            continue
        rel = str(path.relative_to(staging)).replace("\\", "/")
        if rel.startswith("python-embed/Lib/site-packages/"):
            continue  # 依赖由 wheel 自带哈希覆盖，避免校验文件过大
        lines.append(f"{sha256_file(path)}  {rel}")
    target = staging / "SHA256SUMS.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def make_zip(staging: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())
                count += 1
    print(f"  已写入 {count} 个文件，{output.stat().st_size / 1048576:.1f} MB")


# ---------------------------------------------------------------- 主流程
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="构建师兄证照（ShiIDPhoto）便携包")
    parser.add_argument("--version", default=None)
    parser.add_argument("--tier", default=None, choices=["personal", "public"])
    parser.add_argument("--skip-wheels", action="store_true")
    parser.add_argument("--skip-zip", action="store_true")
    parser.add_argument("--no-models", action="store_true", help="构建瘦包：不放入模型，首次启动由页面引导下载")
    args = parser.parse_args(argv)

    started = time.time()
    try:
        config = load_json(PORTABLE_CONFIG)
        assets = load_json(ASSETS_CONFIG)
        tier = args.tier or config["distribution"]["tier"]
        version = args.version or config["version"]
        manifest = load_json(ROOT / config["paths"]["model_manifest"])

        staging = ROOT / "dist" / "staging"
        wheelhouse = ROOT / "build-assets" / "wheelhouse"

        step("1/7 校验构建资产")
        resolved = check_assets(assets, tier, include_models=not args.no_models)

        step("2/7 准备目录")
        clean_dir(staging)

        step("3/7 展开 Python 运行时")
        python_exe = stage_python(staging, resolved["python_embed"])

        step("4/7 安装依赖")
        build_wheelhouse(assets, wheelhouse, args.skip_wheels)
        install_wheels(wheelhouse, staging / "python-embed" / "Lib" / "site-packages")

        step("5/7 复制源码、模型与外壳")
        copy_source(staging, ROOT / "build-assets" / "upstream")
        print("  " + apply_presets(staging))
        app_py = staging / "src" / "webui" / "app.py"
        compile(app_py.read_text(encoding="utf-8"), str(app_py), "exec")
        print("  补丁后语法检查通过")
        if args.no_models:
            print("  跳过模型（瘦包，首次启动由页面下载）")
        else:
            copy_models(staging, manifest, resolved, tier)
        copy_shell(staging)
        stamp_version(staging, version)
        stamp_models(staging, not args.no_models)
        make_output_link(staging, config)

        step("6/7 许可门禁与冒烟测试")
        gate(staging, config, tier, require_files=not args.no_models)
        smoke_test(python_exe, staging)

        step("7/7 生成清单与压缩包")
        write_manifest(staging, config, tier, version, models_included=not args.no_models)
        write_checksums(staging)
        if args.skip_zip:
            print("  已跳过打包")
        else:
            # 产物名前缀跟品牌走（以前硬写 IdPhoto-，改品牌时这里最容易漏）
            name = (
                f"{config['product_name']}-Portable-v{version}"
                f"-win-x64-{tier}{'-slim' if args.no_models else ''}.zip"
            )
            make_zip(staging, ROOT / "dist" / name)

    except CheckError as exc:
        print(f"\n[构建失败] {exc}")
        return 1
    except PatchError as exc:
        print(f"\n[构建失败] 预设注入未完成：{exc}")
        return 1
    except BuildError as exc:
        print(f"\n[构建失败] {exc}")
        return 1

    print(f"\n构建完成，用时 {time.time() - started:.0f}s。产物在 dist/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

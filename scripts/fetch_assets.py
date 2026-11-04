"""构建期资产下载器。

只做一件事：按 config/assets.json 声明的镜像顺序把资产取回本地缓存，并逐字节校验。
运行期永远不会调用这个脚本（它不进发行包）。

下载/续传/校验的实现与运行期首次向导共用 `shell/assets.py`——两边对「什么算下载成功」
必须有同一个答案，否则构建能过、用户装不上这种事迟早发生。

用法：
    python scripts/fetch_assets.py --tier personal            # 下载并校验
    python scripts/fetch_assets.py --tier personal --pin      # 首次拉取时把真实哈希写回配置
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shell"))

import assets  # noqa: E402

ASSETS_CONFIG = ROOT / "config" / "assets.json"


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def unpack(archive: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        # GitHub archive 顶层是 <repo>-<ref>/，剥掉这一层
        roots = {name.split("/", 1)[0] for name in names if "/" in name}
        strip = len(roots) == 1 and all(name.startswith(next(iter(roots)) + "/") for name in names)
        for member in zf.infolist():
            if member.is_dir():
                continue
            rel = member.filename.split("/", 1)[1] if strip else member.filename
            if not rel:
                continue
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="下载并校验构建资产")
    parser.add_argument("--tier", default="personal", choices=["personal", "public"])
    parser.add_argument("--pin", action="store_true", help="把计算出的 size/sha256 写回 config/assets.json")
    parser.add_argument("--only", default=None, help="只处理指定 id，逗号分隔")
    args = parser.parse_args(argv)

    config = json.loads(ASSETS_CONFIG.read_text(encoding="utf-8"))
    cache = ROOT / config.get("cache_dir", "build-assets")
    cache.mkdir(parents=True, exist_ok=True)

    only = set(args.only.split(",")) if args.only else None
    failures: list[str] = []
    pinned = False

    for asset in config["assets"]:
        asset_id = asset["id"]
        if only and asset_id not in only:
            continue
        tiers = asset.get("tiers")
        if tiers and args.tier not in tiers:
            print(f"[跳过] {asset_id}：不适用于 {args.tier} 档")
            continue

        dest = cache / asset["filename"]
        print(f"\n[{asset_id}] -> {dest.relative_to(ROOT)}")

        def report(text: str) -> None:
            print(f"    {text}")

        def show_progress(done: int, total: int) -> None:
            if total:
                print(f"\r    {human(done)} / {human(total)}", end="", flush=True)

        try:
            assets.fetch_file(
                list(asset.get("urls") or []),
                dest,
                size=int(asset.get("size_bytes") or 0),
                digest=str(asset.get("sha256") or "").strip().lower(),
                log=report,
                progress=show_progress,
            )
        except assets.AssetError as exc:
            print()
            print(f"    下载失败：{exc}")
            failures.append(asset_id)
            continue
        print()

        if args.pin and (not asset.get("sha256") or not asset.get("size_bytes")):
            asset["size_bytes"] = dest.stat().st_size
            asset["sha256"] = assets.sha256_file(dest)
            pinned = True
            print(f"    已回写 size_bytes={asset['size_bytes']} sha256={asset['sha256'][:12]}…")

        target_name = asset.get("unpack_to")
        if target_name:
            unpack(dest, cache / target_name)
            print(f"    已展开到 {(cache / target_name).relative_to(ROOT)}")

    if pinned:
        ASSETS_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("\n已更新 config/assets.json 中的哈希。")

    if failures:
        print(f"\n[失败] 以下资产未能获取：{', '.join(failures)}")
        return 1

    print("\n全部资产就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

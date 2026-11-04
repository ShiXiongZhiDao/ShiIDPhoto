"""模型资产的运行期安装器（纯标准库，不碰图像）。

为什么单独一层：构建期的 `scripts/fetch_assets.py` 和运行期的首次向导做的是同一件事——
按清单取文件、校验大小与 sha256、放到正确位置。两份实现迟早漂移，
所以运行期这份放在 `shell/` 里（会被打进包），构建脚本改为调用它。

边界：
- 只认 `config/model-manifest.json` + `config/assets.json` 里声明过的资产；
- 页面永远不能传 URL 或路径进来，只能传资产 id / 文件字节；
- 下载支持 Range 续传，失败保留 `.part`，换下一个镜像；
- 写盘一律先写临时文件、校验通过后原子替换。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 复用根目录那份哈希实现，避免两套算法漂移
    sys.path.insert(0, str(ROOT))

from portable_checks import SETUP_UA, sha256_file  # noqa: E402

CHUNK = 1 << 20
# 下载模型时带的 UA。品牌常量放在 portable_checks（构建期与运行期共用那一份）
USER_AGENT = SETUP_UA
DISK_RESERVE = 200 * 1024 * 1024  # 给解压/临时文件留的余量

Progress = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class AssetError(RuntimeError):
    """资产不可用：来源缺失、校验失败、磁盘不足等。"""


class Cancelled(RuntimeError):
    """用户主动暂停。"""


@dataclass(frozen=True)
class Asset:
    id: str
    model_id: str
    title: str
    filename: str
    size: int
    sha256: str
    urls: tuple[str, ...]


@dataclass
class Plan:
    home: Path
    missing: list[Asset]
    resolved: dict[str, Path]
    total_bytes: int
    free_bytes: int
    disk_ok: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict:
        return {
            "home": str(self.home),
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "disk_ok": self.disk_ok,
            "warnings": list(self.warnings),
            "missing": [
                {
                    "id": asset.id,
                    "model_id": asset.model_id,
                    "title": asset.title,
                    "filename": asset.filename,
                    "size": asset.size,
                    "state": "pending",
                    "done": 0,
                    "error": None,
                }
                for asset in self.missing
            ],
        }


def verify_file(path: Path, size: int, digest: str) -> bool:
    try:
        if size and path.stat().st_size != size:
            return False
        return not digest or sha256_file(path) == digest.lower()
    except OSError:
        return False


def build_plan(root: Path, home: Path, manifest: dict, assets_doc: dict, tier: str) -> Plan:
    """算出「还缺哪些模型」。

    查找顺序：`user-data/models/<file>` → 包内 `<manifest.path>`。
    两处都没有、或大小/哈希不符，就算缺失；缺失项必须能在 assets.json 里找到来源，
    否则直接报配置错误——瘦包不能出现"要求下载但没有地址"的模型。
    """
    by_id = {str(item.get("id")): item for item in assets_doc.get("assets", []) if isinstance(item, dict)}
    resolved: dict[str, Path] = {}
    missing: list[Asset] = []
    warnings: list[str] = []

    for entry in manifest.get("models", []):
        if not isinstance(entry, dict) or not entry.get("required"):
            continue
        model_id = str(entry.get("id") or "")
        filename = Path(str(entry.get("path") or "")).name
        expected_size = int(entry.get("size_bytes") or 0)
        expected_hash = str(entry.get("sha256") or "").lower()

        found = None
        for candidate in (home / filename, root / str(entry.get("path"))):
            if candidate.is_file() and verify_file(candidate, expected_size, expected_hash):
                found = candidate
                break
        if found is not None:
            resolved[model_id] = found
            continue

        asset = by_id.get(str(entry.get("asset_id") or ""))
        if asset is None:
            raise AssetError(f"模型「{model_id}」没有下载来源（manifest 缺少 asset_id 或 assets.json 无此资产）")

        asset_size = int(asset.get("size_bytes") or 0)
        asset_hash = str(asset.get("sha256") or "").lower()
        if asset_size != expected_size or asset_hash != expected_hash:
            raise AssetError(
                f"模型「{model_id}」在 model-manifest 与 assets.json 中的大小/哈希不一致，拒绝下载"
            )

        tiers = asset.get("tiers")
        if tiers and tier not in tiers:
            raise AssetError(f"模型「{model_id}」不属于 {tier} 档，不能在此时安装")

        urls = tuple(str(url) for url in asset.get("urls") or [] if str(url).strip())
        if not urls:
            raise AssetError(f"模型「{model_id}」的资产条目没有下载地址")

        missing.append(
            Asset(
                id=str(asset.get("id")),
                model_id=model_id,
                title=str(entry.get("feature") or model_id),
                filename=filename,
                size=expected_size,
                sha256=expected_hash,
                urls=urls,
            )
        )

    total = sum(asset.size for asset in missing)
    probe = home
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent  # home 还没建时，用最近的存在祖先取磁盘余量
    free = shutil.disk_usage(probe).free
    disk_ok = free >= total + DISK_RESERVE
    if total and not disk_ok:
        warnings.append(
            f"磁盘剩余 {free // (1024 * 1024)} MB，下载并安装需要约 {(total + DISK_RESERVE) // (1024 * 1024)} MB"
        )
    return Plan(home=home, missing=missing, resolved=resolved, total_bytes=total, free_bytes=free, disk_ok=disk_ok, warnings=warnings)


def download(asset: Asset, home: Path, progress: Progress | None = None, cancelled: CancelCheck | None = None,
             base_url: str | None = None, timeout: float = 30.0) -> Path:
    """下载一个资产并原子落盘。

    `base_url` 只用于测试/内网镜像，把每个地址的 scheme+host 换掉，路径保留。
    """
    home.mkdir(parents=True, exist_ok=True)
    dest = home / asset.filename
    fetch_file(
        list(asset.urls),
        dest,
        size=asset.size,
        digest=asset.sha256,
        progress=progress,
        cancelled=cancelled,
        base_url=base_url,
        timeout=timeout,
    )
    return dest


def fetch_file(
    urls: list[str],
    dest: Path,
    *,
    size: int = 0,
    digest: str = "",
    log: Callable[[str], None] | None = None,
    progress: Progress | None = None,
    cancelled: CancelCheck | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> Path:
    """按顺序试镜像，续传、校验、原子替换。

    构建期（fetch_assets.py）和运行期（首次向导）都走这里，保证「什么算下载成功」
    只有一个答案。`size`/`digest` 为空时只做尽力校验——只有 `--pin` 场景会这样用。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.is_file() and verify_file(dest, size, digest):
        if log:
            log(f"已存在，校验通过：{dest.name}")
        return dest

    if part.is_file() and size and part.stat().st_size == size and verify_file(part, size, digest):
        part.replace(dest)
        if log:
            log(f"续传完成：{dest.name}")
        return dest

    errors: list[str] = []
    for url in urls:
        if cancelled and cancelled():
            raise Cancelled()
        real_url = _with_base(url, base_url)
        if log:
            log(f"尝试 {_host(real_url)}")
        try:
            _fetch(real_url, part, size, progress, cancelled, timeout)
        except Cancelled:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, AssetError) as exc:
            errors.append(f"{_host(real_url)}：{exc}")
            continue

        if verify_file(part, size, digest):
            part.replace(dest)
            return dest

        errors.append(f"{_host(real_url)}：文件校验未通过（镜像可能返回了错误页）")
        part.unlink(missing_ok=True)

    raise AssetError("；".join(errors) or "没有可用的下载地址")


def write_local(data: bytes, plan: Plan) -> Asset:
    """把用户手动提供的文件认领成某个缺失资产，并写入模型目录。"""
    digest = hashlib.sha256(data).hexdigest()
    for asset in plan.missing:
        if asset.size == len(data) and asset.sha256 == digest:
            home = plan.home
            home.mkdir(parents=True, exist_ok=True)
            temp = home / (asset.filename + ".local.tmp")
            temp.write_bytes(data)
            if not verify_file(temp, asset.size, asset.sha256):  # 理论上不会发生，防写坏
                temp.unlink(missing_ok=True)
                raise AssetError("写入后校验失败，磁盘可能有问题")
            temp.replace(home / asset.filename)
            return asset
    raise AssetError("这个文件与所需模型都对不上：大小或 sha256 不符")


def _fetch(url: str, part: Path, size: int, progress: Progress | None, cancelled: CancelCheck | None,
           timeout: float) -> None:
    offset = part.stat().st_size if part.is_file() else 0
    if size and offset > size:
        part.unlink(missing_ok=True)
        offset = 0

    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if offset and getattr(response, "status", 200) != 206:
            offset = 0  # 服务器不支持 Range，只能从头写

        done = offset
        if progress:
            progress(done, size)

        with part.open("ab" if offset else "wb") as stream:
            while True:
                if cancelled and cancelled():
                    raise Cancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                stream.write(chunk)
                done += len(chunk)
                if size and done > size:
                    raise AssetError("下载内容超出预期大小")
                if progress:
                    progress(done, size)

    if size and (not part.is_file() or part.stat().st_size != size):
        actual = part.stat().st_size if part.is_file() else 0
        raise AssetError(f"下载不完整（{actual}/{size}）")


def _with_base(url: str, base_url: str | None) -> str:
    if not base_url:
        return url
    path = urllib.parse.urlsplit(url).path
    return base_url.rstrip("/") + path


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def default_base_url() -> str | None:
    """测试与内网镜像用：IDPHOTO_ASSET_BASE 覆盖下载域名，路径不变。"""
    return os.environ.get("IDPHOTO_ASSET_BASE") or None

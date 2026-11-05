"""共享校验逻辑。

启动器（运行期）和构建脚本（构建期）都调用这里，避免两套判断标准漂移。
任何一处判定"不许启动/不许打包"，都必须来自同一个函数。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# 发行档位 -> 允许出现的模型再分发状态
ALLOWED_STATUS_BY_TIER: dict[str, set[str]] = {
    "personal": {"approved", "pending", "restricted"},
    "public": {"approved"},
}

VALID_STATUS = {"approved", "pending", "restricted"}

# 构建期禁止出现在清单里的占位符，防止把模板当成真实配置发布
PLACEHOLDERS = ("REPLACE", "TODO", "unknown", "REVIEW_REQUIRED")

# 用户双击的那个文件名。它由品牌决定，但**不能**从 config 读出来——读配置要先知道
# 配置文件在哪，而这条正是"包结构检查"要验证的东西，所以写成常量。
# 风险在于它有三处消费者（构建复制、验收必需项、启动器自检），漏一处就会报成
# "缺少必需文件"，看起来像打包出错，其实是改名没改全。所以由
# tests/test_gates.py::BrandTests 钉住：这个常量必须含 config 的 product_title。
LAUNCHER_BAT = "双击启动师兄证照.bat"

#: 同一份入口脚本的英文名。内容与中文那份逐字相同（两边都是纯 ASCII），
#: 存在的理由是：中文名在解压工具与命令行这两条路上都可能被代码页弄乱，而英文文档
#: 的读者本来也不该被要求去敲一个中文文件名。两份一起进包，也一起出现在公开源码里。
LAUNCHER_BAT_EN = "Launch-ShiIDPhoto.bat"

#: 构建与验收一律遍历这份元组，别再各写各的——两处清单分开之后，漏拷的那份
#: 表现是"用户看不见它"，不是"构建失败"。
LAUNCHER_BATS = (LAUNCHER_BAT, LAUNCHER_BAT_EN)

#: 拉丁品牌名。唯一来源是 `config/portable.json` 的 `product_name`，但下面这两个
#: 协议串要在配置读出来之前就有值（HTTP 头、下载 UA），所以在这里落成常量，
#: 由 tests/test_gates.py::BrandTests 钉住与 config 一致。
PRODUCT_LATIN = "ShiIDPhoto"

# 启动器探活用的 User-Agent（launcher.py）
HEALTH_UA = f"{PRODUCT_LATIN}-Portable-HealthCheck"

# 首次安装向导下载模型时带的 User-Agent（shell/assets.py）
SETUP_UA = f"{PRODUCT_LATIN}-Setup/1.0"


class CheckError(RuntimeError):
    """不可继续的校验失败。"""


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.severity}] {self.code}: {self.message}"


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckError(f"找不到配置文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"配置文件不是合法 JSON：{path}（{exc}）") from exc


def resolve_inside(root: Path, relative: str) -> Path:
    """把清单里的相对路径解析成绝对路径，并防止 ../ 越界。"""
    if not isinstance(relative, str) or not relative.strip():
        raise CheckError("模型清单中存在空路径")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise CheckError(f"路径越出程序目录，已拒绝：{relative}") from exc
    return candidate


def _model_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    models = manifest.get("models")
    if not isinstance(models, list):
        raise CheckError("模型清单格式错误：models 必须是数组")
    return [item for item in models if isinstance(item, dict)]


def _placeholder_hits(entry: dict[str, Any]) -> list[str]:
    hits: list[str] = []
    raw = json.dumps(entry, ensure_ascii=False)
    for token in PLACEHOLDERS:
        if token in raw:
            hits.append(token)
    return hits


def validate_models(
    root: Path,
    manifest_path: Path,
    tier: str,
    *,
    build_mode: bool = False,
    verify_hash: bool = True,
    require_files: bool = True,
) -> list[Issue]:
    """校验模型清单与实际文件。

    build_mode=True 时执行更严格的检查（占位符、许可证字段完整性）。
    require_files=False 用于「瘦包」：模型不在包里，由首次启动向导下载；
    此时不要求文件存在，但许可判定照做——瘦包同样不能夹带 public 档不允许的权重。
    返回问题列表；调用方决定 error 是否致命。
    """
    if tier not in ALLOWED_STATUS_BY_TIER:
        raise CheckError(f"未知的发行档位：{tier}（可选：{', '.join(ALLOWED_STATUS_BY_TIER)}）")

    manifest = load_json(manifest_path)
    issues: list[Issue] = []
    allowed_status = ALLOWED_STATUS_BY_TIER[tier]
    seen_ids: set[str] = set()

    def check_license(entry: dict[str, Any], model_id: str) -> None:
        """许可判定与文件是否存在无关——瘦包里它照样要过。"""
        license_info = entry.get("license") or {}
        status = str(license_info.get("redistribution_status") or "").strip()
        if status not in VALID_STATUS:
            issues.append(
                Issue("error", "license.bad_status", f"{model_id}：redistribution_status 非法（{status or '空'}）")
            )
        elif status not in allowed_status:
            issues.append(
                Issue(
                    "error",
                    "license.not_allowed",
                    f"{model_id}：许可状态为 {status}，不允许进入 {tier} 档发行包",
                )
            )
        if not build_mode:
            return
        for field in ("spdx", "url", "review_basis"):
            if not str(license_info.get(field) or "").strip():
                issues.append(
                    Issue("error", "license.incomplete", f"{model_id}：license.{field} 为空，不能进入发行包")
                )
        if not str((entry.get("source") or {}).get("url") or "").strip():
            issues.append(Issue("error", "source.missing", f"{model_id}：缺少 source.url"))
        hits = _placeholder_hits(entry)
        if hits:
            issues.append(
                Issue("error", "manifest.placeholder", f"{model_id}：含有占位内容 {hits}，请填写真实数据")
            )

    for entry in _model_entries(manifest):
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            issues.append(Issue("error", "manifest.no_id", "模型清单存在缺少 id 的条目"))
            continue
        if model_id in seen_ids:
            issues.append(Issue("error", "manifest.duplicate_id", f"模型 id 重复：{model_id}"))
        seen_ids.add(model_id)

        required = bool(entry.get("required", False))
        expected_hash = str(entry.get("sha256") or "").strip().lower()
        rel_path = entry.get("path")

        # 1) 路径合法性与文件存在性
        try:
            model_path = resolve_inside(root, str(rel_path))
        except CheckError as exc:
            issues.append(Issue("error", "path.invalid", str(exc)))
            continue

        if not model_path.is_file():
            if required:
                if require_files:
                    issues.append(
                        Issue("error", "model.missing", f"缺少必要 AI 模型「{model_id}」，路径：{rel_path}")
                    )
                elif build_mode:
                    # 瘦包：文件不在，但它将来会被下载，许可判定现在就要过
                    check_license(entry, model_id)
            else:
                issues.append(
                    Issue("warning", "model.optional_missing", f"可选 AI 模型「{model_id}」未安装，相关功能将不可用")
                )
            # 文件不存在就不会进包，其完整性无需判定
            continue

        # 2) 再分发状态与构建期字段
        check_license(entry, model_id)

        if not require_files:
            # 瘦包：模型由首次启动向导负责，文件不必在包里；损坏/缺失都交给运行期自愈
            continue

        expected_size = entry.get("size_bytes")
        actual_size = model_path.stat().st_size
        if isinstance(expected_size, int) and expected_size > 0 and actual_size != expected_size:
            issues.append(
                Issue(
                    "error",
                    "model.size_mismatch",
                    f"AI 模型「{model_id}」大小不符：期望 {expected_size} 字节，实际 {actual_size} 字节",
                )
            )
            continue

        if build_mode and not expected_hash:
            issues.append(
                Issue("error", "hash.empty", f"{model_id}：文件存在但清单未提供 sha256，不能进入发行包")
            )
            continue

        if not expected_hash:
            issues.append(
                Issue("warning", "hash.missing", f"{model_id}：清单未提供 sha256，本次跳过完整性校验")
            )

        if verify_hash and expected_hash:
            actual_hash = sha256_file(model_path)
            if actual_hash != expected_hash:
                issues.append(
                    Issue("error", "model.hash_mismatch", f"AI 模型「{model_id}」校验未通过，文件可能损坏")
                )

    return issues


def has_error(issues: Iterable[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def format_issues(issues: Iterable[Issue]) -> str:
    lines = []
    for issue in issues:
        prefix = "错误" if issue.severity == "error" else "提示"
        lines.append(f"  [{prefix}] {issue.message}")
    return "\n".join(lines)

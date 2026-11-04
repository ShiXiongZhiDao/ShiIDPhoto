"""师兄证照（ShiIDPhoto）本地服务。

职责：把上游的图像处理能力暴露成一组 JSON 接口，供自研前端调用。
刻意不重写任何算法——处理管线仍然走 `src/webui/app.py:process_image`，
这样上游更新时我们只需要跟着改接口层。

只监听 127.0.0.1，不启用任何对外访问。图片不出本机。

接口：
    GET  /                 前端页面
    GET  /static/*         静态资源
    GET  /api/state        规格、底色、预设、版本
    POST /api/process      处理一张图（body 为原始图片字节）
    GET  /out/<name>       取回生成的文件
    POST /api/open-folder  在资源管理器里打开输出目录
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import warnings
from contextlib import ExitStack, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SHELL_DIR = ROOT / "shell"
sys.path.insert(0, str(SHELL_DIR))  # 让 contracts / assets 在"脚本方式"和"模块方式"下都能导入

import assets  # noqa: E402  (依赖上面的 sys.path)
import contracts  # noqa: E402
import custom_sizes  # noqa: E402
import imaging  # noqa: E402
import prefs  # noqa: E402

STATIC_DIR = SHELL_DIR / "static"
APP_PY = ROOT / "src" / "webui" / "app.py"
CONFIG_PATH = ROOT / "config" / "portable.json"
OUTPUT_DIR = ROOT / "user-data" / "output"
INPUT_DIR = ROOT / "user-data" / "input"
LOG_DIR = ROOT / "user-data" / "logs"
# 自定义规格是用户数据，和模型一样不进 config/（覆盖安装会被替换）
CUSTOM_PATH = ROOT / "user-data" / "custom-sizes.json"
# 界面选择也进 user-data：浏览器那份按端口分桶，端口一漂就等于没有
PREFS_PATH = ROOT / "user-data" / "prefs.json"

# 前端用这个伪规格名表示"这次用自定义尺寸"，不会与 CSV 里的中文名冲突
CUSTOM_SIZE_KEY = "__custom__"

MAX_UPLOAD = 40 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

_processing_lock = threading.Lock()
_state_lock = threading.Lock()

# 服务先绑定端口，模型在后台加载；前端据此显示进度，而不是白屏干等。
STATE: dict = {
    "ready": False,
    "mode": "starting",  # starting | setup | loading | ready | error
    "stage": "正在启动",
    "error": None,
    "setup": None,
}
STATE_LOCK = threading.Lock()
SETUP_LOCK = threading.Lock()

# 启动时读一次。presets.json 在运行期视为不变（改完重启即可），
# 每次请求重读会让 defaults 里的非法值反复刷日志，UI 也会在两次刷新间跳变。
PRESETS: dict = {}
DEFAULT_PRESET_ID: str | None = None
OUTPUT_DEFAULTS: dict = dict(contracts.DEFAULT_OUTPUT)

#: 品牌串。唯一来源是 config/portable.json，`main()` 读到配置后填这里，
#: 就绪日志与 HTTP `Server` 头都从这里取——不再各写一份字面量。
#: 兜底值不带品牌：配置读不出来时宁可朴素，也不印一个作废的名字。
BRAND: dict = {"title": "证件照工具", "latin": "photo", "tagline": ""}


def set_brand(cfg: dict) -> None:
    """把 config 里的品牌字段搬进 BRAND，并同步 HTTP 响应头。"""
    BRAND["latin"] = str(cfg.get("product_name") or "").strip() or BRAND["latin"]
    BRAND["title"] = str(cfg.get("product_title") or "").strip() or BRAND["latin"]
    BRAND["tagline"] = str(cfg.get("product_tagline") or "").strip()
    Handler.server_version = f"{BRAND['latin']}/1.0"


def log(message: str) -> None:
    print(message, flush=True)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_presets() -> dict:
    return json.loads((ROOT / load_config()["paths"]["presets"]).read_text(encoding="utf-8"))


def prepare_contracts() -> None:
    """把 presets.json 解释成运行期要用的三样东西。

    包括：打开页面停在哪个预设（启动参数优先）、出图用哪套排版参数。
    这两件事以前各断一头——`--preset` 只对 --classic 生效，defaults 里四个字段
    被 server 硬编码覆盖。放在 bootstrap 里做，失败就走既有的"页面显示原因"路径。
    """
    global PRESETS, DEFAULT_PRESET_ID, OUTPUT_DEFAULTS

    PRESETS = load_presets()

    preset, note = contracts.resolve_preset(PRESETS, os.environ.get("IDPHOTO_PRESET"))
    if note:
        log(f"[预设] {note}")
    DEFAULT_PRESET_ID = preset.get("id") if preset else None
    if DEFAULT_PRESET_ID:
        log(f"默认预设：{DEFAULT_PRESET_ID}")

    defaults, notes = contracts.output_defaults(PRESETS)
    for item in notes:
        log(f"[预设] {item}")
    if defaults["compress"] and not (ROOT / "src" / "tool" / "ext" / "pngquant.exe").is_file():
        # 压缩依赖 pngquant（GPL，未随包）。配置开了但工具不在时不能装作没看见，
        # 也不能让上游在首次出图时炸掉——关掉并说明。
        log("[预设] defaults.compress=true，但包里没有 pngquant，已关闭压缩")
        defaults["compress"] = False
    OUTPUT_DEFAULTS = defaults


# --------------------------------------------------------------------------
# 首次安装（瘦包）：模型不在包里时，由页面引导下载
# --------------------------------------------------------------------------
def model_home() -> Path:
    return ROOT / load_config()["paths"]["user_data"] / "models"


def load_manifest() -> dict:
    return json.loads((ROOT / load_config()["paths"]["model_manifest"]).read_text(encoding="utf-8"))


def load_assets_doc() -> dict:
    return json.loads((ROOT / "config" / "assets.json").read_text(encoding="utf-8"))


def plan_models() -> assets.Plan:
    cfg = load_config()
    home = model_home()
    home.mkdir(parents=True, exist_ok=True)
    return assets.build_plan(
        ROOT,
        home,
        load_manifest(),
        load_assets_doc(),
        str(cfg.get("distribution", {}).get("tier", "personal")),
    )


def status_snapshot() -> dict:
    with STATE_LOCK:
        return {
            "ok": STATE["error"] is None,
            "ready": bool(STATE["ready"]),
            "mode": STATE["mode"],
            "stage": STATE["stage"],
            "error": STATE["error"],
            "setup": copy.deepcopy(STATE["setup"]),
            # 品牌串：界面顶栏与 <title> 从这里取，HTML 里那份只是服务还没连上时的兜底，
            # 两者由 tests/test_gates.py::BrandTests 钉成同源
            "brand": dict(BRAND),
        }


def activate_setup(plan: assets.Plan, note: str | None = None) -> None:
    payload = plan.to_dict()
    payload.update({"installing": False, "cancel": False, "paused": False, "error": note})
    with STATE_LOCK:
        STATE.update({"ready": False, "mode": "setup", "stage": "等待首次设置", "error": None, "setup": payload})


def load_upstream(paths: dict[str, Path]) -> None:
    """模型齐备后建立会话并进入就绪态。失败一律进 error，不留在半加载状态。"""
    try:
        with STATE_LOCK:
            STATE["mode"] = "loading"
            STATE["stage"] = "正在加载模型与规格表"
        upstream = Upstream(on_stage=lambda text: STATE.__setitem__("stage", text), model_paths=paths)
        catalog = upstream.catalog()
        Handler.upstream = upstream
        with STATE_LOCK:
            STATE.update({"ready": True, "mode": "ready", "stage": "就绪", "error": None, "setup": None})
        log(f"已加载 {len(catalog['photos'])} 个照片规格 / {len(catalog['colors'])} 个底色")
        log(f"{BRAND['title']} 服务已就绪")
    except Exception as exc:  # noqa: BLE001
        with STATE_LOCK:
            STATE.update({"ready": False, "mode": "error", "stage": "加载失败", "error": str(exc)})
        log(f"[启动失败] {exc}")
        log(traceback.format_exc())


def start_install(plan: assets.Plan) -> bool:
    if not SETUP_LOCK.acquire(blocking=False):
        return False
    threading.Thread(target=install_worker, args=(plan,), daemon=True).start()
    return True


def install_worker(plan: assets.Plan) -> None:
    setup = STATE["setup"]

    def cancelled() -> bool:
        return bool(setup.get("cancel"))

    try:
        with STATE_LOCK:
            setup["installing"] = True
            setup["paused"] = False
            setup["error"] = None

        # 小的先下：试印版按 1→2→3 依次显影，用户能更早看到进度在动
        for item in sorted(setup["missing"], key=lambda row: row["size"]):
            if item["state"] == "done":
                continue
            if cancelled():
                raise assets.Cancelled()
            asset = next((a for a in plan.missing if a.model_id == item["model_id"]), None)
            if asset is None:
                continue
            with STATE_LOCK:
                item["state"] = "downloading"
                item["done"] = 0
                item["error"] = None

            def progress(done: int, _total: int, item=item) -> None:
                with STATE_LOCK:
                    item["done"] = done

            assets.download(
                asset,
                model_home(),
                progress=progress,
                cancelled=cancelled,
                base_url=assets.default_base_url(),
            )
            with STATE_LOCK:
                item["state"] = "done"
                item["done"] = item["size"]
            log(f"  [模型] {item['title']} 下载完成")

        if cancelled():
            raise assets.Cancelled()

        fresh = plan_models()
        if not fresh.ready:
            missing = "、".join(a.title for a in fresh.missing)
            with STATE_LOCK:
                setup["error"] = f"仍有模型未就绪：{missing}"
            return

        load_upstream(fresh.resolved)
    except assets.Cancelled:
        with STATE_LOCK:
            setup["paused"] = True
            setup["error"] = "已暂停；已下载的部分会保留，点「继续下载」接着来。"
            for item in setup["missing"]:
                if item["state"] == "downloading":
                    item["state"] = "pending"
    except Exception as exc:  # noqa: BLE001
        with STATE_LOCK:
            setup["paused"] = False
            setup["error"] = str(exc)
            for item in setup["missing"]:
                if item["state"] == "downloading":
                    item["state"] = "failed"
                    item["error"] = str(exc)
        log(f"[安装失败] {exc}")
    finally:
        with STATE_LOCK:
            setup["installing"] = False
        SETUP_LOCK.release()


def install_local(data: bytes) -> str:
    """用户手动提供的文件：按 sha256 认领，写进模型目录。"""
    plan = plan_models()
    if plan.ready:
        raise assets.AssetError("模型已经齐备，不需要安装")
    asset = assets.write_local(data, plan)
    fresh = plan_models()
    if fresh.ready:
        threading.Thread(target=load_upstream, args=(fresh.resolved,), daemon=True).start()
    else:
        activate_setup(fresh, note=f"已安装「{asset.title}」，还缺 {len(fresh.missing)} 个")
    return asset.model_id


def install_session_cache() -> None:
    """让 onnxruntime 的模型会话在整个进程内只建一次。

    上游的 `ImageProcessor` 每次处理都会重新 `ImageSegmentation(...)`，
    而建一个 RMBG 会话实测要 2.5–3.7 秒——这是单次请求十几秒的主要开销。
    这里在 ORT 层做会话缓存：同一路径的会话复用，模型加载从"每张图一次"变成"启动一次"。
    不改任何算法，也不改上游调用方式。
    """
    import onnxruntime as ort

    cache: dict[str, object] = {}
    key_attr = "_idphoto_session_key"
    ready_attr = "_idphoto_session_ready"
    orig_new = ort.InferenceSession.__new__
    orig_init = ort.InferenceSession.__init__

    def session_key(path_or_bytes) -> str | None:
        if isinstance(path_or_bytes, str):
            return os.path.abspath(path_or_bytes)
        if isinstance(path_or_bytes, bytes) and len(path_or_bytes) < 4096:
            return f"bytes:{hash(path_or_bytes)}"
        return None  # 大模型的内存字节流不缓存

    def patched_new(cls, path_or_bytes=None, *_a, **_kw):
        key = session_key(path_or_bytes)
        if key and key in cache:
            return cache[key]
        obj = orig_new(cls)
        if key:
            setattr(obj, key_attr, key)
        return obj

    def patched_init(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kwargs):
        if getattr(self, ready_attr, False):
            return
        orig_init(self, path_or_bytes, sess_options, providers, provider_options, **kwargs)
        setattr(self, ready_attr, True)
        key = getattr(self, key_attr, None)
        if key:
            cache[key] = self

    ort.InferenceSession.__new__ = patched_new
    ort.InferenceSession.__init__ = patched_init


# --------------------------------------------------------------------------
# 上游模块（延迟到启动时加载一次，保持进程内模型常驻）
# --------------------------------------------------------------------------
class Upstream:
    def __init__(self, on_stage=None, model_paths: dict[str, Path] | None = None) -> None:
        if not APP_PY.is_file():
            raise RuntimeError(f"缺少上游入口：{APP_PY}")
        if on_stage:
            on_stage("正在加载处理模块")
        spec = importlib.util.spec_from_file_location("idphoto_upstream_app", APP_PY)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载上游模块")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        install_session_cache()

        self.module = module
        self.model_dir = Path(module.MODEL_DIR)
        if model_paths:
            # 模型可能来自 user-data/models 或包内 src/model，由调用方解析好再传进来
            missing = {"pose", "face_yunet", "background_removal"} - set(model_paths)
            if missing:
                raise RuntimeError(f"模型路径解析不完整：{'、'.join(sorted(missing))}")
            self.pose = Path(model_paths["pose"])
            self.yunet = Path(model_paths["face_yunet"])
            self.rmbg = Path(model_paths["background_removal"])
        else:
            self.pose = self.model_dir / "yolov8n-pose.onnx"
            self.yunet = self.model_dir / "face_detection_yunet_2023mar.onnx"
            self.rmbg = self.model_dir / "RMBG-1.4-model.onnx"
        for path in (self.pose, self.yunet, self.rmbg):
            if not path.is_file():
                raise RuntimeError(f"缺少模型：{path}")

        # 启动时就把三个会话真正建一遍：ORT 只在校验模型头时才会发现文件损坏，
        # 光检查文件存在会让"模型坏了"拖到用户第一次生成时才暴露。
        import onnxruntime as ort

        for label, path in (("姿态", self.pose), ("人脸", self.yunet), ("抠图", self.rmbg)):
            if on_stage:
                on_stage(f"正在加载{label}模型")
            ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])

        if on_stage:
            on_stage("正在读取规格与底色表")
        cfg = load_config()
        lang = cfg.get("webui", {}).get("lang", "zh")
        self.requirements = module.PhotoRequirements(language=lang)
        self.manager = module.ConfigManager(language=lang)

    def catalog(self) -> dict:
        photos = self.manager.get_photo_size_configs()
        sheets = self.manager.get_sheet_size_configs()
        colors = self.manager.color_config

        def shape(item: dict) -> dict:
            return {
                "w": int(item["ElectronicWidth"]),
                "h": int(item["ElectronicHeight"]),
                "print_w": float(item["PrintWidth"]),
                "print_h": float(item["PrintHeight"]),
                "dpi": int(item["Resolution"]),
            }

        return {
            "photos": [
                {"name": name, **shape(cfg)} for name, cfg in photos.items()
            ],
            "sheets": [
                {"name": name, **shape(cfg)} for name, cfg in sheets.items()
            ],
            "colors": [
                {"name": name, "hex": "#%02X%02X%02X" % (c["R"], c["G"], c["B"]),
                 "rgb": [c["R"], c["G"], c["B"]]}
                for name, c in colors.items()
            ],
        }

    def resolution_for(self, size_name: str) -> int:
        info = self.requirements.get_resize_image_list(size_name)
        return int(info.get("resolution", 300))


# --------------------------------------------------------------------------
# 处理
# --------------------------------------------------------------------------
def _kb(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def size_notes(label: str, encoded: imaging.Encoded, max_kb: int | None) -> list[str]:
    """体积上限的如实反馈。

    两种沉默都不可接受：压不到却装作压到了，用户上传时才被打回；
    压到了但质量已经烂了，用户打印时才发现脸是块状的。
    """
    if max_kb is None:
        return []
    if not encoded.hit_target:
        return [f"{label}最低只能压到 {_kb(encoded.size)}，超过上限 {max_kb} KB。"]
    if encoded.quality is not None and encoded.quality < contracts.LOW_QUALITY_WARN:
        return [f"{label}为压到 {max_kb} KB，质量降到 {encoded.quality}，画质可能不合格。"]
    return []


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return stem[:40] or "photo"


def handle_process(up: Upstream, body: bytes, query: dict) -> dict:
    started = time.time()

    filename = query.get("name", "photo.jpg")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXT:
        suffix = ".jpg"
    if not body:
        raise ValueError("没有收到图片数据")

    size = query.get("size", "一寸")
    color = query.get("bg", "白色")
    sheet = query.get("sheet", "六寸")
    mode = query.get("mode", "sheet")
    if mode not in ("single", "sheet"):
        raise ValueError(f"未知输出模式：{mode}")
    fmt = contracts.normalize_format(query.get("fmt"))
    quality = contracts.normalize_quality(query.get("quality"))
    max_kb = contracts.normalize_max_kb(query.get("max_kb"))

    # 行列数来自前端，不能直接喂给排版器：999×999 会让它在内存里铺一张巨图再抛错。
    try:
        rows = int(query.get("rows", "4"))
        cols = int(query.get("cols", "4"))
    except ValueError as exc:
        raise ValueError("排版行列数必须是整数") from exc
    if not (1 <= rows <= 32 and 1 <= cols <= 32):
        raise ValueError(f"排版行列数超出范围（1–32）：{rows}×{cols}")

    catalog = up.catalog()
    color_item = next((c for c in catalog["colors"] if c["name"] == color), None)
    if color_item is None:
        raise ValueError(f"未知底色：{color}")

    custom_spec = None
    custom_entry = None
    if size == CUSTOM_SIZE_KEY:
        # 数值来自前端，一律走同一套校验；不信任"界面已经检查过了"
        custom_spec = custom_sizes.normalize(
            width=query.get("w"),
            height=query.get("h"),
            dpi=query.get("dpi", 300),
            mm_w=query.get("mm_w"),
            mm_h=query.get("mm_h"),
        )
        custom_entry = custom_sizes.for_upstream(
            custom_spec["width"], custom_spec["height"], custom_spec["dpi"],
            custom_spec["mm_w"], custom_spec["mm_h"],
        )
    elif size not in [p["name"] for p in catalog["photos"]]:
        raise ValueError(f"未知规格：{size}")
    if mode == "sheet" and sheet not in [s["name"] for s in catalog["sheets"]]:
        raise ValueError(f"未知相纸：{sheet}")

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = f"{safe_stem(filename)}_{stamp}"
    src_path = INPUT_DIR / f"{unique}{suffix}"
    src_path.write_bytes(body)

    # ExitStack 只为了在自定义规格时包一层 try/finally，同时不改变下面整块的缩进
    with _processing_lock, ExitStack() as stack:
        photo_type = size
        if custom_entry is not None:
            config_manager = up.requirements.config_manager
            photo_type = stack.enter_context(custom_sizes.injected(config_manager, custom_entry))
        # 上游遇到"没检测到人"只用 warnings.warn 说一声，然后退化成整图裁切，
        # 接口照样返回成功。不接住它，用户拿到的是"处理完成"+一张不可用的图。
        # catch_warnings 是进程级状态，不能多线程共用——这里在 _processing_lock 里，安全。
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = up.module.process_image(
                str(src_path),
                str(up.pose),
                str(up.yunet),
                str(up.rmbg),
                up.requirements,
                photo_type=photo_type,
                photo_sheet_size=sheet,
                rgb_list=color_item["rgb"],
                compress=OUTPUT_DEFAULTS["compress"],
                change_background=True,
                rotate=False,
                resize=True,
                ratio_crop=False,
                layout=(mode == "sheet"),
                sheet_rows=rows,
                sheet_cols=cols,
                add_crop_lines=OUTPUT_DEFAULTS["add_crop_lines"],
                layout_position=OUTPUT_DEFAULTS["layout_position"],
                photos_spacing=OUTPUT_DEFAULTS["photos_spacing"],
            )

        dpi = custom_spec["dpi"] if custom_spec else up.resolution_for(size)
        single_cv = result["corrected_image"]
        single_path = OUTPUT_DIR / f"{unique}.{fmt}"
        # 上游的 process_image 返回 BGR，而 PIL 按 RGB 存——必须先转，否则肤色发蓝。
        # 保存走 shell/imaging：上游的 save_image 不收 quality，也不做体积约束。
        single_enc = imaging.fit_to_size(
            Image.fromarray(cv2.cvtColor(single_cv, cv2.COLOR_BGR2RGB)), fmt, dpi, quality, max_kb
        )
        imaging.save(single_enc, single_path)

        sheet_path = None
        sheet_enc = None
        if mode == "sheet":
            sheet_cv = result["final_image"]
            sheet_path = OUTPUT_DIR / f"{unique}_sheet.{fmt}"
            sheet_enc = imaging.fit_to_size(
                Image.fromarray(cv2.cvtColor(sheet_cv, cv2.COLOR_BGR2RGB)), fmt, dpi, quality, max_kb
            )
            imaging.save(sheet_enc, sheet_path)

    single_h, single_w = single_cv.shape[:2]
    notes = contracts.translate_warnings(str(item.message) for item in caught)
    if notes:
        log(f"[处理提示] {notes[0]}")
    payload = {
        "ok": True,
        "id": unique,
        "size": custom_spec["name"] if custom_spec else size,
        "size_custom": custom_spec,
        "color": color,
        "sheet": sheet if mode == "sheet" else None,
        "format": fmt,
        "quality": single_enc.quality,
        "max_kb": max_kb,
        "single": {
            "url": f"/out/{single_path.name}",
            "w": int(single_w),
            "h": int(single_h),
            "dpi": dpi,
            "bytes": single_enc.size,
        },
        "ms": int((time.time() - started) * 1000),
        "warnings": notes,
        "size_notes": [],
    }
    payload["size_notes"] += size_notes("单张", single_enc, max_kb)
    if sheet_path is not None:
        sheet_h, sheet_w = result["final_image"].shape[:2]
        payload["sheet_image"] = {
            "url": f"/out/{sheet_path.name}",
            "w": int(sheet_w),
            "h": int(sheet_h),
            "dpi": dpi,
            "count": rows * cols,
            "bytes": sheet_enc.size,
            "quality": sheet_enc.quality,
        }
        payload["size_notes"] += size_notes("相纸", sheet_enc, max_kb)
    payload["output_dir"] = str(OUTPUT_DIR)
    return payload


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Server(ThreadingHTTPServer):
    """Windows 上必须独占端口。

    父类默认 allow_reuse_address=True，会设 SO_REUSEADDR；而 Windows 的语义是
    "允许第二个进程绑同一端口"。两个服务同时监听，请求被随机分给其中一个，
    表现就是"新版本启动了，页面还是旧的"。宁可启动失败，也不要这种幻觉。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def prefs_view(catalog: dict, custom_items: list[dict]) -> tuple[dict, list[str], bool]:
    """当前生效的界面选择 + 每一条回退原因 + 磁盘上是否真有存过。

    默认值取自已解析的预设（`--preset` 已经在这条链上生效），
    所以"没存过"和"存过但失效"都落回同一个地方，不会出现第二套默认。
    """
    raw, note = prefs.load(PREFS_PATH)
    if note:
        log(f"[偏好] {note}")
    presets = PRESETS.get("presets", [])
    preset = next((x for x in presets if x.get("id") == DEFAULT_PRESET_ID), None) or (presets[0] if presets else {})
    sheets = catalog.get("sheets") or []
    defaults = {
        "size": preset.get("size"),
        "background": preset.get("background"),
        "sheet": preset.get("sheet") or (sheets[0]["name"] if sheets else None),
        "mode": preset.get("mode") or ("sheet" if preset.get("sheet") else "single"),
        "format": "jpg",
        "quality": "high",
        "max_kb": 100,
        "max_kb_on": False,
    }
    clean, notes = prefs.normalize(raw, catalog, {x["id"] for x in custom_items}, defaults)
    if note:
        notes.insert(0, note)
    return clean, notes, bool(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "photo/1.0"  # 真实值由 set_brand() 按 config 覆写
    upstream: Upstream | None = None

    def log_message(self, fmt: str, *args) -> None:  # 安静点，只记错误
        pass

    # ---- helpers ----
    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # 本地单机工具：宁可每次回源，也不要让用户看到旧界面
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- routes ----
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path in ("/", "/index.html"):
            self.send_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC_DIR / rel).resolve()
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self.send_error(403, "Forbidden")
                return
            self.send_file(target)
            return
        if path.startswith("/out/"):
            target = (OUTPUT_DIR / path[len("/out/"):]).resolve()
            try:
                target.relative_to(OUTPUT_DIR.resolve())
            except ValueError:
                self.send_error(403, "Forbidden")
                return
            self.send_file(target)
            return
        if path == "/api/status":
            self.send_json(status_snapshot())
            return
        if path == "/api/state":
            snap = status_snapshot()
            if snap["mode"] == "error":
                self.send_json({"ok": False, "mode": "error", "error": snap["error"], "fatal": True}, 500)
                return
            if snap["mode"] == "setup":
                self.send_json({"ok": False, "mode": "setup", "setup": snap["setup"]}, 503)
                return
            if not snap["ready"]:
                self.send_json(
                    {"ok": False, "mode": snap["mode"], "loading": True, "stage": snap["stage"]}, 503
                )
                return
            try:
                cfg = load_config()
                custom, custom_note = custom_sizes.load(CUSTOM_PATH)
                if custom_note:
                    log(f"[自定义规格] {custom_note}")
                clean_prefs, prefs_notes, prefs_saved = prefs_view(self.upstream.catalog(), custom)
                self.send_json({
                    "ok": True,
                    "mode": "ready",
                    "version": cfg.get("version"),
                    "product": BRAND["title"],
                    "product_latin": BRAND["latin"],
                    "tagline": BRAND["tagline"],
                    "brand": dict(BRAND),
                    "catalog": self.upstream.catalog(),
                    "custom_sizes": custom,
                    "presets": PRESETS.get("presets", []),
                    # 已按 启动参数 > defaults.id > 第一条 解析过，前端不需要再猜
                    "default_preset": DEFAULT_PRESET_ID,
                    # 界面选择：服务端是唯一校验点，前端只念结果
                    "prefs": clean_prefs,
                    "prefs_notes": prefs_notes,
                    "prefs_saved": prefs_saved,
                    # 页面分不清预设是启动参数给的还是配置默认的，就没法执行
                    # "启动参数赢且不写回"——这个字段就是为了让它可执行
                    "preset_source": "cli" if os.environ.get("IDPHOTO_PRESET") else "config",
                })
            except Exception as exc:  # noqa: BLE001
                log(traceback.format_exc())
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/health":
            self.send_json({"ok": True})
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/api/setup/install":
            snap = status_snapshot()
            if snap["mode"] != "setup" or not snap["setup"]:
                self.send_json({"ok": False, "error": "当前不在首次设置状态"}, 409)
                return
            if snap["setup"].get("installing"):
                self.send_json({"ok": False, "error": "已经在下载中"}, 409)
                return
            try:
                plan = plan_models()
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, 500)
                return
            if plan.ready:
                threading.Thread(target=load_upstream, args=(plan.resolved,), daemon=True).start()
                self.send_json({"ok": True, "ready": True})
                return
            if not plan.disk_ok:
                self.send_json(
                    {"ok": False, "error": plan.warnings[0] if plan.warnings else "磁盘空间不足"}, 507
                )
                return
            activate_setup(plan)  # 复位进度与上次的错误
            with STATE_LOCK:
                STATE["setup"]["installing"] = True  # 先占位，避免手动安装在启动线程前插进来
            if not start_install(plan):
                with STATE_LOCK:
                    STATE["setup"]["installing"] = False
                self.send_json({"ok": False, "error": "下载已经在进行"}, 409)
                return
            self.send_json({"ok": True}, 202)
            return

        if path == "/api/setup/cancel":
            snap = status_snapshot()
            setup = snap.get("setup") or {}
            if not setup.get("installing"):
                self.send_json({"ok": False, "error": "当前没有在下载"}, 409)
                return
            with STATE_LOCK:
                STATE["setup"]["cancel"] = True
            self.send_json({"ok": True})
            return

        if path == "/api/setup/install-local":
            snap = status_snapshot()
            if snap["mode"] != "setup" or not snap["setup"]:
                self.send_json({"ok": False, "error": "当前不在首次设置状态"}, 409)
                return
            if snap["setup"].get("installing"):
                self.send_json({"ok": False, "error": "下载进行中，请先暂停再手动安装"}, 409)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                plan = plan_models()
                limit = max((asset.size for asset in plan.missing), default=0) + 1024 * 1024
                if length <= 0 or length > limit:
                    raise assets.AssetError(f"文件为空或超过预期大小（{limit // 1024 // 1024} MB）")
                data = self.rfile.read(length)
                model_id = install_local(data)
                self.send_json({"ok": True, "installed": model_id})
            except assets.AssetError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                log(traceback.format_exc())
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return

        if path == "/api/process":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD:
                    raise ValueError("图片为空或超过 40MB")
                body = self.rfile.read(length)
                if not STATE["ready"] or self.upstream is None:
                    snap = status_snapshot()
                    self.send_json(
                        {
                            "ok": False,
                            "mode": snap["mode"],
                            "loading": snap["mode"] != "setup",
                            "error": "模型尚未就绪",
                        },
                        503,
                    )
                    return
                payload = handle_process(self.upstream, body, query)
                self.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                log(f"[处理失败] {exc}")
                log(traceback.format_exc())
                self.send_json({"ok": False, "error": contracts.translate_error(exc)}, 400)
            return

        if path == "/api/custom-sizes":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 64 * 1024:
                    raise ValueError("请求体为空或过大")
                doc = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("请求体必须是 JSON 对象")

                items, note = custom_sizes.load(CUSTOM_PATH)
                if note:
                    log(f"[自定义规格] {note}")
                if len(items) >= custom_sizes.MAX_SIZES:
                    raise ValueError(
                        f"最多保存 {custom_sizes.MAX_SIZES} 条自定义规格，请先删除不用的"
                    )

                spec = custom_sizes.normalize(
                    width=doc.get("w"),
                    height=doc.get("h"),
                    dpi=doc.get("dpi", 300),
                    mm_w=doc.get("mm_w"),
                    mm_h=doc.get("mm_h"),
                    name=doc.get("name", ""),
                )
                items.append(spec)
                custom_sizes.save(CUSTOM_PATH, items)
                log(f"[自定义规格] 已保存「{spec['name']}」{spec['width']}×{spec['height']}@{spec['dpi']}")
                self.send_json({"ok": True, "size": spec, "sizes": items})
            except Exception as exc:  # noqa: BLE001
                log(f"[自定义规格] 保存失败：{exc}")
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/prefs":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 64 * 1024:
                    raise ValueError("请求体为空或过大")
                doc = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                catalog = self.upstream.catalog()
                custom, _ = custom_sizes.load(CUSTOM_PATH)
                clean, _notes, _saved = prefs_view(catalog, custom)
                raw = {k: doc[k] for k in prefs.FIELDS if k in doc}
                saved_prefs, notes = prefs.normalize(raw, catalog, {x["id"] for x in custom}, clean)
                try:
                    prefs.save(PREFS_PATH, saved_prefs)
                except OSError as exc:
                    # 存不下来不影响出片，但必须看得见
                    self.send_json({"ok": False, "error": f"设置存不下来：{exc}",
                                    "prefs": saved_prefs, "prefs_notes": notes}, 500)
                    return
                self.send_json({"ok": True, "prefs": saved_prefs, "prefs_notes": notes})
            except Exception as exc:  # noqa: BLE001
                log(f"[偏好] 保存失败：{exc}")
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/open-folder":
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(OUTPUT_DIR))  # noqa: S606  (Windows 专用，仅打开资源管理器)
                self.send_json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return

        self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        prefix = "/api/custom-sizes/"
        if not path.startswith(prefix):
            self.send_error(404, "Not Found")
            return

        size_id = path[len(prefix):].strip("/")
        if not size_id:
            self.send_json({"ok": False, "error": "缺少规格 id"}, 400)
            return
        try:
            items, _note = custom_sizes.load(CUSTOM_PATH)
            remain = [item for item in items if item.get("id") != size_id]
            if len(remain) == len(items):
                self.send_json({"ok": False, "error": "没有这条自定义规格"}, 404)
                return
            custom_sizes.save(CUSTOM_PATH, remain)
            log(f"[自定义规格] 已删除 {size_id}，剩余 {len(remain)} 条")
            self.send_json({"ok": True, "sizes": remain})
        except Exception as exc:  # noqa: BLE001
            log(f"[自定义规格] 删除失败：{exc}")
            self.send_json({"ok": False, "error": str(exc)}, 400)


def main() -> int:
    cfg = load_config()
    set_brand(cfg)  # 端口还没绑之前就要有品牌串：就绪日志与 Server 头都读它
    webui = cfg.get("webui", {})
    host = webui.get("host", "127.0.0.1")
    port = int(os.environ.get("IDPHOTO_PORT") or webui.get("port_start", 7860))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    model_home().mkdir(parents=True, exist_ok=True)

    httpd = Server((host, port), Handler)
    log(f"服务已在 http://{host}:{port} 监听，正在加载模型……")

    def bootstrap() -> None:
        try:
            STATE["stage"] = "正在读取预设"
            prepare_contracts()

            plan = plan_models()
            if not plan.ready:
                names = "、".join(asset.title for asset in plan.missing)
                log(f"缺少 {len(plan.missing)} 个模型（{names}），等待在页面完成首次设置")
                log(f"需要下载约 {plan.total_bytes / 1024 / 1024:.0f} MB，目标目录：{plan.home}")
                activate_setup(plan)
                return

            for model_id, path in plan.resolved.items():
                log(f"  模型 {model_id}：{path}")
            load_upstream(plan.resolved)
        except Exception as exc:  # noqa: BLE001
            with STATE_LOCK:
                STATE.update({"ready": False, "mode": "error", "stage": "加载失败", "error": str(exc)})
            log(f"[启动失败] {exc}")
            log(traceback.format_exc())

    threading.Thread(target=bootstrap, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("正在停止……")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

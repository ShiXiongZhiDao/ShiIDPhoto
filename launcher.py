"""师兄证照（ShiIDPhoto）便携版启动器。

职责边界（保持单一）：自检 → 找端口 → 拉起上游 WebUI → 等服务就绪 → 开浏览器 → 守着子进程。
不处理任何图像逻辑，不修改上游代码。

用法：
    双击 `双击启动师兄证照.bat`（英文名 `Launch-ShiIDPhoto.bat`，两份内容逐字相同）
    python launcher.py --check-only   （只自检，不启动；这条路径保留全部提示，供验收与排查）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shell"))  # 语言层与包内用的是同一份词条表，别在这里抄第二份

import i18n  # noqa: E402  (依赖上面两行 sys.path；它只用标准库)

from portable_checks import (  # noqa: E402  (必须在 sys.path 调整之后导入)
    HEALTH_UA,
    CheckError,
    format_issues,
    has_error,
    load_json,
    validate_models,
)

CONFIG_PATH = ROOT / "config" / "portable.json"
LOG_LIMIT_BYTES = 8 * 1024 * 1024
LOG_KEEP = 3

# 品牌串。唯一来源是 `config/portable.json`，`main()` 读到配置后填这里，
# 下面所有窗口标题与横幅都从这里取——以前 13 处各写一次字面量，换品牌时要逐个找，
# 找漏的那处只表现为"名字没换干净"，没人会当 bug 报，正是既有约定说的那类沉默。
# 兜底值用无品牌的中性词：配置读不出来时宁可标题朴素，也不要印出一个作废的旧名字。
BRAND: dict = {"title": "证件照工具", "latin": "photo", "tagline": ""}

#: 界面语言：启动器只 stdlib，`shell/` 不在它的 sys.path 上，
#: 所以下面显式加一次——`shell/i18n.py` 本身也只用标准库，不破这条约束。
PREFS_PATH = ROOT / "user-data" / "prefs.json"


def init_language(config: dict) -> None:
    """定这次控制台的语种：prefs 显式值 > config 的 webui.lang > 系统 UI language。

    启动失败时界面根本不会出现，所以这句话必须在这条路径上自己成立——
    它不能等 `/api/state`，只能自己读文件（既有约定的那条独立路径）。
    """
    intent = None
    try:
        doc = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            intent = doc.get("language")
    except (OSError, ValueError):
        intent = None
    cfg_lang = (config.get("webui", {}) or {}).get("lang") if isinstance(config, dict) else None
    _intent, effective = i18n.resolve(intent, cfg_lang)
    i18n.set_lang(effective)


def brand_name() -> str:
    """英文态用已签的拉丁名，不硬塞汉字（既有约定的分层 + 0022 决策 16）。"""
    return BRAND["latin"] if i18n.LANG == "en" else BRAND["title"]


def set_brand(config: dict) -> None:
    """把 config 里的品牌字段搬进 BRAND；缺哪个就退到中性值，不崩。"""
    BRAND["latin"] = str(config.get("product_name") or "").strip() or "photo"
    BRAND["title"] = str(config.get("product_title") or "").strip() or BRAND["latin"]
    BRAND["tagline"] = str(config.get("product_tagline") or "").strip()


def brand_title(stage: str) -> str:
    """控制台标题统一成「品牌 · 阶段」。"""
    return f"{brand_name()} · {i18n.out(stage)}"


def brand_banner() -> str:
    """启动横幅与窗口初始标题那行字。英文态 tagline 留空（决策 16）。"""
    if i18n.LANG == "en":
        return BRAND["latin"]
    return " ".join(part for part in (BRAND["title"], BRAND["tagline"]) if part)


def say(message: str = "") -> None:
    print(i18n.out(message), flush=True)


_TTY = sys.stdout.isatty()
_progress_last = 0.0


def progress(text: str, elapsed: float, interval: float = 3.0) -> None:
    """进度只在真终端里回写同一行。

    重定向到文件时（验收脚本、`> log`）`\\r` 不会换行，几十次刷新会挤成一行，
    把日志里真正有用的上下文冲掉。这种情况下改成每 interval 秒记一行。
    """
    global _progress_last
    text = i18n.out(text)      # 传进来的就是那几个中文阶段名，词条里有它们
    if _TTY:
        sys.stdout.write("\r" + f"  {text}… {elapsed:4.1f}s")
        sys.stdout.flush()
        return
    now = time.monotonic()
    if now - _progress_last >= interval:
        _progress_last = now
        say(f"  {text}… {elapsed:.1f}s")


def clear_progress() -> None:
    """把光标那一行擦掉。非 TTY 下没有"那一行"，什么也不做。"""
    if not _TTY:
        return
    sys.stdout.write("\r" + " " * 72 + "\r")
    sys.stdout.flush()



def fail(message: str, hint: str | None = None) -> int:
    say()
    say(f"[启动失败] {message}")
    if hint:
        say(hint)
    return 1


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise CheckError(f"缺少配置文件：{CONFIG_PATH.relative_to(ROOT)}")
    return load_json(CONFIG_PATH)


def resolve_python() -> Path:
    """优先用包内解释器；IDPHOTO_PYTHON 仅供开发期自测。"""
    override = os.environ.get("IDPHOTO_PYTHON")
    if override:
        return Path(override)
    return ROOT / "python-embed" / "python.exe"


def ensure_user_data(config: dict) -> Path:
    user_data = ROOT / config["paths"]["user_data"]
    for key in ("input", "output", "logs", "config", "models"):
        (user_data / key).mkdir(parents=True, exist_ok=True)
    probe = user_data / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise CheckError("当前目录不可写，无法创建用户数据目录。请将程序移动到有写入权限的位置后重试。") from exc
    return user_data


def port_available(host: str, port: int) -> bool:
    """端口是否真的能用。

    不能只看 bind 成不成功：Windows 的 SO_REUSEADDR 允许绑到别人已经监听的端口上，
    bind 通过、但流量仍然被原进程接走。启动器会以为挑到了空端口，用户看到的是
    "新版本启动了，页面还是旧的"——比启动失败更难查。

    所以先 connect 一次：连得上就是有人在听，直接排除。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        try:
            probe.connect((host, port))
            return False
        except OSError:
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            # Windows 要的是"独占"，不是"复用"
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def choose_port(host: str, start: int, end: int) -> int:
    for port in range(start, end + 1):
        if port_available(host, port):
            return port
    raise CheckError(f"端口 {start}-{end} 全部被占用，请关闭占用程序后重试。")


def rotate_log(log_path: Path) -> None:
    """日志超过上限就滚动，避免用户长期使用后日志无限增长。"""
    if not log_path.is_file() or log_path.stat().st_size < LOG_LIMIT_BYTES:
        return
    for index in range(LOG_KEEP - 1, 0, -1):
        older = log_path.with_suffix(f".log.{index}")
        newer = log_path.with_suffix(f".log.{index - 1}")
        if newer.is_file():
            older.unlink(missing_ok=True)
            newer.rename(older)
    rotated = log_path.with_suffix(".log.1")
    log_path.replace(rotated)


SESSION_MARK = "===== 启动 "
LOG_NOTE_PREFIX = "[自检] "


def start_log_session(log_path: Path, notes: list[str] | None = None) -> None:
    """每次启动写一条分隔线，失败时只展示本次记录，不把上次的成功日志混进来。

    自检备注（可选模型没装、pngquant 缺席这类）只进日志不进控制台：
    用户看见一串「未安装 / 不可用」会以为程序坏了，排查时又确实需要这几行。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n{SESSION_MARK}{stamp} =====\n")
        for note in notes or []:
            stream.write(f"{LOG_NOTE_PREFIX}{note}\n")


def tail(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    last = None
    for index, row in enumerate(content):
        if row.startswith(SESSION_MARK):
            last = index
    if last is not None:
        content = content[last + 1:]
    return "\n".join(content[-lines:])


def resolve_entry(config: dict, classic: bool) -> Path:
    """默认走自研前端；--classic 时回退到上游 Gradio 界面。"""
    section = "upstream" if classic else "shell"
    return ROOT / config[section]["entry"]


def build_command(python: Path, entry: Path, webui: dict, port: int, classic: bool) -> list[str]:
    if classic:
        return [
            str(python),
            str(entry),
            f"--lang={webui.get('lang', 'zh')}",
            "--server_name",
            webui["host"],
            "--server_port",
            str(port),
        ]
    return [str(python), str(entry)]


def set_console_title(text: str) -> None:
    """控制台标题栏也显示状态，双击启动时用户最小化也能看到还在跑。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def interpret_status(payload: dict) -> tuple[str, str]:
    """把 /api/status 的返回翻译成 (状态, 说明)。

    四种情况必须分开：等待首次设置、还在加载、已经就绪、加载失败。
    把"失败"混进"超时"会让用户白等两分钟；把"待安装"当成失败，
    则会在向导刚出现时就把服务杀掉。
    """
    if not isinstance(payload, dict):
        return "loading", "正在启动"
    if payload.get("mode") == "error" or payload.get("error"):
        return "failed", str(payload.get("error") or "启动失败")
    if payload.get("mode") == "setup":
        return "setup", "等待首次设置"
    if payload.get("ready") or payload.get("mode") == "ready":
        return "ready", ""
    return "loading", str(payload.get("stage") or "正在启动")


def probe_status(url: str) -> dict | None:
    try:
        request = urllib.request.Request(
            url + "/api/status", headers={"User-Agent": HEALTH_UA}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def wait_for_port(url: str, process: subprocess.Popen, timeout: int, classic: bool = False) -> bool:
    """只等端口能响应，不等模型加载完。

    这样浏览器可以立刻打开，由页面自己显示加载进度。
    """
    probe = url if classic else url + "/api/health"
    deadline = time.monotonic() + timeout
    started = time.monotonic()

    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            request = urllib.request.Request(probe, headers={"User-Agent": HEALTH_UA})
            with urllib.request.urlopen(request, timeout=1) as response:
                if 200 <= response.status < 500:
                    clear_progress()
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

        progress("[启动中] 等待本地服务响应", time.monotonic() - started)
        time.sleep(0.3)

    clear_progress()
    return False


def monitor_service(url: str, process: subprocess.Popen, timeout: int) -> tuple[str, str]:
    """盯着模型加载，返回 (ready|failed|timeout, 说明)。

    失败要单独识别：把"加载失败"混进"超时"，用户会白等两分钟才看到真相。
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    hinted = False
    setup_hinted = False

    while time.monotonic() < deadline:
        if process.poll() is not None:
            return "failed", "服务进程意外退出。"

        payload = probe_status(url)
        if payload is None:
            time.sleep(0.4)
            continue
        state, detail = interpret_status(payload)
        elapsed = time.monotonic() - started

        if state == "ready":
            clear_progress()
            return "ready", ""
        if state == "failed":
            clear_progress()
            return "failed", detail

        if state == "setup":
            # 首次安装可能要几分钟，不能套用 180 秒的启动超时
            clear_progress()
            if not setup_hinted:
                setup_hinted = True
                set_console_title(brand_title("等待首次设置"))
                say("  首次使用：请在浏览器里完成 AI 模型下载，下载期间窗口不要关。")
                say("  装好后页面会自动进入主界面，不需要重开。")
            deadline = max(deadline, time.monotonic() + 3600)
            time.sleep(0.6)
            continue

        progress(f"[加载中] {detail}", elapsed)

        if not hinted and elapsed > 8:
            hinted = True
            clear_progress()
            say("  提示：首次启动要加载三个 AI 模型并校验，大约 20 秒，窗口不要关。")

        time.sleep(0.4)

    clear_progress()
    return "timeout", ""


def summarize_log(path: Path, lines: int = 5) -> str:
    """从日志里挑出给人看的那几行。

    Python 的 traceback 对普通用户没有意义，全打出来只会把真正的原因淹掉；
    完整堆栈仍然留在日志文件里，供排查用。
    """
    raw = tail(path, 60)
    if not raw:
        return ""

    keep = []
    for row in raw.splitlines():
        stripped = row.strip()
        if not stripped:
            continue
        # 自检备注是"某个可选功能没装"，不是这次失败的原因；
        # 混进摘要会把真正的报错挤出那 5 行。完整日志里仍然留着。
        if stripped.startswith(LOG_NOTE_PREFIX):
            continue
        if stripped.startswith("Traceback (most recent call last)"):
            continue
        if stripped.startswith('File "'):
            continue
        if set(stripped) <= {"^", "~"}:
            continue
        keep.append(stripped)

    return "\n".join(keep[-lines:])


def report_failure(process: subprocess.Popen, log_path: Path, message: str) -> None:
    """把失败说清楚，并保证窗口不会一闪而过。"""
    code = process.poll()
    say()
    say("  " + "-" * 56)
    say(f"  [启动失败] {message}")
    if code is not None:
        say(f"  服务进程已退出，退出码 {code}。")

    snippet = summarize_log(log_path)
    if snippet:
        say()
        say("  日志摘要：")
        for row in snippet.splitlines():
            say("    " + row)

    say()
    say(f"  完整日志：{log_path}")
    say("  常见原因：")
    say("    · AI 模型文件不完整或被安全软件隔离")
    say("    · 缺少 Microsoft Visual C++ 运行库")
    say("    · 端口被其他程序或防火墙拦截")
    say("  关闭本窗口即可退出。")
    say()

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def run_self_check(
    config: dict, python: Path, classic: bool = False, verbose: bool = False
) -> tuple[int, list[str]]:
    """返回 (退出码, 自检备注)。任何 error 都直接中止，不做降级。

    备注是"某个可选能力没装"这类信息，正常启动不往控制台印：用户看见
    「可选模型未安装 / pngquant 找不到」只会以为程序坏了，而这些能力在自研
    界面里根本没有入口（既有约定的"出声"针对的是失败，不是可选功能缺席）。
    备注仍然逐条写进本次启动的日志，`--check-only` 时照常打印，
    验收脚本与人工排查都不丢信息。真正的失败照旧出声，不受这里影响。
    """
    notes: list[str] = []

    def tell(message: str) -> None:
        notes.append(message)
        if verbose:
            say(f"提示：{message}")

    if verbose:
        say("正在自检……")

    entry = resolve_entry(config, classic)
    if not entry.is_file():
        return fail(
            f"程序文件不完整，缺少 {entry.relative_to(ROOT)}。",
            "请重新解压完整的压缩包，不要只复制 bat 文件。",
        ), notes
    if not classic:
        index_html = ROOT / config["shell"]["static_dir"] / "index.html"
        if not index_html.is_file():
            return fail(
                f"界面文件不完整，缺少 {index_html.relative_to(ROOT)}。",
                "请重新解压完整的压缩包。",
            ), notes
    if not python.is_file():
        return fail(
            f"未找到内置 Python：{python.relative_to(ROOT) if python.is_relative_to(ROOT) else python}",
            "请重新解压完整的压缩包。",
        ), notes

    tier = config.get("distribution", {}).get("tier", "personal")
    bundled = bool((config.get("models") or {}).get("bundled", True))
    manifest_path = ROOT / config["paths"]["model_manifest"]
    try:
        issues = validate_models(ROOT, manifest_path, tier, build_mode=False, require_files=bundled)
    except CheckError as exc:
        return fail(str(exc)), notes

    if has_error(issues):
        say()
        say("[启动失败] AI 模型文件不完整或已损坏：")
        say(format_issues(i for i in issues if i.severity == "error"))
        say()
        say("请重新下载完整安装包。程序不会在运行时自动联网下载 AI 模型。")
        return 1, notes

    if not bundled:
        tell("AI 模型未随包提供，首次启动会在浏览器里引导下载（约 180 MB）。")

    for issue in issues:
        if issue.severity == "warning":
            tell(issue.message)

    pngquant = ROOT / "src" / "tool" / "ext" / "pngquant.exe"
    if not pngquant.is_file():
        tell("未找到 pngquant，图片压缩功能将不可用（其余功能不受影响）。")

    if verbose:
        say("自检通过。")
    return 0, notes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="师兄证照（ShiIDPhoto）便携版启动器")
    parser.add_argument("--check-only", action="store_true", help="只自检，不启动服务")
    parser.add_argument("--preset", default=None, help="指定预设 id，覆盖 config/presets.json 的默认值")
    parser.add_argument("--classic", action="store_true", help="使用上游 Gradio 界面（备用入口）")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        say(f"提示：忽略未知参数 {unknown}")

    try:
        config = load_config()
    except CheckError as exc:
        return fail(str(exc))
    set_brand(config)  # 品牌串从这里开始才可信；标题、横幅都在它之后才打印
    init_language(config)  # 语种要在这之前定下来：下面第一句 say() 就得用对语言

    python = resolve_python()
    code, notes = run_self_check(config, python, args.classic, verbose=args.check_only)
    if code != 0:
        return code

    if args.check_only:
        return 0

    if args.preset:
        try:
            presets = load_json(ROOT / config["paths"]["presets"])
            known = {item.get("id") for item in presets.get("presets", [])}
        except (CheckError, KeyError):
            known = set()
        if known and args.preset not in known:
            say(f"提示：预设「{args.preset}」不存在，将使用配置文件里的默认预设。")
            args.preset = None

    try:
        user_data = ensure_user_data(config)
        webui = config["webui"]
        port = choose_port(webui["host"], webui["port_start"], webui["port_end"])
    except CheckError as exc:
        return fail(str(exc))

    url = f"http://{webui['host']}:{port}"
    if port != webui["port_start"]:
        say(f"默认端口 {webui['port_start']} 已被占用，改用端口 {port}。")

    log_dir = user_data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "webui.log"
    rotate_log(log_path)

    env = os.environ.copy()
    env.update(
        {
            "IDPHOTO_PORTABLE_ROOT": str(ROOT),
            "IDPHOTO_USER_DATA": str(user_data),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "GRADIO_ANALYTICS_ENABLED": "False",
        }
    )
    if args.preset:
        env["IDPHOTO_PRESET"] = args.preset

    entry = resolve_entry(config, args.classic)
    command = build_command(python, entry, webui, port, args.classic)
    if not args.classic:
        env["IDPHOTO_PORT"] = str(port)
        env["IDPHOTO_ROOT"] = str(ROOT)

    line = "=" * 60
    set_console_title(brand_title("正在启动"))
    say()
    say(line)
    say(f"  {brand_banner()}")
    say(line)
    say(f"  地址：{url}")
    say(f"  日志：{log_path.relative_to(ROOT)}")
    if bool((config.get("models") or {}).get("bundled", True)):
        say("  首次启动要加载三个 AI 模型并校验，约 20 秒。窗口不要关。")
    else:
        say("  首次启动需要下载 AI 模型（约 180 MB），请在浏览器里完成，窗口不要关。")
    say(line)
    say()

    start_log_session(log_path, notes)
    with log_path.open("a", encoding="utf-8", errors="replace") as stream:
        process = subprocess.Popen(
            command,
            cwd=str(entry.parent if args.classic else ROOT),
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )

        set_console_title(brand_title("正在启动服务…"))
        if not wait_for_port(url, process, int(webui.get("startup_timeout_seconds", 180)), args.classic):
            set_console_title(brand_title("启动失败"))
            report_failure(process, log_path, "本地服务没有在预期时间内打开端口。")
            return 1

        # 端口通了就开浏览器，模型加载留给页面显示
        say()
        say(line)
        say(f"  本地服务已响应：{url}")
        # 自动化验收时关掉自动开浏览器（IDPHOTO_NO_BROWSER=1），否则每次跑 QA 都多一个窗口。
        # 提示语也跟着改，避免日志里写着"正在打开浏览器"但实际没开。
        auto_open = bool(webui.get("open_browser", True)) and not os.environ.get("IDPHOTO_NO_BROWSER")
        say("  正在打开浏览器…" if auto_open else "  已跳过自动打开浏览器（IDPHOTO_NO_BROWSER）")
        say("  页面若显示「正在加载 AI 模型…」，属于正常，等它转完即可。")
        if auto_open:
            say("  如果没有自动打开，请把上面的地址复制到浏览器。")
        say("  照片只在本机处理。关闭本窗口即停止服务。")
        say(line)
        say()

        if auto_open:
            opened = False
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if not opened:
                say("  [提示] 自动打开浏览器失败，请手动访问上面的地址。")
        say()

        # 继续盯着模型加载：失败要立刻报出来，而不是让用户对着死窗口猜
        if not args.classic:
            set_console_title(brand_title("正在加载 AI 模型…"))
            state, detail = monitor_service(url, process, int(webui.get("startup_timeout_seconds", 180)))
            if state == "failed":
                set_console_title(brand_title("启动失败"))
                report_failure(process, log_path, detail or "AI 模型加载失败。")
                return 1
            if state == "timeout":
                set_console_title(brand_title("启动失败"))
                report_failure(process, log_path, "AI 模型加载超时。")
                return 1

        set_console_title(brand_title("运行中"))
        say("  AI 模型已加载完成，可以开始用了。")
        say()

        try:
            return process.wait()
        except KeyboardInterrupt:
            say("正在停止……")
            process.terminate()
            try:
                return process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

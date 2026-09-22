"""视频模型测试广场 — FastAPI 后端。

启动:  python -m uvicorn main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import providers

DATA_HOME_NAME = "VideoStudio"


def _app_dir() -> Path:
    """可写数据目录（视频、上传、.env 都在这里）。

    - 源码运行：脚本所在目录
    - Windows 打包：exe 同级目录
    - macOS .app：不能写进应用包（替换 .app 会丢数据），落到
      ~/Library/Application Support/VideoStudio
    - 其他打包形态：可执行文件同级目录
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin" and ".app/Contents/MacOS" in exe.as_posix():
        base = Path.home() / "Library" / "Application Support" / DATA_HOME_NAME
        base.mkdir(parents=True, exist_ok=True)
        return base
    return exe.parent


def _res_dir() -> Path:
    """只读资源目录：打包后为 PyInstaller 的临时解压目录（_MEIPASS）。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


load_dotenv(_app_dir() / ".env")  # 打包后也从 exe 同级目录读取 .env

BASE_DIR = _app_dir()
STATIC_DIR = _res_dir() / "static"
VIDEOS_DIR = BASE_DIR / "videos"
UPLOAD_DIR = BASE_DIR / "uploads"
INDEX_FILE = VIDEOS_DIR / "index.json"
VIDEOS_DIR.mkdir(exist_ok=True)

MAX_FILES = 9
MAX_FILES_REF2VA = 12  # 全参考模式附件总数上限
# 单个素材大小上限（MB），按类型区分；总个数内不限制累计大小
MAX_FILE_MB = {"image": 30, "video": 50, "audio": 15}
KIND_LABEL = {"image": "图片", "video": "视频", "audio": "音频"}
MAX_BODY_MB = 64  # API 请求体上限（MB）

# 按模式的素材规格：fl2va 仅图片 0~2 张；ref2va 图片+视频+音频（见 /api/generate 校验）
DIM_MIN, DIM_MAX = 256, 5760          # 图片/视频宽高像素范围
RATIO_MIN, RATIO_MAX = 2 / 5, 5 / 2   # 宽高比范围 5:2 ~ 2:5
DUR_MIN, DUR_MAX = 2.0, 15.0          # 音视频单段时长（秒）
DUR_TOTAL_MAX = 15.0                  # 视频/音频总时长（秒）
DUR_EPS = 0.01                        # 时长比较容差（探测值存在舍入）

INDEX: list[dict] = []
TASKS: dict[str, dict] = {}
TASK_TTL = 15 * 60  # 完成任务保留 15 分钟后清理

# 后台生成/拼接任务集合：用于优雅关闭时显式取消，避免 Ctrl+C 卡在等待长任务
BG_TASKS: set[asyncio.Task] = set()


def load_index() -> None:
    global INDEX
    if INDEX_FILE.exists():
        try:
            data = json.loads(INDEX_FILE.read_text("utf-8"))
            INDEX = [
                m for m in data.get("videos", [])
                if isinstance(m, dict) and (VIDEOS_DIR / m.get("file", "")).exists()
            ]
        except Exception:
            INDEX = []


def save_index() -> None:
    INDEX_FILE.write_text(
        json.dumps({"videos": INDEX}, ensure_ascii=False, indent=2), "utf-8"
    )


def register_video(
    path: Path,
    prompt: str,
    model_id: str,
    model_name: str,
    provider: str,
    kind: str = "gen",
    parent_id: str | None = None,
    inputs: list | None = None,
) -> dict:
    vid = uuid.uuid4().hex[:8]
    target = VIDEOS_DIR / f"{vid}.mp4"
    shutil.move(str(path), target)
    duration, w, h = providers.probe(target)
    meta = {
        "id": vid,
        "file": target.name,
        "prompt": prompt,
        "model_id": model_id,
        "model_name": model_name,
        "provider": provider,
        "kind": kind,
        "parent_id": parent_id,
        "inputs": inputs or [],
        "duration": round(duration, 2),
        "width": w,
        "height": h,
        "size": target.stat().st_size,
        "created_at": int(time.time()),
    }
    INDEX.insert(0, meta)
    save_index()
    return meta


def get_meta(vid: str) -> dict:
    for m in INDEX:
        if m["id"] == vid:
            return m
    raise HTTPException(404, "视频不存在")


def purge_stale_tasks() -> None:
    now = time.time()
    for tid in [t for t, v in TASKS.items() if now - t_info(v) > TASK_TTL]:
        TASKS.pop(tid, None)


def t_info(task: dict) -> float:
    return float(task.get("_ts", 0))


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_index()
    # 服务重启后内存任务已丢失，清掉遗留的上传临时目录
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    # 抑制 Windows 上客户端断开连接导致的噪声异常：
    # 浏览器拖动视频进度条 seek / 关闭标签页 / 刷新页面时会强制断开正在
    # 流式传输视频文件的 socket，Proactor 事件循环在 connection_lost 回调里
    # 对已经断开的 socket 调 shutdown() 抛 ConnectionResetError [WinError 10054]。
    # 这是客户端行为、不影响服务运行，直接吞掉避免刷屏（Ctrl+C 关闭也更清爽）。
    loop = asyncio.get_running_loop()
    prev = loop.get_exception_handler() or loop.default_exception_handler

    def _suppress_client_reset(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")
        if (isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
                or "10054" in msg or "ConnectionResetError" in msg):
            return
        prev(loop, context)

    loop.set_exception_handler(_suppress_client_reset)

    # Ctrl+C 兜底：无论用 `python main.py` 还是 `python -m uvicorn` 启动都会生效
    # （lifespan 是 app 的一部分，uvicorn 一启动就执行这段）。按下 Ctrl+C 立即退出，
    # 彻底避免 uvicorn 等待浏览器视频流连接而卡在 Shutting down。临时上传目录会在
    # 下次启动的 lifespan startup 里被清空，不会残留垃圾。
    import signal as _signal

    def _force_quit(signum, frame):
        print("\n正在关闭服务…", flush=True)
        os._exit(0)

    try:
        _signal.signal(_signal.SIGINT, _force_quit)
    except (ValueError, OSError):
        pass  # Windows 服务 / 非主线程等环境可能无法注册信号，忽略即可

    yield
    # 优雅关闭阶段：取消所有后台生成/拼接任务，确保进程能快速退出
    # （否则 uvicorn 会一直等待这些长任务结束，Ctrl+C 后像卡死在 Shutting down）
    for t in list(BG_TASKS):
        t.cancel()
    if BG_TASKS:
        try:
            await asyncio.wait_for(
                asyncio.gather(*BG_TASKS, return_exceptions=True), timeout=3.0
            )
        except asyncio.TimeoutError:
            pass


class _BodyTooLarge(Exception):
    """请求体超限信号，由 BodySizeLimitMiddleware 统一转为 413。"""


class BodySizeLimitMiddleware:
    """API 请求体大小上限：带 Content-Length 的预检直接拒绝；chunked 传输边读边计数。"""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._reject(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        received = 0

        async def receive_limited():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        response_started = False

        async def send_guarded(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_limited, send_guarded)
        except _BodyTooLarge:
            if response_started:  # 响应已开始发送则无法改发 413
                raise
            await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send) -> None:
        response = JSONResponse(
            {"detail": f"请求体过大（上限 {self.max_bytes // 1024 // 1024}MB）"},
            status_code=413,
        )
        await response(scope, receive, send)


app = FastAPI(title="视频模型测试广场", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_MB * 1024 * 1024)


# ---------------- 生成设置 ----------------

@app.get("/api/models")
def list_models():
    try:
        provider = providers.get_provider()
        provider_name = provider.name
    except RuntimeError:
        provider_name = "demo"
    return {
        "provider": provider_name,
        "formats": [dict(f) for f in providers.FORMAT_PRESETS],
        "defaults": dict(providers.GEN_DEFAULTS),
    }


class KeyCheckReq(BaseModel):
    api_url: str = ""
    api_key: str = ""


@app.post("/api/check_key")
async def check_key(req: KeyCheckReq):
    """检测 API Key 是否被模型接口接受。

    携带 Authorization: Bearer 头向接口发送空载荷 POST：鉴权层先于参数
    校验执行，401/403 判定 Key 无效；其余状态码（400/404/405/422 等校验
    类错误）说明请求已通过鉴权，判定有效。空载荷不会触发真实生成。
    """
    api_url = req.api_url.strip()
    api_key = req.api_key.strip()
    if not api_url:
        raise HTTPException(400, "请先填写模型 API 接口地址")
    if not api_url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "API 接口地址需以 http:// 或 https:// 开头")
    if not api_key:
        raise HTTPException(400, "请输入 API Key")

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True
        ) as client:
            r = await client.post(
                api_url, json={}, headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as e:
        return {"ok": False, "status": None, "message": f"无法连接模型接口: {e}"}

    if r.status_code in (401, 403):
        return {
            "ok": False,
            "status": r.status_code,
            "message": f"Key 无效或未授权（HTTP {r.status_code}）",
        }
    return {
        "ok": True,
        "status": r.status_code,
        "message": f"检测通过：接口已接受该 Key（HTTP {r.status_code}）",
    }


# ---------------- 生成任务 ----------------

def _file_kind(f: UploadFile) -> str:
    mime = (f.content_type or "").lower()
    if mime.startswith(("image/", "video/", "audio/")):
        return mime.split("/", 1)[0]
    ext = Path(f.filename or "").suffix.lower()
    for kind, exts in {
        "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
        "video": {".mp4", ".mov", ".webm", ".avi", ".mkv"},
        "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    }.items():
        if ext in exts:
            return kind
    return ""


def _check_dims(entry: dict, w: int, h: int, *, ratio: bool = False) -> None:
    label = KIND_LABEL[entry["kind"]]
    if not w or not h:
        raise HTTPException(400, f"无法读取{label}尺寸: {entry['name']}")
    if not (DIM_MIN <= w <= DIM_MAX and DIM_MIN <= h <= DIM_MAX):
        raise HTTPException(
            400, f"{label}宽高需在 {DIM_MIN}~{DIM_MAX}px 之间: {entry['name']}（{w}×{h}）")
    if ratio and not (RATIO_MIN <= w / h <= RATIO_MAX):
        raise HTTPException(
            400, f"{label}宽高比需在 5:2~2:5 之间: {entry['name']}（{w}×{h}）")


def _check_duration(entry: dict, d: float) -> None:
    label = KIND_LABEL[entry["kind"]]
    if not (DUR_MIN - DUR_EPS <= d <= DUR_MAX + DUR_EPS):
        raise HTTPException(
            400, f"{label}单段时长需在 {DUR_MIN:g}~{DUR_MAX:g} 秒: {entry['name']}（{d:.1f} 秒）")


@app.post("/api/generate")
async def generate(
    prompt: str = Form(""),
    format: str = Form("t2va"),
    api_url: str = Form(""),
    api_key: str = Form(""),
    aspect: str = Form("16:9"),
    resolution: str = Form("720p"),
    duration: float = Form(8),
    files: list[UploadFile] | None = File(None),
):
    prompt = prompt.strip()
    files = files or []
    if not prompt and not files:
        raise HTTPException(400, "请输入提示词或添加附件")
    if len(prompt) > 500:
        raise HTTPException(400, "提示词过长（最多 500 字）")

    fmt_ids = [f["id"] for f in providers.FORMAT_PRESETS]
    if format not in fmt_ids:
        raise HTTPException(400, f"未知模型格式（可选：{'、'.join(fmt_ids)}）")

    d = providers.GEN_DEFAULTS
    if aspect not in d["ratios"] and aspect != "auto":
        raise HTTPException(400, f"长宽比需为 {'、'.join(d['ratios'])} 或 auto")
    if aspect == "auto" and format != "ref2va":
        aspect = "16:9"  # 自动模式仅全参考有效，其余回退
    if resolution not in providers.RES_MODES:
        raise HTTPException(400, f"分辨率需为 {'、'.join(providers.RES_MODES)}")
    duration = max(d["duration_min"], min(d["duration_max"], float(duration)))

    api_url = api_url.strip()
    if api_url:
        if not api_url.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "API 接口地址需以 http:// 或 https:// 开头")
        provider = providers.CustomAPIProvider(api_url, api_key.strip())
        model_id = model_name = format
    else:
        try:
            provider = providers.get_provider()
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        if provider.name == "zhipu" and not prompt:
            raise HTTPException(400, "该提供方需要文字提示词")
        if provider.name == "zhipu":
            m = provider.list_models()[0]
            model_id, model_name = m["id"], m["name"]
        else:
            model_id = model_name = format

    max_files = MAX_FILES_REF2VA if format == "ref2va" else MAX_FILES
    if len(files) > max_files:
        raise HTTPException(400, f"附件最多 {max_files} 个")

    # 按模式预检附件类型与数量；尺寸/时长需落盘探测后再校验
    if format == "fl2va":
        for f in files:
            if _file_kind(f) != "image":
                raise HTTPException(
                    400, f"首尾帧模式仅支持图片附件: {f.filename or '未命名文件'}")
        if len(files) > 2:
            raise HTTPException(400, "首尾帧模式图片最多 2 张（首帧 + 尾帧）")
    elif format == "ref2va":
        counts = {"image": 0, "video": 0, "audio": 0}
        for f in files:
            k = _file_kind(f)
            if k in counts:
                counts[k] += 1
        if counts["image"] > 9:
            raise HTTPException(400, "全参考模式图片最多 9 张")
        if counts["video"] > 3:
            raise HTTPException(400, "全参考模式视频最多 3 段")
        if counts["audio"] > 3:
            raise HTTPException(400, "全参考模式音频最多 3 段")
        if counts["audio"] and not (counts["image"] or counts["video"]):
            raise HTTPException(400, "音频需搭配图片或视频输入，不能单独使用")

    purge_stale_tasks()
    task_id = uuid.uuid4().hex[:12]
    task_dir = UPLOAD_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # 保存附件并按类型归组（图片列表 / 视频底 / 音频轨）
    all_videos: list[dict] = []  # 全部视频/音频（规格校验与全量转发用；演示渲染仅取首个）
    all_audios: list[dict] = []
    media: dict = {"images": [], "video": None, "audio": None,
                   "videos": all_videos, "audios": all_audios}
    inputs_meta: list[dict] = []
    try:
        for f in files:
            kind = _file_kind(f)
            if not kind:
                raise HTTPException(400, f"不支持的附件类型: {f.filename or '未命名文件'}")
            limit = MAX_FILE_MB[kind] * 1024 * 1024
            if (f.size or 0) > limit:  # 先按声明大小快速拒绝
                raise HTTPException(
                    400, f"附件过大（{KIND_LABEL[kind]}单个上限 {MAX_FILE_MB[kind]}MB）: {f.filename}")
            safe = re.sub(r'[\\/:*?"<>|]+', "-", f.filename or "file")[:60] or "file"
            dest = task_dir / f"{uuid.uuid4().hex[:6]}_{safe}"
            with open(dest, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            size = dest.stat().st_size  # 声明大小缺失/不按时以实际落盘大小兜底
            if size > limit:
                raise HTTPException(
                    400, f"附件过大（{KIND_LABEL[kind]}单个上限 {MAX_FILE_MB[kind]}MB）: {f.filename}")
            entry = {"path": dest, "name": f.filename or safe, "kind": kind,
                     "mime": f.content_type or ""}
            if kind == "image":
                media["images"].append(entry)
            elif kind == "video":
                all_videos.append(entry)
                if not media["video"]:
                    media["video"] = entry
            elif kind == "audio":
                all_audios.append(entry)
                if not media["audio"]:
                    media["audio"] = entry
            inputs_meta.append({"name": entry["name"], "kind": kind, "size": size})

        # ---- 按模式校验尺寸/时长（需落盘后用 ffprobe 探测）----
        if format == "fl2va":
            for entry in media["images"]:
                _, w, h, _ = providers.probe_streams(entry["path"])
                _check_dims(entry, w, h, ratio=True)
        elif format == "ref2va":
            for entry in media["images"]:
                _, w, h, _ = providers.probe_streams(entry["path"])
                _check_dims(entry, w, h)
            v_total = 0.0
            for entry in all_videos:
                d, w, h = providers.probe(entry["path"])
                _check_dims(entry, w, h, ratio=True)
                _check_duration(entry, d)
                v_total += d
            if v_total > DUR_TOTAL_MAX + DUR_EPS:
                raise HTTPException(
                    400, f"视频总时长需 ≤ {DUR_TOTAL_MAX:g} 秒（当前 {v_total:.1f} 秒）")
            a_total = 0.0
            for entry in all_audios:
                d, _, _ = providers.probe(entry["path"])
                _check_duration(entry, d)
                a_total += d
            if a_total > DUR_TOTAL_MAX + DUR_EPS:
                raise HTTPException(
                    400, f"音频总时长需 ≤ {DUR_TOTAL_MAX:g} 秒（当前 {a_total:.1f} 秒）")
    except HTTPException:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise

    # ---- 长宽比按模式解析 ----
    # 首尾帧：遵循首张输入图片的原始比例；全参考+auto：由模型自行判断；其余用所选固定比例
    img_ratio = None
    if media["images"]:
        _, iw, ih, _ = providers.probe_streams(media["images"][0]["path"])
        if iw and ih:
            img_ratio = providers.ratio_from_dims(iw, ih)

    if format == "fl2va":
        final_aspect = img_ratio or aspect
    elif format == "ref2va" and aspect == "auto":
        final_aspect = "auto"  # 透传给模型判断；演示渲染回退参考图/16:9
    else:
        final_aspect = aspect

    demo_ratio = final_aspect if final_aspect != "auto" else (img_ratio or "16:9")
    w, h = providers.dims_for(demo_ratio, resolution)
    params = {
        "format": format, "aspect": final_aspect, "resolution": resolution,
        "width": w, "height": h, "duration": duration,
        "short_edge": providers.RES_MODES[resolution]["short"],
    }

    TASKS[task_id] = {
        "status": "queued",
        "progress": 0.02,
        "message": "已提交",
        "video_id": None,
        "error": None,
        "_ts": time.time(),
    }
    t = asyncio.create_task(run_task(task_id, prompt, params, model_id, model_name,
                                   provider, media, inputs_meta, task_dir))
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return {"task_id": task_id}


async def run_task(task_id: str, prompt: str, params: dict,
                   model_id: str, model_name: str, provider,
                   media: dict, inputs_meta: list, task_dir: Path) -> None:
    task = TASKS[task_id]

    def report(p: float, msg: str) -> None:
        task["progress"] = max(0.0, min(1.0, p))
        task["message"] = msg
        task["_ts"] = time.time()

    try:
        path = await provider.generate(prompt, params, report, media)
        report(0.97, "保存中")
        meta = register_video(path, prompt, model_id, model_name, provider.name,
                              inputs=inputs_meta)
        task.update(status="done", progress=1.0, message="完成", video_id=meta["id"])
    except Exception as e:  # noqa: BLE001
        task.update(status="error", error=str(e) or "生成失败", message="失败")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)  # 生成结束即清理上传的附件


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已过期")
    return {k: v for k, v in task.items() if not k.startswith("_")}


# ---------------- 视频列表 / 文件 ----------------

@app.get("/api/videos")
def list_videos():
    return {"videos": INDEX}


@app.get("/api/videos/{vid}/file")
def video_file(vid: str):
    meta = get_meta(vid)
    # 浏览器播放/拖动进度条时会持续发 range 请求，默认 HTTP keep-alive 让连接长期
    # 保持；这会导致 Ctrl+C 后 uvicorn 的 server.wait_closed() 一直等这些连接断开而
    # 卡在 Shutting down。设 Connection: close 让每个 range 请求完成后即断开，
    # 关闭服务时进程能立即退出（同时在 lifespan 里也装了 Ctrl+C 兜底处理器）。
    return FileResponse(
        VIDEOS_DIR / meta["file"],
        media_type="video/mp4",
        headers={"Connection": "close"},
    )


@app.delete("/api/videos/{vid}")
def delete_video(vid: str):
    meta = get_meta(vid)
    try:
        (VIDEOS_DIR / meta["file"]).unlink()
    except OSError:
        pass
    INDEX.remove(meta)
    save_index()
    return {"ok": True}


# ---------------- 裁剪 ----------------

class TrimReq(BaseModel):
    start: float
    end: float


@app.post("/api/videos/{vid}/trim")
async def trim_video(vid: str, req: TrimReq):
    meta = get_meta(vid)
    start = max(0.0, float(req.start))
    end = min(float(req.end), meta["duration"])
    if not (start < end) or end - start < 0.2:
        raise HTTPException(400, "裁剪区间无效（至少 0.2 秒）")

    out = VIDEOS_DIR / f"tmp_{uuid.uuid4().hex[:8]}.mp4"
    ok, err = await asyncio.to_thread(
        providers.ffmpeg_cut, VIDEOS_DIR / meta["file"], start, end, out
    )
    if not ok:
        raise HTTPException(500, f"裁剪失败: {err}")
    return register_video(
        out, meta["prompt"], meta["model_id"], meta["model_name"],
        meta["provider"], kind="trim", parent_id=vid,
    )


# ---------------- 拼接 ----------------

class MergeSeg(BaseModel):
    id: str
    start: float = 0.0
    end: float = 0.0


class MergeReq(BaseModel):
    segments: list[MergeSeg]


@app.post("/api/videos/merge")
async def merge_videos(req: MergeReq):
    if len(req.segments) < 2:
        raise HTTPException(400, "拼接至少需要 2 个片段")
    segs = []
    for s in req.segments:
        m = get_meta(s.id)  # 不存在时抛 404
        start = max(0.0, float(s.start))
        end = min(float(s.end) or m["duration"], m["duration"])
        if end - start < 0.2:
            raise HTTPException(400, "存在裁剪区间过短（&lt;0.2 秒）的片段")
        segs.append({"path": VIDEOS_DIR / m["file"], "start": start, "end": end, "meta": m})

    purge_stale_tasks()
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "status": "queued",
        "progress": 0.02,
        "message": "已提交",
        "video_id": None,
        "error": None,
        "_ts": time.time(),
    }
    t = asyncio.create_task(run_merge_task(task_id, segs))
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return {"task_id": task_id}


async def run_merge_task(task_id: str, segs: list[dict]) -> None:
    task = TASKS[task_id]

    def report(p: float, msg: str) -> None:
        task["progress"] = max(0.0, min(1.0, p))
        task["message"] = msg
        task["_ts"] = time.time()

    try:
        report(0.05, "准备片段")
        path = await asyncio.to_thread(providers.ffmpeg_merge, segs, report)
        report(0.97, "保存中")
        prompt = " ＋ ".join((s["meta"]["prompt"] or "")[:14] for s in segs)[:80]
        meta = register_video(path, prompt, "merge", "拼接", "local", kind="merge")
        task.update(status="done", progress=1.0, message="完成", video_id=meta["id"])
    except Exception as e:  # noqa: BLE001
        task.update(status="error", error=str(e) or "拼接失败", message="失败")


# ---------------- 批量下载 ----------------

def _safe_name(meta: dict) -> str:
    base = re.sub(r'[\\/:*?"<>|\s]+', "-", meta["prompt"])[:24].strip("-")
    return f"{base or meta['id']}-{meta['id']}"


@app.get("/api/download")
def download(ids: str = ""):
    id_list = [s.strip() for s in ids.split(",") if s.strip()]
    metas = []
    for vid in id_list:
        try:
            meta = get_meta(vid)
        except HTTPException:
            continue
        if (VIDEOS_DIR / meta["file"]).exists():
            metas.append(meta)
    if not metas:
        raise HTTPException(400, "没有可下载的视频")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for meta in metas:
            zf.write(VIDEOS_DIR / meta["file"], arcname=f"{_safe_name(meta)}.mp4")
    buf.seek(0)
    ts = time.strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="videos-{ts}.zip"'},
    )


# 静态资源挂载放最后，避免覆盖 /api 路由
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    url = f"http://{host}:{port}"
    print(f"视频模型测试广场已启动：{url}", flush=True)
    print("视频与上传文件保存在：" + str(VIDEOS_DIR), flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    if os.getenv("T2V_NO_BROWSER", "") != "1":  # 设为 1 则不自动打开浏览器
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")

"""文生视频提供方（Provider）。

- DemoProvider    : 本地演示渲染。按「模型格式 + 自定义尺寸/时长」用 ffmpeg 的
                    gradients 滤镜生成一段动态渐变视频（画面随提示词变化，
                    仅左下角保留小水印），模拟真实文生视频的耗时与进度。
- CustomAPIProvider: 用户在界面填写的自定义模型 API 接口。POST
                    {prompt, format, width, height, duration}，支持返回
                    视频二进制或含视频 URL 的 JSON。
- ZhipuProvider   : 智谱开放平台 CogVideoX 文生视频 API（在 .env 中配置
                    T2V_PROVIDER=zhipu 与 ZHIPU_API_KEY 后启用）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

import imageio_ffmpeg


def _ffmpeg_exe() -> str:
    """ffmpeg 可执行文件路径。

    打包后（PyInstaller）优先使用随包分发的副本，因为此时
    imageio_ffmpeg 的 importlib.resources 定位方式不可用。
    """
    if getattr(sys, "frozen", False):
        bundled_dir = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg"
        for name in ("ffmpeg.exe", "ffmpeg"):  # Windows / macOS、Linux
            cand = bundled_dir / name
            if cand.is_file():
                return str(cand)
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _ffmpeg_exe()

_CANDIDATE_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",           # 微软雅黑
    r"C:\Windows\Fonts\segoeui.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT = next((p for p in _CANDIDATE_FONTS if os.path.exists(p)), None)

ProgressFn = Callable[[float, str], None]

# ---------------- 生成设置：模型格式与默认参数 ----------------

FORMAT_PRESETS: list[dict] = [
    {
        "id": "t2va", "desc": "文生视频",
        "speed": 0.05, "grain": 0, "vignette": False,
        "palettes": [
            ["0x0B1B3F", "0x2E7CF6", "0x9AD9FF"],
            ["0x062B26", "0x0FA97E", "0xB8F1E0"],
            ["0x1E1B4B", "0x6366F1", "0xC7D2FE"],
        ],
    },
    {
        "id": "fl2va", "desc": "首尾帧生视频",
        "speed": 0.035, "grain": 5, "vignette": False,
        "palettes": [
            ["0x2B0A4E", "0x7C3AED", "0xF0ABFC"],
            ["0x3B0764", "0xDB2777", "0xFDE68A"],
            ["0x0C2D48", "0x1450A3", "0xFCD9A6"],
        ],
    },
    {
        "id": "ref2va", "desc": "全参考生视频",
        "speed": 0.02, "grain": 9, "vignette": True,
        "palettes": [
            ["0x1C0A05", "0xB45309", "0xFCD34D"],
            ["0x0F0B02", "0x9A3412", "0xFBBF24"],
            ["0x111827", "0x334155", "0xE2E8F0"],
        ],
    },
]

GEN_DEFAULTS: dict = {
    "format": "t2va",
    "aspect": "16:9",
    "ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "resolution": "720p",
    "resolutions": ["720p", "768p", "1440p"],
    "duration": 8,
    "duration_min": 5, "duration_max": 15,
    "short_edge": 720,
}

# 分辨率模式：区间内（16:9 ~ 9:16）按短边；超宽/超窄按 32px 网格面积预算
# budget 为 32×32 网格单位数：768p → 1008 单位 ≈ 1.03M 像素（21:9 → 1536×672）
#                          1440p → 3627 单位 ≈ 3.71M 像素（21:9 → 2976×1248）
RES_MODES: dict = {
    "720p": {"short": 720},
    "768p": {"short": 768, "budget": 1008},
    "1440p": {"short": 1440, "budget": 3627},
}


def _parse_ratio(ratio: str) -> tuple[int, int]:
    try:
        a, b = (int(x) for x in ratio.split(":"))
        if a <= 0 or b <= 0:
            raise ValueError
        return a, b
    except ValueError:
        return 16, 9


def dims_for(ratio: str, resolution: str = "720p") -> tuple[int, int]:
    """按长宽比 + 分辨率模式推算输出尺寸（偶数）。

    - 比例在 16:9 ~ 9:16 之间：短边 = 模式短边像素（720 / 768 / 1440）
    - 超出该范围（如 21:9）：768p/1440p 按 32px 网格面积预算取最接近
      目标比例的因子组合（≈1.03M / ≈3.71M 像素）；720p 仍按短边
    """
    a, b = _parse_ratio(ratio)
    mode = RES_MODES.get(resolution, RES_MODES["720p"])
    r = a / b
    in_range = (9 / 16) - 1e-9 <= r <= (16 / 9) + 1e-9

    if in_range or "budget" not in mode:
        short = mode["short"]
        if a >= b:
            h, w = short, round(short * a / b)
        else:
            w, h = short, round(short * b / a)
        return w - (w % 2), h - (h % 2)

    # 面积预算：在 32px 网格上找 x*y=budget 且比例最接近 a:b 的组合
    budget = mode["budget"]
    long_side, short_side = max(a, b), min(a, b)
    target = long_side / short_side
    best = (budget, 1)
    d = 1
    while d * d <= budget:
        if budget % d == 0:
            p, q = budget // d, d
            if abs(p / q - target) < abs(best[0] / best[1] - target):
                best = (p, q)
        d += 1
    p, q = best
    return (32 * p, 32 * q) if a >= b else (32 * q, 32 * p)


def ratio_from_dims(w: int, h: int) -> str:
    """由像素尺寸归约出「宽:高」比例字符串。"""
    if w <= 0 or h <= 0:
        return "16:9"
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


def format_preset(fmt_id: str) -> dict:
    for f in FORMAT_PRESETS:
        if f["id"] == fmt_id:
            return f
    raise ValueError(f"未知模型格式: {fmt_id}")


def probe(path: Path) -> tuple[float, int, int]:
    """返回 (时长秒, 宽, 高)。"""
    dur, w, h, _ = probe_streams(path)
    return dur, w, h


def probe_streams(path: Path) -> tuple[float, int, int, bool]:
    """返回 (时长秒, 宽, 高, 是否含音轨)。"""
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    info = r.stderr or ""
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    w = h = 0
    has_audio = "Audio:" in info
    for line in info.splitlines():
        if "Video:" in line:
            mm = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if mm:
                w, h = int(mm[1]), int(mm[2])
                break
    return dur, w, h, has_audio


def ffmpeg_cut(src: Path, start: float, end: float, out: Path) -> tuple[bool, str]:
    """按时间区间裁剪视频（重编码，帧精确）。"""
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{max(0.0, end - start):.3f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == 0 and out.exists() and out.stat().st_size > 1000
    return ok, (r.stderr or "")[-300:]


def ffmpeg_merge(segments: list[dict], report: ProgressFn) -> Path:
    """按顺序拼接多个片段（各自可带 [start, end] 裁剪区间）。

    统一规格：首段分辨率（其余缩放+黑边适配）、24fps、44.1kHz 立体声；
    无音轨的片段自动补静音，保证输出始终含音轨。
    """
    infos = [probe_streams(Path(s["path"])) for s in segments]
    W, H = infos[0][1], infos[0][2]
    n = len(segments)
    total = sum(s["end"] - s["start"] for s in segments)

    in_args: list[str] = []
    fc: list[str] = []
    audio_labels: list[str] = []
    for i, s in enumerate(segments):
        in_args += [
            "-ss", f"{s['start']:.3f}",
            "-t", f"{s['end'] - s['start']:.3f}",
            "-i", str(s["path"]),
        ]
        fc.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24[v{i}]"
        )
    ai = n
    for i, s in enumerate(segments):
        if infos[i][3]:
            fc.append(f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            in_args += [
                "-f", "lavfi", "-t", f"{s['end'] - s['start']:.3f}",
                "-i", "anullsrc=r=44100:cl=stereo",
            ]
            fc.append(f"[{ai}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
            ai += 1
        audio_labels.append(f"[a{i}]")
    fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")
    fc.append("".join(audio_labels) + f"concat=n={n}:v=0:a=1[acat]")

    out = Path(tempfile.gettempdir()) / f"t2v_merge_{uuid.uuid4().hex[:8]}.mp4"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        *in_args,
        "-filter_complex", ";".join(fc),
        "-map", "[vcat]", "-map", "[acat]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(out),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                us = int(line.strip().split("=")[1])
            except ValueError:
                continue
            if us > 0 and total > 0:
                report(0.05 + 0.92 * min(1.0, us / 1e6 / total), "正在拼接")
    err = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait()
    if rc != 0 or not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError(f"拼接失败: {err[-250:] or f'ffmpeg 退出码 {rc}'}")
    report(0.97, "编码封装中")
    return out


def _fpath(p: Path | str) -> str:
    """把路径转成 ffmpeg 滤镜参数里可用的带引号形式。"""
    s = str(p).replace("\\", "/")
    s = s.replace(":", "\\:").replace("'", "\\'")
    return f"'{s}'"


_COND_BUDGET = 900 * 1024  # H3 风格服务单个表单部件上限 1024KB，留余量


def _data_uri_bytes(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _to_jpeg(path: Path) -> bytes:
    """图片转高画质 JPEG（ffmpeg -q:v 2），用于压缩超出部件上限的附件。"""
    out = Path(tempfile.gettempdir()) / f"t2v_cond_{uuid.uuid4().hex[:8]}.jpg"
    r = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-q:v", "2", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 100:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"附件压缩失败: {path.name}")
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data


def _cond_uri(entry: dict, *, json_mode: bool = False) -> str:
    """附件编码为 conditions 用的 data URI。

    - json_mode=False（默认）：用于 multipart/form-data 的 `conditions` 部件，
      单部件体积上限约 1MB（服务端硬限）。图片自动转 JPEG 压缩；视频/音频超
      限直接报错（multipart 路径下无法承载）。
    - json_mode=True：用于 JSON body POST，整请求体上限（通常 ≥100MB）远高于
      multipart 单部件，因此不强制 1MB 上限；图片仍自动转 JPEG 以减小请求体。
    """
    kind = entry.get("kind") or ""
    mime = entry.get("mime") or ""
    if "/" not in mime:
        mime = {"image": "image/png", "video": "video/mp4",
                "audio": "audio/mpeg"}.get(kind, "application/octet-stream")
    data = Path(entry["path"]).read_bytes()
    uri = _data_uri_bytes(mime, data)
    if json_mode:
        if kind == "image" and len(uri) > _COND_BUDGET:
            uri = _data_uri_bytes("image/jpeg", _to_jpeg(Path(entry["path"])))
        return uri
    if len(uri) <= _COND_BUDGET:
        return uri
    if kind != "image":
        raise RuntimeError(
            f"附件 {entry['name']} 过大（{len(data) / 1024 / 1024:.1f}MB），"
            "该接口单个表单部件上限 1MB，无法内联提交视频/音频")
    uri = _data_uri_bytes("image/jpeg", _to_jpeg(Path(entry["path"])))
    if len(uri) <= _COND_BUDGET:
        return uri
    raise RuntimeError(f"附件 {entry['name']} 压缩后仍超过接口 1MB 部件上限")
class DemoProvider:
    """本地演示渲染：无需任何 API Key，按用户设置格式/尺寸/时长出片。

    附件的演示语义：
    - 上传视频  → 作为画面底板（缩放适配、循环补足时长）
    - 上传图片  → 以参考卡片形式叠加在画面中央
    - 上传音频  → 混流为成片音轨（循环补足时长）
    - 均无      → 纯提示词驱动的动态渐变
    """

    name = "demo"

    async def generate(self, prompt: str, params: dict, report: ProgressFn,
                       media: dict | None = None) -> Path:
        preset = format_preset(params["format"])
        w, h, dur = params["width"], params["height"], params["duration"]
        report(0.04, "任务已提交")
        await asyncio.sleep(0.5)
        report(0.08, "模型加载中")
        await asyncio.sleep(0.4)
        return await asyncio.to_thread(
            self._render, prompt, preset, w, h, dur, report, media
        )

    def _render(self, prompt: str, preset: dict, w: int, h: int, dur: float,
                report: ProgressFn, media: dict | None = None) -> Path:
        media = media or {}
        images = media.get("images") or []
        video_in = media.get("video")
        audio_in = media.get("audio")

        seed = int(hashlib.md5(f"{prompt}|{preset['id']}|{time.time_ns()}".encode()).hexdigest()[:10], 16) % (2**31)
        rng = random.Random(seed)
        c0, c1, c2 = rng.choice(preset["palettes"])
        shape = rng.choice(["linear", "radial", "circular", "spiral", "square"])

        in_args: list[str] = []   # ffmpeg 输入参数
        fc: list[str] = []        # filter_complex 片段
        nxt = 0                   # 下一个输入索引

        # ---- 底层画面：上传视频（循环补足）或动态渐变 ----
        if video_in:
            in_args += ["-stream_loop", "-1", "-i", str(video_in["path"])]
            chain = [
                f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                "setsar=1", "fps=24",
            ]
        else:
            grad = (
                f"gradients=s={w}x{h}:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:seed={seed}"
                f":speed={preset['speed']}:type={shape}:duration={dur}:rate=24"
            )
            in_args += ["-f", "lavfi", "-i", grad]
            chain = []
        style = []
        if preset.get("grain"):
            style.append(f"noise=alls={preset['grain']}:allf=t+u")
        if preset.get("vignette"):
            style.append("vignette=PI/4.5")
        chain += style or ([] if video_in else ["null"])
        fc.append(f"[0:v]{','.join(chain)}[vbase]")
        nxt = 1
        cur = "[vbase]"

        # ---- 图片：水平参考卡片条，居中叠加 ----
        if images:
            ih = max(32, int(h * 0.36))
            labels = []
            for img in images:
                fc.append(f"[{nxt}:v]scale=-2:{ih}[im{nxt}]")
                labels.append(f"[im{nxt}]")
                in_args += ["-i", str(img["path"])]
                nxt += 1
            if len(labels) > 1:
                fc.append("".join(labels) + f"hstack=inputs={len(labels)}[strip0]")
            else:
                fc.append(f"{labels[0]}null[strip0]")
            # 参考条过宽时缩进画面内；min() 中的逗号需转义
            fc.append(f"[strip0]scale=min(iw\\,{w}):-2[strip]")
            # 注意：overlay 中 main_w/W 是主画面、overlay_w/w 是叠加图
            fc.append(f"{cur}[strip]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2[vimg]")
            cur = "[vimg]"

        # ---- 左下角水印 ----
        if FONT:
            fc.append(
                f"{cur}drawtext=fontfile={_fpath(FONT)}:text='{preset['id']}  ·  DEMO':"
                f"expansion=none:fontcolor=white@0.55:fontsize={max(11, int(h * 0.028))}:"
                f"x=w*0.03:y=h-th-h*0.055[vout]"
            )
            cur = "[vout]"

        # ---- 输出映射：画面 + 可选音频轨 ----
        map_args = ["-map", cur]
        a_args = ["-an"]
        if audio_in:
            in_args += ["-stream_loop", "-1", "-i", str(audio_in["path"])]
            map_args += ["-map", f"{nxt}:a"]
            a_args = ["-c:a", "aac", "-b:a", "128k"]

        out = Path(tempfile.gettempdir()) / f"t2v_{uuid.uuid4().hex[:10]}.mp4"
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            *in_args,
            "-filter_complex", ";".join(fc),
            *map_args,
            "-t", str(dur),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            *a_args,
            "-progress", "pipe:1",
            str(out),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    us = int(line.strip().split("=")[1])
                except ValueError:
                    continue
                if us > 0:
                    report(0.10 + 0.85 * min(1.0, us / 1e6 / dur), "正在生成画面")
        err = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
        if rc != 0 or not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError(f"渲染失败: {err[-200:] or f'ffmpeg 退出码 {rc}'}")
        report(0.97, "编码封装中")
        return out


class CustomAPIProvider:
    """用户自定义模型 API 接口，自适应两种风格：

    - 同步：POST 后直接返回 video/* 二进制，或 JSON 中含视频地址字段
      （url / video_url / video / download_url / data.url / video_result[0].url）。
    - 异步（Sora 风格视频服务）：POST 返回 {id, status, progress}，随后轮询
      {api_url}/{id}，完成后从 url 字段或 {api_url}/{id}/content 下载。
      已适配 MiniMax-H3 服务（/v1/videos）：multipart 提交
      task=t2va/fl2va/ref2va + extra_body={"target": {short_edge,
      aspect_ratio, duration_seconds}}，服务端对 short_edge 有硬性要求时
      自动按提示值重试。

    可选 api_key：填写后以 Authorization: Bearer 头随提交 / 轮询 / 同源
    下载请求发送（外部预签名下载地址不带，避免破坏其签名）。
    """

    name = "custom"

    _DONE = {"completed", "succeeded", "success", "done"}
    _FAILED = {"failed", "error", "cancelled", "canceled"}

    def __init__(self, api_url: str, api_key: str = ""):
        self.url = api_url
        self.base = api_url.rstrip("/")
        self.key = (api_key or "").strip()
        self.auth = {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def generate(self, prompt: str, params: dict, report: ProgressFn,
                       media: dict | None = None) -> Path:
        import httpx

        media = media or {}
        images = media.get("images") or []
        videos = media.get("videos") or ([media["video"]] if media.get("video") else [])
        audios = media.get("audios") or ([media["audio"]] if media.get("audio") else [])
        entries = [*images, *videos, *audios]
        # 参考图/视频走 input_reference 字段（fl2va / ref2va 语义）
        ref = (videos[0] if videos else None) or (images[0] if images else None)

        w, h, dur = params["width"], params["height"], params["duration"]
        aspect = params.get("aspect") or "16:9"
        fmt = params["format"]
        target = {
            "short_edge": params.get("short_edge") or min(w, h),
            "aspect_ratio": aspect,
            "duration_seconds": dur,
        }
        payload = {
            "prompt": prompt, "format": fmt, "aspect": aspect,
            "width": w, "height": h, "duration": dur,
        }
        out = Path(tempfile.gettempdir()) / f"t2v_{uuid.uuid4().hex[:10]}.mp4"
        timeout = httpx.Timeout(600.0, connect=10.0)

        report(0.08, "提交生成任务")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = None
            # 路径 1：JSON body POST（H3 协议原生方式；conditions[].uri 接受 data URI，
            # 整请求体通常 ≥100MB，可承载任意大小的视频/音频附件，无 1MB 单部件上限）
            try:
                r = await self._submit_json(client, payload, target, entries, fmt)
            except httpx.HTTPError as e:
                raise RuntimeError(f"无法连接模型接口: {e}") from e
            # JSON body 路径：short_edge 不符合服务端要求时，按服务端提示值自动重试一次
            # ——这是参数错误，应在 JSON body 路径里纠正；回退到 multipart 会撞 1MB 上限
            if r is not None and r.status_code in (400, 422):
                m = re.search(r"short_edge must be (\d+)", r.text or "")
                if m:
                    target["short_edge"] = int(m.group(1))
                    r = await self._submit_json(client, payload, target, entries, fmt)
            # 记录 JSON body 失败响应，供回退路径报错时附加上下文
            json_diag = ""
            if r is not None and r.status_code >= 400:
                json_diag = (f"（注：JSON body POST 已被服务端拒绝，"
                             f"返回 {r.status_code}: {(r.text or '')[:160]}；"
                             "如服务端支持 JSON body，本应能承载任意大小附件）")
            # 路径 2：仅在服务端完全不接受 JSON（404/415）时才回退到 multipart/form-data。
            # 400/422 一律不再回退（避免 short_edge/字段缺失等参数错误把大附件推到 1MB 限制下）
            if r is None or r.status_code in (404, 415):
                try:
                    r = await self._post_form(client, payload, target, entries, ref)
                    # multipart 兜底：同样按 short_edge 提示重试一次
                    if r.status_code in (400, 422):
                        m = re.search(r"short_edge must be (\d+)", r.text or "")
                        if m:
                            target["short_edge"] = int(m.group(1))
                            r = await self._post_form(client, payload, target, entries, ref)
                except RuntimeError as e:
                    # multipart 路径撞 1MB 单部件上限时，把 JSON body 失败的诊断信息附上
                    msg = str(e)
                    if ("1MB" in msg or "无法内联" in msg) and json_diag:
                        raise RuntimeError(f"{msg}\n{json_diag}") from e
                    raise
            if r.status_code != 200:
                msg = f"接口返回 {r.status_code}: {r.text[:180]}"
                if json_diag:
                    msg += "\n" + json_diag
                raise RuntimeError(msg)

            ctype = (r.headers.get("content-type") or "").lower()
            if ctype.startswith("video/"):
                report(0.7, "保存视频")
                out.write_bytes(r.content)
                if out.stat().st_size < 1000:
                    raise RuntimeError("接口返回的视频内容为空")
                report(0.97, "保存中")
                return out
            if "json" not in ctype:
                raise RuntimeError(f"接口响应类型不支持: {ctype or '未知'}（需 video/* 或 JSON）")

            data = r.json()
            url = self._extract_video_url(data)
            if url:
                return await self._download(client, url, out, report)
            if isinstance(data.get("id"), str) and ("status" in data or "progress" in data):
                return await self._poll(client, data["id"], out, report)
            raise RuntimeError("接口响应中未找到视频地址或任务 id")

    async def _post_form(self, client, payload: dict, target: dict,
                         entries: list, ref: dict | None):
        import httpx

        data = {
            "prompt": payload["prompt"],
            "task": payload["format"],
            "format": payload["format"],
            "aspect": payload.get("aspect", "16:9"),
            "seconds": str(int(payload["duration"])),
            "size": f"{payload['width']}x{payload['height']}",
            "width": str(payload["width"]),
            "height": str(payload["height"]),
            "duration": str(payload["duration"]),
            "extra_body": json.dumps({"target": target}),
        }
        # MiniMax-H3 风格服务：conditions 以 data URI 声明素材。
        # ref2va 全部素材作 reference；fl2va 首尾帧图片作 keyframe（0=首帧 -1=尾帧）。
        fmt = payload["format"]
        has_conds = False
        if fmt in ("ref2va", "fl2va") and entries:
            conds = []
            for i, e in enumerate(entries):
                c = {
                    "role": "keyframe" if fmt == "fl2va" else "reference",
                    "type": e.get("kind") or "image",
                    "uri": _cond_uri(e, json_mode=False),
                }
                if fmt == "fl2va":
                    # H3 协议：frame_index 仅接受 0(首帧) / -1(尾帧) / [0,-1](首尾指定)。
                    # 第 1 张图 → 0(首帧)，第 2 张图 → -1(尾帧)；单张图时即 [0]。
                    c["frame_index"] = 0 if i == 0 else -1
                conds.append(c)
            blob = json.dumps(conds)
            if len(blob) > 950 * 1024:
                raise RuntimeError("附件过多或过大：conditions 总量超过该接口 1MB 部件上限")
            data["conditions"] = blob
            has_conds = True
        files = []
        if ref is not None and not has_conds:
            # H3 风格服务的 input_reference 处理路径实测会触发其 VAE 内部错误，
            # 素材已随 conditions 提交时不再发送该部件
            files.append(("input_reference", (
                ref["name"], Path(ref["path"]).read_bytes(),
                ref["mime"] or "application/octet-stream")))
        for e in entries:
            files.append(("files", (
                e["name"], Path(e["path"]).read_bytes(),
                e["mime"] or "application/octet-stream")))
        if not files:
            # 部分服务只接受 multipart/form-data（urlencoded 会 500），无附件时补一个空件目强制 multipart
            files = [("_dummy", ("", b"", None))]
        try:
            return await client.post(self.url, data=data, files=files or None,
                                     headers=self.auth)
        except httpx.HTTPError as e:
            raise RuntimeError(f"无法连接模型接口: {e}") from e

    def _build_json_body(self, payload: dict, target: dict,
                         entries: list, fmt: str) -> dict:
        """构造 H3 协议 JSON body：conditions[] 数组里 uri 用 data URI 承载附件。

        与 multipart 路径（`_post_form`）相比：JSON body 无单部件 1MB 上限，
        因此可承载任意大小的视频/音频；图片超限仍自动转 JPEG 减小请求体。
        fl2va 的首/尾帧语义沿用 `frame_index = 0 / -1`。
        """
        body = {
            **payload,
            "task": fmt,
            "seconds": str(int(payload["duration"])),
            "size": f"{payload['width']}x{payload['height']}",
            "width": str(payload["width"]),
            "height": str(payload["height"]),
            "duration": str(payload["duration"]),
            "target": target,
        }
        if fmt in ("ref2va", "fl2va") and entries:
            conds = []
            for i, e in enumerate(entries):
                c = {
                    "role": "keyframe" if fmt == "fl2va" else "reference",
                    "type": e.get("kind") or "image",
                    "uri": _cond_uri(e, json_mode=True),
                }
                if fmt == "fl2va":
                    c["frame_index"] = 0 if i == 0 else -1
                conds.append(c)
            body["conditions"] = conds
        return body

    async def _submit_json(self, client, payload: dict, target: dict,
                           entries: list, fmt: str):
        """POST JSON body（提取为辅助方法以便 short_edge 错误时重试）。"""
        return await client.post(
            self.url,
            json=self._build_json_body(payload, target, entries, fmt),
            headers=self.auth,
        )

    async def _poll(self, client, vid: str, out: Path, report: ProgressFn) -> Path:
        report(0.12, "任务已提交，排队中")
        heartbeat = 0.12  # 本地心跳进度：远端不返回 progress 时用于持续推进 UI
        deadline = time.monotonic() + 600  # 最长 10 分钟
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            r = await client.get(f"{self.base}/{vid}", headers=self.auth)
            if r.status_code != 200:
                raise RuntimeError(f"查询任务失败 {r.status_code}: {r.text[:120]}")
            data = r.json()
            status = str(data.get("status") or "").lower()
            prog = data.get("progress")
            # 两分支：远端给了真实进度 → 直接用；远端没给进度（包括 queued、
            # processing、status 为空等所有情况）→ 本地心跳稳步推进到 0.85。
            # 注：之前 queued 状态单独封顶 0.30，会让 H3 在推理阶段（服务端仍
            # 返回 queued 或无 status）卡在 30% 不动；现在统一推进。
            if isinstance(prog, (int, float)) and 0 < prog <= 100:
                pct = int(prog)
                cur = 0.12 + 0.73 * pct / 100
                heartbeat = cur
                report(cur, f"模型生成中 {pct}%")
            else:
                heartbeat = min(0.85, heartbeat + 0.04)
                label = "排队中，等待模型空闲" if status in (
                    "queued", "pending", "waiting", "in_queue"
                ) else "模型生成中"
                report(heartbeat, label)
            if status in self._FAILED:
                raise RuntimeError(data.get("error") or f"模型任务失败（{status}）")
            if status in self._DONE:
                url = data.get("url")
                if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                    urls = data.get("urls")
                    url = urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else None
                if not url:
                    url = f"{self.base}/{vid}/content"
                return await self._download(client, url, out, report)
        raise RuntimeError("生成超时（10 分钟）")

    async def _download(self, client, url: str, out: Path, report: ProgressFn) -> Path:
        import httpx

        report(0.92, "下载视频")
        # 仅同源地址带鉴权头；外部地址多为预签名 URL，附加头会导致签名校验失败
        headers = self.auth if url.startswith(self.base) else {}
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                got = 0
                with open(out, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            report(0.92 + 0.06 * got / total, "下载视频")
        except httpx.HTTPError as e:
            raise RuntimeError(f"下载视频失败: {e}") from e
        if out.stat().st_size < 1000:
            raise RuntimeError("下载的视频内容为空")
        report(0.97, "保存中")
        return out

    @staticmethod
    def _extract_video_url(data) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("url", "video_url", "video", "download_url"):
            v = data.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        d = data.get("data")
        if isinstance(d, dict):
            for key in ("url", "video_url", "video"):
                v = d.get(key)
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    return v
        vr = data.get("video_result")
        if isinstance(vr, list) and vr and isinstance(vr[0], dict):
            v = vr[0].get("url")
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        return None


class ZhipuProvider:
    """智谱开放平台 CogVideoX 文生视频 API。"""

    name = "zhipu"

    MODELS: list[dict] = [
        {"id": "cogvideox-3", "name": "CogVideoX-3", "desc": "1080P · 约 10 秒 · 旗舰"},
        {"id": "cogvideox-2", "name": "CogVideoX-2", "desc": "720P · 约 6 秒"},
        {"id": "cogvideox-flash", "name": "CogVideoX-Flash", "desc": "720P · 极速生成"},
    ]

    def __init__(self, api_key: str, base_url: str = "https://open.bigmodel.cn"):
        if not api_key:
            raise RuntimeError("已选择 zhipu 提供方，但未配置 ZHIPU_API_KEY（请查看 .env.example）")
        self.key = api_key
        self.base = base_url.rstrip("/")

    def list_models(self) -> list[dict]:
        return [dict(m) for m in self.MODELS]

    async def generate(self, prompt: str, params: dict, report: ProgressFn,
                       media: dict | None = None) -> Path:
        import httpx

        model_id = self.MODELS[0]["id"]
        headers = {"Authorization": f"Bearer {self.key}"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base}/api/paas/v4/videos/generations",
                headers=headers,
                json={"model": model_id, "prompt": prompt},
            )
            if r.status_code != 200:
                raise RuntimeError(f"提交任务失败: {r.text[:200]}")
            tid = r.json().get("id")
            if not tid:
                raise RuntimeError(f"提交任务失败: {r.text[:200]}")

        report(0.08, "任务已提交，排队中")
        progress = 0.08
        url = None
        for _ in range(150):  # 最长约 10 分钟
            await asyncio.sleep(4)
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{self.base}/api/paas/v4/videos/generations/{tid}", headers=headers
                )
                data = r.json()
            status = data.get("task_status")
            if status == "SUCCESS":
                results = data.get("video_result") or [{}]
                url = results[0].get("url")
                if not url:
                    raise RuntimeError("生成成功但未返回视频地址")
                break
            if status == "FAIL":
                raise RuntimeError(data.get("message") or "模型生成失败")
            progress = min(0.90, progress + 0.05)
            report(progress, "模型生成中")
        if not url:
            raise RuntimeError("生成超时")

        report(0.92, "下载视频")
        out = Path(tempfile.gettempdir()) / f"t2v_{uuid.uuid4().hex[:10]}.mp4"
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("GET", url) as r:
                total = int(r.headers.get("content-length") or 0)
                got = 0
                with open(out, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            report(0.92 + 0.07 * got / total, "下载视频")
        return out


def get_provider() -> DemoProvider | ZhipuProvider:
    kind = os.getenv("T2V_PROVIDER", "demo").strip().lower()
    if kind == "zhipu":
        return ZhipuProvider(
            os.getenv("ZHIPU_API_KEY", ""),
            os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn"),
        )
    return DemoProvider()

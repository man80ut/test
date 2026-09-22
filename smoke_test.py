"""一次性冒烟测试：启动打包后的 exe，验证页面、接口与 ffmpeg 渲染链路。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXE = DIST / "VideoStudio.exe"
PORT = "8123"
BASE = f"http://127.0.0.1:{PORT}"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理


def get(path: str) -> tuple[int, bytes]:
    with opener.open(BASE + path, timeout=20) as r:
        return r.status, r.read()


def post_form(path: str, fields: dict) -> tuple[int, bytes]:
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += f"{v}\r\n".encode("utf-8")
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with opener.open(req, timeout=60) as r:
        return r.status, r.read()


env = dict(os.environ, T2V_NO_BROWSER="1", PORT=PORT, PYTHONIOENCODING="utf-8")
proc = subprocess.Popen(
    [str(EXE)], cwd=str(DIST), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    errors="replace",
)
try:
    ready = False
    for _ in range(90):  # 最多等 90 秒（含 78MB 解压）
        if proc.poll() is not None:
            print("exe 提前退出：\n", proc.stdout.read())
            sys.exit(1)
        try:
            st, _ = get("/api/models")
            ready = st == 200
            if ready:
                break
        except Exception:
            time.sleep(1)
    assert ready, "服务未在预期时间内启动"
    t0 = time.time()
    print(f"[OK] 启动耗时约 {t0:.1f}s（含 78MB 自解压）")

    st, body = get("/")
    print(f"[{'OK' if st == 200 else 'FAIL'}] GET / -> {st}, {len(body)} bytes")

    st, body = get("/api/models")
    models = json.loads(body)
    print(f"[{'OK' if st == 200 else 'FAIL'}] GET /api/models -> provider={models['provider']}")

    st, body = get("/api/videos")
    print(f"[{'OK' if st == 200 else 'FAIL'}] GET /api/videos -> {body[:80]}")

    # 关键：跑一次真实渲染，验证打包进 exe 的 ffmpeg 可用
    st, body = post_form("/api/generate", {
        "prompt": "打包冒烟测试", "format": "t2va",
        "aspect": "16:9", "resolution": "720p", "duration": "5",
    })
    assert st == 200, f"提交生成失败 {st}: {body[:200]}"
    tid = json.loads(body)["task_id"]
    print(f"[OK] POST /api/generate -> task {tid}")

    for _ in range(60):
        time.sleep(2)
        st, body = get(f"/api/tasks/{tid}")
        t = json.loads(body)
        print(f"     ...{t['status']} {t['progress']:.2f} {t['message']}")
        if t["status"] in ("done", "error"):
            break
    if t["status"] != "done":
        print(f"[FAIL] 生成未成功: {t}")
        sys.exit(1)
    print(f"[OK] ffmpeg 渲染完成 -> video {t['video_id']}")

    st, body = get(f"/api/videos/{t['video_id']}/file")
    print(f"[{'OK' if st == 200 else 'FAIL'}] 视频文件下载 -> {st}, {len(body)} bytes")

    saved = list((DIST / "videos").glob("*.mp4"))
    print(f"[OK] 视频已写入 exe 同级目录 videos/: {[p.name for p in saved]}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("--- exe 输出 ---")
    print((proc.stdout.read() or "").strip()[-1500:])

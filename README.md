# 视频模型测试广场

anything to video/audio · 基于 FastAPI 的文生视频（Text-to-Video）网页应用：
输入提示词 → 调整生成设置 → 生成视频，
左侧播放器支持播放/暂停、进度拖拽、**时间裁剪**、**拖拽调整尺寸**；右侧生成记录支持点选播放、勾选**批量下载（ZIP）**。
顶栏提供「主页 / 操作台」导航：主页回到大输入框首屏，操作台进入生成后的工作区。

## 快速开始

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
# 或直接双击 run.bat
```

浏览器打开 <http://127.0.0.1:8000>。

默认使用 `demo` 提供方：无需任何 API Key，由内置 ffmpeg（imageio-ffmpeg 自带）按提示词哈希
和你设置的**格式 / 尺寸 / 时长**渲染一段动态渐变视频（画面随提示词变化，仅在左下角保留小水印），
完整模拟「提交任务 → 排队 → 生成进度 → 出片」流程。

## 接入真实文生视频 API

- **界面填入（推荐）**：右上角「生成设置 → 模型 API 接口」填入你的服务地址；
  接口需要鉴权时在下方「API Key」填入密钥并点「检测」即时验证——后端带
  `Authorization: Bearer` 头发送空载荷 POST，`401/403` 判定无效，其余状态码
  （含 400/422 等参数校验错误）说明已通过鉴权；Key 会随后续生成请求一并发送。
  约定：`POST {prompt, format, width, height, duration}`，响应返回
  `video/*` 二进制，或 JSON 中含视频地址（`url` / `video_url` / `video` / `data.url` / `video_result[0].url`），后端自动下载入库。
  带附件时改为 multipart 表单：文本字段同名，图片/视频/音频以 `files` 字段一并转发（首个图/视频同时以 `input_reference` 提供）；
  MiniMax-H3 风格服务（如 `http://<host>:<port>/v1/videos`）会额外附带 `conditions` 表单字段——
  ref2va 全部素材作 `reference`、fl2va 首尾帧图片作 `keyframe`（`frame_index` 0/1），素材以 data URI 内联引用。
  演示模式下附件会被真实合成：上传视频作画面底板、图片作中央参考卡、音频混流为音轨。
- **异步视频服务（Sora 风格）**：POST 返回 `{id, status}` 时自动轮询 `{接口}/{id}`，
  完成后从 `url` 字段或 `{接口}/{id}/content` 下载。已实测适配 MiniMax-H3 服务
  （`http://<host>:<port>/v1/videos`）：自动以 `task=t2va/fl2va/ref2va` +
  `extra_body={"target":{short_edge, aspect_ratio, duration_seconds}}` 提交，
  宽高换算为「宽高比 + 短边」，服务端对短边有硬性要求（如 768）时按提示自动重试。
- **或用 .env**：复制 `.env.example` 为 `.env`，将 `T2V_PROVIDER` 改为 `zhipu` 并填入 `ZHIPU_API_KEY`
  （智谱开放平台 CogVideoX）。

其它服务商可在 `providers.py` 中按同样接口新增 Provider（`generate(prompt, params, report)`），
`main.py` 无需改动。

## 功能对照

| 需求 | 实现 |
| --- | --- |
| 右上角生成设置面板 | 模型格式三选一（t2va / fl2va / ref2va）+ 模型 API 接口输入 + API Key 输入与「检测」（Bearer 鉴权验证）+ 长宽比按钮 + 5–15 秒时长滑杆，按钮实时显示当前配置摘要 |
| 长宽比（按模式） | t2va：固定六档 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16；fl2va：自动遵循首张输入图片的原始长宽比；ref2va：固定六档或「自动」（由模型自行判断，已验证 H3 接受 aspect_ratio=auto） |
| 输出分辨率 | 720p（短边 720，默认）/ 768p / 1440p 三档：比例在 16:9~9:16 之间时短边为对应像素；超宽（如 21:9）时 768p/1440p 改按总面积 ≈1.03M / ≈3.71M 像素在 32px 网格上取最接近比例的组合（21:9 → 1536×672 / 2976×1248） |
| 多模态输入 | 输入框左下「+」按钮 / 拖拽上传，附件的类型/数量/规格按生成模式限制（见下表）；附件以芯片形式展示在输入框内，可 × 删除，随提示词一同 multipart 提交 |
| 大输入框 → 生成后变小 | 首屏居中大输入框，点击「生成视频」后收缩为顶部紧凑输入条 |
| 中下左侧视频演示 + 可拖动尺寸 | 播放器卡片右下角拖拽手柄，自由调整宽高 |
| 播放/暂停、进度拖拽 | 自定义控制条：播放/暂停、可拖拽进度条、时间、静音、全屏、空格/方向键 |
| 视频裁剪并保存到右侧 | 「裁剪」模式：进度条上拖动起止手柄 / 按播放位置设点 / 循环预览 / 导出片段到右侧列表 |
| 视频拼接 | 「生成记录」中勾选 ≥2 个视频 → 点「视频拼接」打开编辑弹窗：时间线卡片可拖拽排序，跨片段连播预览（虚拟时间线），保存后以任务形式按卡片顺序整段拼接出片并写入生成记录（统一到首段分辨率、24fps，无音轨片段自动补静音） |
| 右侧选择视频演示 | 点击列表项即在左侧播放器加载播放 |
| 批量下载 | 勾选任意数量后一键下载 ZIP |

## 附件规格（按生成模式）

| 模式 | 图片 | 视频 | 音频 | 总数 |
| --- | --- | --- | --- | --- |
| t2va 文生视频 | 仅单个 ≤30MB | 仅单个 ≤50MB | 仅单个 ≤15MB | ≤9 个 |
| fl2va 首尾帧 | 0~2 张 · 宽高 256~5760px · 宽高比 5:2~2:5 | 不支持 | 不支持 | ≤2 |
| ref2va 全参考 | ≤9 张 · 宽高 256~5760px | ≤3 段 · 单段 2~15 秒 · 总时长 ≤15 秒 · 宽高 256~5760px · 宽高比 5:2~2:5 | ≤3 段 · 单段 2~15 秒 · 总时长 ≤15 秒 · 需搭配图片或视频（不能单独输入） | ≤12 个 |

> 各类型单个文件大小上限（图片 30MB / 视频 50MB / 音频 15MB）对所有模式生效，累计大小不限；
> API 请求体上限 64MB（超出返回 413）。附件规格不满足时返回 400 并提示具体原因，前端在添加/提交时同步校验。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/models` | 可选格式、默认参数（尺寸/时长范围） |
| POST | `/api/check_key` | 检测 API Key：`{api_url, api_key}` → `{ok, status, message}`（空载荷 Bearer 探测，401/403 即无效） |
| POST | `/api/generate` | 提交生成任务（multipart）：`prompt, format, api_url, api_key, aspect, resolution, duration, files[]` → `{task_id}` |
| GET | `/api/tasks/{task_id}` | 轮询任务状态/进度 |
| GET | `/api/videos` | 生成记录 |
| GET | `/api/videos/{id}/file` | 视频文件（支持 Range，可拖动进度） |
| POST | `/api/videos/{id}/trim` | 裁剪 `{start, end}` → 新视频入列 |
| POST | `/api/videos/merge` | 拼接 `{segments: [{id, start, end}, ...]}`（≥2 段）→ 异步任务 → 拼接视频入列 |
| DELETE | `/api/videos/{id}` | 删除 |
| GET | `/api/download?ids=a,b,c` | 批量下载 ZIP |

> 说明：生成任务状态保存在内存中（重启后进行中的任务会丢失），已生成视频持久化在 `videos/` 目录。
> 附件的类型/数量/规格限制见上文「附件规格（按生成模式）」。

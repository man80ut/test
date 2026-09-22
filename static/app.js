/* 拾帧 · 文生视频 前端逻辑 */
"use strict";

/* ---------------- 工具 ---------------- */

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function toast(msg, ms = 2600) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("#toastWrap").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, ms);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = "";
    try { msg = (await r.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(msg || `请求失败 (${r.status})`);
  }
  return r.json();
}

async function postForm(path, fd) {
  const r = await fetch(path, { method: "POST", body: fd });
  if (!r.ok) {
    let msg = "";
    try { msg = (await r.json()).detail || ""; } catch { /* ignore */ }
    throw new Error(msg || `请求失败 (${r.status})`);
  }
  return r.json();
}

/* ---------------- 附件上传（图片 / 视频片段 / 音频） ---------------- */

const MAX_FILES = 9;
// 单个素材大小上限（MB），按类型区分；总个数内不限制累计大小
const MAX_FILE_MB = { image: 30, video: 50, audio: 15 };
const KIND_LABEL = { image: "图片", video: "视频", audio: "音频" };
// 各模式附件规格（t2va 沿用通用上限：≤9 个 + 按类型 MB，无尺寸/时长限制）
const MODE_RULES = {
  fl2va: { label: "首尾帧模式", types: ["image"], images: 2, maxFiles: 2 },
  ref2va: { label: "全参考模式", types: ["image", "video", "audio"], images: 9, videos: 3, audios: 3, maxFiles: 12 },
};
const DIM_MIN = 256, DIM_MAX = 5760;                 // 图片/视频宽高像素范围
const RATIO_MIN = 2 / 5, RATIO_MAX = 5 / 2;          // 宽高比 5:2 ~ 2:5
const DUR_MIN = 2, DUR_MAX = 15, DUR_TOTAL_MAX = 15; // 单段 / 总时长（秒）
const DUR_EPS = 0.01;                                // 时长比较容差

const FILE_ICONS = {
  image: '<svg viewBox="0 0 24 24" width="13" height="13"><rect x="3" y="4.5" width="18" height="15" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="8.5" cy="10" r="1.7" fill="currentColor"/><path d="M4 17l4.8-4.8 3.4 3.4 3-3L20 17.4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  video: '<svg viewBox="0 0 24 24" width="13" height="13"><rect x="2.5" y="5" width="13" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M15.5 10.5l5.5-3v9l-5.5-3z" fill="currentColor"/></svg>',
  audio: '<svg viewBox="0 0 24 24" width="13" height="13"><path d="M9 18.5V6l11-2v12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="6.5" cy="18.5" r="2.5" fill="currentColor"/><circle cx="17.5" cy="16.5" r="2.5" fill="currentColor"/></svg>',
};
const X_ICON = '<svg viewBox="0 0 24 24" width="11" height="11"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';
const SLOT_PLUS_SVG = '<svg class="slot-plus" viewBox="0 0 24 24" width="24" height="24"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const ARROW_SVG = '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M5 12h13M13 6l5 6-5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function fileKindOf(file) {
  const t = (file.type || "").split("/")[0];
  if (["image", "video", "audio"].includes(t)) return t;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext)) return "image";
  if (["mp4", "mov", "webm", "avi", "mkv"].includes(ext)) return "video";
  if (["mp3", "wav", "m4a", "aac", "ogg", "flac"].includes(ext)) return "audio";
  return "";
}

/* 元数据探测：图片返回 {w,h}，音视频返回 {dur,w,h}（视频含尺寸），失败返回 null */
function probeImage(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const r = { w: img.naturalWidth, h: img.naturalHeight };
      URL.revokeObjectURL(url);
      resolve(r);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
    img.src = url;
  });
}

function probeAV(file, kind) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const el = document.createElement(kind === "video" ? "video" : "audio");
    el.preload = "metadata";
    el.onloadedmetadata = () => {
      const r = isFinite(el.duration)
        ? { dur: el.duration, w: el.videoWidth || 0, h: el.videoHeight || 0 }
        : null;
      URL.revokeObjectURL(url);
      resolve(r);
    };
    el.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
    el.src = url;
  });
}

async function fileProbe(f) {
  if (state.probes.has(f)) return state.probes.get(f);
  const kind = fileKindOf(f);
  const r = kind === "image" ? await probeImage(f) : await probeAV(f, kind);
  state.probes.set(f, r);
  return r;
}

/* 按模式校验单个已探测文件的尺寸/比例/时长，返回错误文案（null 表示通过） */
function fileRuleError(f, fmt, p) {
  const kind = fileKindOf(f);
  if (kind === "image") {
    if (p.w < DIM_MIN || p.w > DIM_MAX || p.h < DIM_MIN || p.h > DIM_MAX)
      return `图片宽高需在 ${DIM_MIN}~${DIM_MAX}px 之间：${f.name}（${p.w}×${p.h}）`;
    if (fmt === "fl2va" && (p.w / p.h < RATIO_MIN || p.w / p.h > RATIO_MAX))
      return `图片宽高比需在 5:2~2:5 之间：${f.name}（${p.w}×${p.h}）`;
  } else if (kind === "video") {
    if (p.dur < DUR_MIN - DUR_EPS || p.dur > DUR_MAX + DUR_EPS)
      return `视频单段时长需在 ${DUR_MIN}~${DUR_MAX} 秒：${f.name}（${p.dur.toFixed(1)} 秒）`;
    if (p.w < DIM_MIN || p.w > DIM_MAX || p.h < DIM_MIN || p.h > DIM_MAX)
      return `视频宽高需在 ${DIM_MIN}~${DIM_MAX}px 之间：${f.name}（${p.w}×${p.h}）`;
    if (p.w / p.h < RATIO_MIN || p.w / p.h > RATIO_MAX)
      return `视频宽高比需在 5:2~2:5 之间：${f.name}（${p.w}×${p.h}）`;
  } else if (kind === "audio") {
    if (p.dur < DUR_MIN - DUR_EPS || p.dur > DUR_MAX + DUR_EPS)
      return `音频单段时长需在 ${DUR_MIN}~${DUR_MAX} 秒：${f.name}（${p.dur.toFixed(1)} 秒）`;
  }
  return null;
}

function renderChips() {
  // 兼容旧调用点；文件预览已统一迁移到槽位内的缩略图网格，此函数不再渲染任何内容。
  return;
}

async function addFiles(list) {
  const errs = [];
  const fmt = state.settings.format;
  const rule = MODE_RULES[fmt];
  const unit = { image: "张", video: "段", audio: "段" };
  // 当前已选附件的类型计数与视频/音频总时长（增量校验用）
  const counts = { image: 0, video: 0, audio: 0 };
  let vdur = 0, adur = 0;
  for (const f of state.files) {
    const k = fileKindOf(f);
    if (k) counts[k] += 1;
    const p = state.probes.get(f);
    if (p) {
      if (k === "video") vdur += p.dur;
      else if (k === "audio") adur += p.dur;
    }
  }
  for (const f of list) {
    const max = rule?.maxFiles || MAX_FILES;
    if (state.files.length >= max) { errs.push(`附件最多 ${max} 个`); break; }
    const kind = fileKindOf(f);
    if (!kind) { errs.push(`不支持的附件类型：${f.name}`); continue; }
    if (rule && !rule.types.includes(kind)) {
      errs.push(`${rule.label}不支持${KIND_LABEL[kind]}附件：${f.name}`);
      continue;
    }
    const limit = MAX_FILE_MB[kind];
    if (f.size > limit * 1024 * 1024) { errs.push(`${KIND_LABEL[kind]}单个上限 ${limit}MB：${f.name}`); continue; }
    const cap = kind === "image" ? rule?.images : kind === "video" ? rule?.videos : rule?.audios;
    if (cap !== undefined && counts[kind] + 1 > cap) {
      errs.push(`${rule.label}${KIND_LABEL[kind]}最多 ${cap} ${unit[kind]}`);
      continue;
    }
    if (rule) { // fl2va/ref2va 需探测元数据校验尺寸/比例/时长
      const p = await fileProbe(f);
      const err = p ? fileRuleError(f, fmt, p) : `无法读取文件信息：${f.name}`;
      if (err) { errs.push(err); continue; }
      if (kind === "video" && vdur + p.dur > DUR_TOTAL_MAX + DUR_EPS) {
        errs.push(`视频总时长将超过 ${DUR_TOTAL_MAX} 秒（已选 ${vdur.toFixed(1)} 秒 + 本段 ${p.dur.toFixed(1)} 秒）`);
        continue;
      }
      if (kind === "audio" && adur + p.dur > DUR_TOTAL_MAX + DUR_EPS) {
        errs.push(`音频总时长将超过 ${DUR_TOTAL_MAX} 秒（已选 ${adur.toFixed(1)} 秒 + 本段 ${p.dur.toFixed(1)} 秒）`);
        continue;
      }
      if (kind === "video") vdur += p.dur;
      else if (kind === "audio") adur += p.dur;
    }
    state.files.push(f);
    counts[kind] += 1;
  }
  renderChips();
  refreshAttachSlots();
  if (errs.length) toast(errs[0], 3600);
}

function bindAttachments() {
  // 支持直接拖拽文件到输入框或槽位区
  const box = $("#promptBox");
  ["dragenter", "dragover"].forEach((ev) =>
    box.addEventListener(ev, (e) => {
      e.preventDefault();
      box.classList.add("dragover");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    box.addEventListener(ev, (e) => {
      e.preventDefault();
      box.classList.remove("dragover");
    }));
  box.addEventListener("drop", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    const fmt = state.settings.format;
    const files = [...e.dataTransfer.files];
    if (fmt === "fl2va") {
      // 拖到输入框区域（非槽位内）：路由到第一个空槽位；若两个都满则只取第一张
      const target = state.files.length < 1 ? 0 : 1;
      if (files[0]) addFileAtSlot(target, files[0]);
    } else {
      addFiles(files);
    }
  });
  bindAttachSlots();
}

/* ---------------- 上传槽位（按模式切换首帧/尾帧 或 全参考） ---------------- */

function renderAttachSlots() {
  const fmt = state.settings.format;
  const slotsEl = $("#attachSlots");
  if (fmt === "fl2va") {
    slotsEl.classList.remove("attach-grid-host");
    slotsEl.innerHTML = `
      <div class="attach-slot" data-slot="0">
        <div class="slot-inner">${SLOT_PLUS_SVG}<span class="slot-label">首帧</span></div>
        <img class="slot-thumb" alt="" hidden>
        <button type="button" class="slot-x" data-x="0" hidden title="移除">${X_ICON}</button>
      </div>
      <div class="attach-arrow">${ARROW_SVG}</div>
      <div class="attach-slot" data-slot="1">
        <div class="slot-inner">${SLOT_PLUS_SVG}<span class="slot-label">尾帧</span></div>
        <img class="slot-thumb" alt="" hidden>
        <button type="button" class="slot-x" data-x="1" hidden title="移除">${X_ICON}</button>
      </div>`;
    slotsEl.hidden = false;
  } else if (fmt === "ref2va") {
    slotsEl.classList.add("attach-grid-host");
    slotsEl.innerHTML = `<div class="attach-grid" id="attachGrid"></div>`;
    slotsEl.hidden = false;
  } else {
    slotsEl.classList.remove("attach-grid-host");
    slotsEl.innerHTML = "";
    slotsEl.hidden = true;
  }
}

/* 按当前 state.files 刷新槽位显示（不重建 fl2va 槽位 DOM，ref2va 用网格重渲染） */
function refreshAttachSlots() {
  const fmt = state.settings.format;
  const slotsEl = $("#attachSlots");
  if (fmt === "fl2va") {
    const slots = slotsEl.querySelectorAll(".attach-slot");
    slots.forEach((slotEl, i) => {
      const f = state.files[i];
      const thumb = slotEl.querySelector(".slot-thumb");
      const xBtn = slotEl.querySelector(".slot-x");
      const inner = slotEl.querySelector(".slot-inner");
      if (f) {
        thumb.src = URL.createObjectURL(f);
        thumb.hidden = false;
        xBtn.hidden = false;
        inner.hidden = true;
        slotEl.classList.add("filled");
      } else {
        thumb.removeAttribute("src");
        thumb.hidden = true;
        xBtn.hidden = true;
        inner.hidden = false;
        slotEl.classList.remove("filled");
      }
    });
  } else if (fmt === "ref2va") {
    const grid = slotsEl.querySelector(".attach-grid");
    if (!grid) return;
    const max = MODE_RULES.ref2va.maxFiles;
    const files = state.files;
    const items = files.map((f, i) => {
      const k = fileKindOf(f);
      const thumb =
        k === "image"
          ? `<img class="grid-thumb" alt="" src="${URL.createObjectURL(f)}">`
          : `<div class="grid-thumb grid-thumb-${k}">${FILE_ICONS[k] || ""}<span class="grid-name" title="${esc(f.name)}">${esc(f.name)}</span></div>`;
      return `<div class="grid-item" data-i="${i}">${thumb}<button type="button" class="grid-x" data-i="${i}" title="移除">${X_ICON}</button></div>`;
    }).join("");
    const addBtn =
      files.length < max
        ? `<div class="grid-add" data-role="add"><div class="grid-add-inner">${SLOT_PLUS_SVG}<span>添加</span><span class="grid-count">${files.length}/${max}</span></div></div>`
        : "";
    grid.innerHTML = items + addBtn;
  }
}

/* 模式切换下拉（文生视频 · 首/尾帧 · 全能参考）：根据 format 切换槽位与按钮可见性 */
function renderModeSwitcher() {
  const fmt = state.settings.format;
  const sw = $("#modeSwitcher");
  const labels = { t2va: "文生视频", fl2va: "首/尾帧", ref2va: "全能参考" };
  const available = (state.formats || []).map((f) => f.id).filter((id) => labels[id]);
  // 重新生成下拉项（仅展示当前服务器支持的格式）
  const panel = $("#modeSwitcherPanel");
  panel.innerHTML = available
    .map((id) => `<button type="button" class="ms-opt${id === fmt ? " on" : ""}" data-fmt="${id}" role="option">${labels[id]}</button>`)
    .join("");
  // 服务器至少支持 2 种格式、且当前 fmt 在其中 → 显示切换器
  const showSwitcher = available.length >= 2 && available.includes(fmt);
  sw.hidden = !showSwitcher;
  if (showSwitcher) $("#modeSwitcherLabel").textContent = labels[fmt];
}

function bindAttachSlots() {
  const slotsEl = $("#attachSlots");

  slotsEl.addEventListener("click", (e) => {
    // fl2va 槽位（含 ×）
    const slotEl = e.target.closest(".attach-slot");
    if (slotEl) {
      const x = e.target.closest(".slot-x");
      if (x) {
        e.stopPropagation();
        removeFileAt(parseInt(x.dataset.x, 10));
        return;
      }
      openSlotPicker(slotEl);
      return;
    }
    // ref2va 网格：删除单张
    const gridX = e.target.closest(".grid-x");
    if (gridX) {
      e.stopPropagation();
      removeFileAt(parseInt(gridX.dataset.i, 10));
      return;
    }
    // ref2va 网格：点击"添加"
    const gridAdd = e.target.closest(".grid-add");
    if (gridAdd) {
      openSlotPicker({ dataset: { slot: "all" } });
      return;
    }
  });

  // 拖拽到槽位（fl2va 单槽位 或 ref2va 网格）
  slotsEl.addEventListener("dragover", (e) => {
    if (!e.target.closest(".attach-slot, .attach-grid, .grid-add")) return;
    e.preventDefault();
    e.stopPropagation();
    const dropZone = e.target.closest(".attach-slot, .attach-grid");
    if (dropZone) dropZone.classList.add("dragover");
  });
  slotsEl.addEventListener("dragleave", (e) => {
    const dropZone = e.target.closest(".attach-slot, .attach-grid");
    if (dropZone) dropZone.classList.remove("dragover");
  });
  slotsEl.addEventListener("drop", (e) => {
    if (!e.target.closest(".attach-slot, .attach-grid, .grid-add")) return;
    e.preventDefault();
    e.stopPropagation();  // 阻止冒泡到 promptBox 导致重复处理
    slotsEl.querySelectorAll(".attach-slot.dragover, .attach-grid.dragover")
      .forEach((el) => el.classList.remove("dragover"));
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    const slotEl = e.target.closest(".attach-slot");
    if (slotEl) {
      handleSlotFiles(slotEl, [...e.dataTransfer.files]);
    } else {
      // ref2va 网格整区接收
      handleSlotFiles({ dataset: { slot: "all" } }, [...e.dataTransfer.files]);
    }
  });
}

function openSlotPicker(slotEl) {
  const fmt = state.settings.format;
  const inp = document.createElement("input");
  inp.type = "file";
  inp.style.cssText = "position:fixed;left:-9999px;top:0;";
  if (fmt === "fl2va") {
    inp.accept = "image/*";
  } else if (fmt === "ref2va") {
    inp.accept = "image/*,video/*,audio/*";
    inp.multiple = true;
  }
  inp.addEventListener("change", () => {
    if (inp.files.length) handleSlotFiles(slotEl, [...inp.files]);
    inp.remove();
  });
  document.body.appendChild(inp);
  inp.click();
}

async function handleSlotFiles(slotEl, files) {
  const fmt = state.settings.format;
  if (fmt === "fl2va") {
    if (files.length > 1) {
      toast("首/尾帧模式每次只能添加 1 张图片", 3000);
      return;
    }
    let idx = parseInt(slotEl.dataset.slot, 10);
    // 拖拽到"尾帧"但首帧还空：自动路由到首帧
    if (idx === 1 && state.files.length < 1) idx = 0;
    await addFileAtSlot(idx, files[0]);
  } else if (fmt === "ref2va") {
    addFiles(files);
  }
}

async function addFileAtSlot(idx, f) {
  const fmt = state.settings.format;
  const rule = MODE_RULES[fmt];
  const kind = fileKindOf(f);
  if (!kind) { toast(`不支持的文件类型：${f.name}`, 3000); return; }
  if (!rule.types.includes(kind)) {
    toast(`${rule.label}不支持${KIND_LABEL[kind]}附件`, 3000);
    return;
  }
  const limit = MAX_FILE_MB[kind];
  if (f.size > limit * 1024 * 1024) {
    toast(`${KIND_LABEL[kind]}单个上限 ${limit}MB：${f.name}`, 3600);
    return;
  }
  const p = await fileProbe(f);
  if (!p) { toast(`无法读取文件信息：${f.name}`, 3000); return; }
  const err = fileRuleError(f, fmt, p);
  if (err) { toast(err, 3600); return; }
  // 替换 idx 位或追加
  if (idx < state.files.length) {
    state.probes.delete(state.files[idx]);
    state.files[idx] = f;
  } else {
    state.files.push(f);
  }
  refreshAttachSlots();
}

function removeFileAt(idx) {
  if (idx < 0 || idx >= state.files.length) return;
  const removed = state.files.splice(idx, 1)[0];
  if (removed) state.probes.delete(removed);
  refreshAttachSlots();
}

function bindModeSwitcher() {
  const dd = $("#modeSwitcher");
  $("#modeSwitcherBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    dd.classList.toggle("open");
  });
  $("#modeSwitcherPanel").addEventListener("click", (e) => {
    e.stopPropagation();
    const btn = e.target.closest(".ms-opt");
    if (!btn) return;
    applyFormat(btn.dataset.fmt);
    dd.classList.remove("open");
  });
  document.addEventListener("click", () => dd.classList.remove("open"));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") dd.classList.remove("open");
  });
}

/* 统一应用格式变更：状态裁剪 + 比例/槽位/下拉/提示文案一并刷新 */
function applyFormat(fmt) {
  state.settings.format = fmt;
  syncDefaultApiUrl(fmt);
  pruneFilesForFormat(fmt);
  renderFormatSeg();
  renderRatioRow();
  renderAttachSlots();
  refreshAttachSlots();
  renderModeSwitcher();
  updateSettingsSummary();
  updateQuickChips();
  updateAttachHint();
}

/* ---------------- 状态 ---------------- */

const state = {
  formats: [],
  ratios: ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
  settings: { format: "t2va", apiUrl: "", apiKey: "", aspect: "16:9", resolution: "720p", duration: 8 },
  files: [],             // 待提交的附件 File 对象
  probes: new Map(),     // File -> 元数据缓存（{w,h} / {dur,w,h}）
  videos: [],
  tasks: new Map(),      // task_id -> {status, progress, message, prompt, fmt}
  activeTaskId: null,    // 点选展示「生成中」占位界面的任务
  currentId: null,
  selected: new Set(),
  trim: { on: false, start: 0, end: 0, dragging: null, previewing: false },
  playerSize: null,
  pollTimer: null,
  exporting: false,
};

const videoEl = $("#videoEl");
const playerCard = $("#playerCard");

/* ---------------- 模式切换 ---------------- */

function setMode(mode) {
  document.body.classList.toggle("mode-hero", mode === "hero");
  document.body.classList.toggle("mode-workspace", mode === "workspace");
  $("#homeNav").classList.toggle("active", mode === "hero");
  $("#consoleNav").classList.toggle("active", mode === "workspace");
  if (mode === "workspace" && !state.playerSize) {
    requestAnimationFrame(initPlayerSize);
  }
}

function bindNav() {
  // 主页：回到大输入框首屏
  $("#homeNav").addEventListener("click", () => setMode("hero"));
  // 操作台：进入生成后的工作区；若有历史视频则载入最新一条
  $("#consoleNav").addEventListener("click", () => {
    setMode("workspace");
    if (!state.currentId && state.videos.length && !videoEl.getAttribute("src")) {
      loadVideo(state.videos[0], { autoplay: false });
      updateEmptyProgress();
    }
  });
}

/* ---------------- 生成设置面板 ---------------- */

function updateSettingsSummary() {
  const s = state.settings;
  const ratioLabel = s.format === "fl2va" ? "按图片" : (s.aspect === "auto" ? "自动" : s.aspect);
  $("#settingsSummary").textContent = `${s.format} · ${ratioLabel} · ${s.resolution} · ${s.duration}s`;
}

/* 把当前 aspect / resolution / duration 同步到参数按钮的三个 chip */
function updateQuickChips() {
  const s = state.settings;
  const aspectTxt = s.format === "fl2va" ? "按图片" : (s.aspect === "auto" ? "自动" : s.aspect);
  const a = $("#quickChipAspect"); if (a) a.textContent = aspectTxt;
  const r = $("#quickChipRes");    if (r) r.textContent = s.resolution;
  const d = $("#quickChipDur");    if (d) d.textContent = `${s.duration}s`;
  // fl2va 模式下比例 chip 不可点（会让 chip 看起来"可选"）
  const aWrap = a && a.closest(".quick-chip");
  if (aWrap) aWrap.style.opacity = s.format === "fl2va" ? ".55" : "";
}

/* 按模式渲染长宽比：t2va 固定六档 / fl2va 跟随输入图片 / ref2va 六档 + 自动 */
function renderRatioRow() {
  const s = state.settings;
  const row = $("#ratioRow");
  if (s.format === "fl2va") {
    row.innerHTML = `<span class="gs-note">遵循输入图片的原始长宽比（未上传图片时按 ${s.aspect} 生成）</span>`;
    return;
  }
  const opts = s.format === "ref2va" ? [...state.ratios, "auto"] : state.ratios;
  if (!opts.includes(s.aspect)) s.aspect = "16:9";
  row.innerHTML = opts
    .map((r) => `
      <button type="button" class="gs-ratio ${r === s.aspect ? "on" : ""}" data-ratio="${r}">${r === "auto" ? "自动" : r}</button>`)
    .join("");
}

/* 切换模式后移除不支持类型的附件；若超出新模式上限也一并截断（保留前 N 个） */
function pruneFilesForFormat(fmt) {
  const rule = MODE_RULES[fmt];
  if (!rule) return;
  const before = state.files.length;
  state.files = state.files.filter((f) => rule.types.includes(fileKindOf(f)));
  const cap = rule.maxFiles || MAX_FILES;
  if (state.files.length > cap) state.files = state.files.slice(0, cap);
  const removed = before - state.files.length;
  if (!removed) return;
  for (const f of [...state.probes.keys()])
    if (!state.files.includes(f)) state.probes.delete(f);
  refreshAttachSlots();
  toast(`已切换到${rule.label}，移除 ${removed} 个附件`);
}

/* 默认 API URL：随生成模式自动填充（仅当当前为空或上一模式默认值时才覆盖，避免覆盖用户手填值） */
const API_URL_DEFAULTS = {
  t2va: "http://10.124.35.7:30010/v1/videos",
  fl2va: "http://10.124.35.8:30011/v1/videos",
  ref2va: "http://10.124.32.3:30020/v1/videos",
};
function syncDefaultApiUrl(fmt) {
  const urlInp = $("#apiUrlInput");
  const def = API_URL_DEFAULTS[fmt];
  if (!urlInp || !def) return;
  const allDefaults = new Set(Object.values(API_URL_DEFAULTS));
  const cur = state.settings.apiUrl || "";
  if (!cur || allDefaults.has(cur)) {
    state.settings.apiUrl = def;
    urlInp.value = def;
    showKeyMsg(null);
  }
}

/* 附件入口的提示文案随模式更新（按钮与隐藏 input 已在重构中移除） */
function updateAttachHint() {
  // 槽位内的提示文案已通过 SLOT_PLUS_SVG / .slot-label 体现；
  // 这里仅做占位以保留调用点，必要时再补 textarea placeholder 等。
  return;
}

function renderFormatSeg() {
  $("#formatSeg").innerHTML = state.formats
    .map(
      (f) => `
      <button type="button" data-format="${esc(f.id)}" class="${f.id === state.settings.format ? "on" : ""}">
        <span class="gs-seg-name">${esc(f.id)}</span>
        <span class="gs-seg-desc">${esc(f.desc || "")}</span>
      </button>`
    )
    .join("");
}

function bindSettings() {
  const dd = $("#settingsDD");

  $("#settingsBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    dd.classList.toggle("open");
  });
  $("#settingsPanel").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => dd.classList.remove("open"));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") dd.classList.remove("open");
  });

  $("#formatSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-format]");
    if (!btn) return;
    applyFormat(btn.dataset.format);
  });

  $("#apiUrlInput").addEventListener("input", (e) => {
    state.settings.apiUrl = e.target.value.trim();
    showKeyMsg(null); // 接口已变化，此前的检测结果不再有效
  });

  $("#apiKeyInput").addEventListener("input", (e) => {
    state.settings.apiKey = e.target.value.trim();
    showKeyMsg(null);
  });

  $("#apiKeyCheckBtn").addEventListener("click", checkApiKey);

  $("#ratioRow").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ratio]");
    if (!btn) return;
    state.settings.aspect = btn.dataset.ratio;
    renderRatioRow();
    updateSettingsSummary();
    updateQuickChips();
  });

  $("#resSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-res]");
    if (!btn) return;
    state.settings.resolution = btn.dataset.res;
    $$("#resSeg button").forEach((b) => b.classList.toggle("on", b === btn));
    updateSettingsSummary();
    updateQuickChips();
  });

  $("#durationRange").addEventListener("input", (e) => {
    state.settings.duration = parseInt(e.target.value, 10);
    $("#durationVal").textContent = `${state.settings.duration} 秒`;
    updateSettingsSummary();
    updateQuickChips();
  });

  $("#settingsDone").addEventListener("click", () => dd.classList.remove("open"));

  bindQuickBtn();
}

/* 参数按钮（比例 / 分辨率 / 时长 折叠入口）：点 trigger 切换 panel，点击外部 / Esc 关闭 */
function bindQuickBtn() {
  const wrap = $("#quickBtnWrap");
  const trig = $("#quickBtn");
  const panel = $("#quickPanel");
  if (!wrap || !trig || !panel) return;

  function open() { panel.hidden = false; trig.classList.add("open"); trig.setAttribute("aria-expanded", "true"); }
  function close() { panel.hidden = true; trig.classList.remove("open"); trig.setAttribute("aria-expanded", "false"); }
  function isOpen() { return !panel.hidden; }

  trig.addEventListener("click", (e) => {
    e.stopPropagation();
    isOpen() ? close() : open();
  });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { if (isOpen()) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen()) close(); });
}

/* API Key 检测结果提示：kind 为 "ok"（通过）/ "bad"（失败），null 清除 */
function showKeyMsg(kind, text) {
  const el = $("#apiKeyMsg");
  el.hidden = !kind;
  el.className = `gs-key-msg ${kind || ""}`.trim();
  el.textContent = text || "";
}

/* 检测当前填写的 API Key 是否被模型接口接受（后端带 Bearer 头发空载荷
   探测请求：401/403 判定无效，其余状态码说明已通过鉴权层） */
async function checkApiKey() {
  const s = state.settings;
  if (!s.apiUrl) { toast("请先填写模型 API 接口地址"); return; }
  if (!s.apiKey) { toast("请输入 API Key"); return; }
  const btn = $("#apiKeyCheckBtn");
  btn.disabled = true;
  btn.textContent = "检测中…";
  const url = s.apiUrl, key = s.apiKey;
  // 期间改动过接口或 Key 则丢弃过期结果
  const stillCurrent = () =>
    state.settings.apiUrl === url && state.settings.apiKey === key;
  try {
    const r = await api("/api/check_key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_url: url, api_key: key }),
    });
    if (stillCurrent()) showKeyMsg(r.ok ? "ok" : "bad", r.message);
  } catch (err) {
    if (stillCurrent()) showKeyMsg("bad", err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "检测";
  }
}

/* ---------------- 生成 ---------------- */

function bindComposer() {
  const input = $("#promptInput");
  input.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") generate();
  });
  $("#generateBtn").addEventListener("click", generate);
}

/* 提交前对整套附件按当前模式做完整校验（覆盖切换模式后遗留的不合规附件），
   返回首个错误文案（null 表示通过） */
async function validateSelection() {
  const fmt = state.settings.format;
  const rule = MODE_RULES[fmt];
  if (!rule) return null; // t2va：仅通用个数/大小限制，添加时已校验
  const imgs = [], vids = [], auds = [];
  for (const f of state.files) {
    const k = fileKindOf(f);
    if (k === "image") imgs.push(f);
    else if (k === "video") vids.push(f);
    else if (k === "audio") auds.push(f);
  }
  if (state.files.length > (rule.maxFiles || MAX_FILES))
    return `${rule.label}附件最多 ${rule.maxFiles || MAX_FILES} 个`;
  if (imgs.length > rule.images) return `${rule.label}图片最多 ${rule.images} 张`;
  if (rule.videos !== undefined && vids.length > rule.videos)
    return `${rule.label}视频最多 ${rule.videos} 段`;
  if (rule.audios !== undefined && auds.length > rule.audios)
    return `${rule.label}音频最多 ${rule.audios} 段`;
  if (fmt === "ref2va" && auds.length && !(imgs.length || vids.length))
    return "音频需搭配图片或视频输入，不能单独使用";
  let vdur = 0, adur = 0;
  for (const f of state.files) {
    const p = await fileProbe(f);
    if (!p) return `无法读取文件信息：${f.name}`;
    const err = fileRuleError(f, fmt, p);
    if (err) return err;
    const k = fileKindOf(f);
    if (k === "video") vdur += p.dur;
    else if (k === "audio") adur += p.dur;
  }
  if (vdur > DUR_TOTAL_MAX + DUR_EPS)
    return `视频总时长需 ≤ ${DUR_TOTAL_MAX} 秒（当前 ${vdur.toFixed(1)} 秒）`;
  if (adur > DUR_TOTAL_MAX + DUR_EPS)
    return `音频总时长需 ≤ ${DUR_TOTAL_MAX} 秒（当前 ${adur.toFixed(1)} 秒）`;
  return null;
}

async function generate() {
  const input = $("#promptInput");
  const prompt = input.value.trim();
  if (!prompt && !state.files.length) {
    const box = $("#promptBox");
    box.classList.remove("shake");
    void box.offsetWidth;
    box.classList.add("shake");
    input.focus();
    toast("请输入提示词或添加图片 / 视频 / 音频附件");
    return;
  }

  const ruleErr = await validateSelection();
  if (ruleErr) {
    toast(ruleErr, 3600);
    return;
  }

  const s = state.settings;
  const btn = $("#generateBtn");
  btn.disabled = true;
  btn.style.opacity = ".6";
  try {
    const fd = new FormData();
    fd.append("prompt", prompt);
    fd.append("format", s.format);
    fd.append("api_url", s.apiUrl);
    fd.append("api_key", s.apiKey);
    fd.append("aspect", s.aspect);
    fd.append("resolution", s.resolution);
    fd.append("duration", s.duration);
    for (const f of state.files) fd.append("files", f, f.name);

    const { task_id } = await postForm("/api/generate", fd);
    state.tasks.set(task_id, {
      status: "queued", progress: 0, message: "已提交",
      prompt: prompt || "(仅附件)", fmt: s.format,
    });
    setMode("workspace");
    renderList();
    updateEmptyProgress();
    startPolling();
  } catch (err) {
    toast(`提交失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.style.opacity = "";
  }
}

/* ---------------- 任务轮询 ---------------- */

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(pollTick, 600);
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollTick() {
  const ids = [...state.tasks.keys()];
  if (!ids.length) {
    stopPolling();
    return;
  }
  let structuralChange = false;
  await Promise.all(
    ids.map(async (id) => {
      let t;
      try {
        t = await api(`/api/tasks/${id}`);
      } catch {
        return; // 网络抖动，下轮再试
      }
      const local = state.tasks.get(id);
      if (!local) return;
      Object.assign(local, t);
      if (t.status === "done") {
        state.tasks.delete(id);
        if (state.activeTaskId === id) state.activeTaskId = null;
        structuralChange = true;
        await refreshVideos();
        const v = state.videos.find((x) => x.id === t.video_id);
        if (v) {
          loadVideo(v, { autoplay: true });
          toast("生成完成");
          setTimeout(() => {
            const el = $(`.v-item[data-id="${v.id}"]`);
            if (el) {
              el.classList.add("pulse");
              setTimeout(() => el.classList.remove("pulse"), 1700);
            }
          }, 60);
        } else {
          updateEmptyProgress();
        }
      } else if (t.status === "error") {
        state.tasks.delete(id);
        if (state.activeTaskId === id) state.activeTaskId = null;
        structuralChange = true;
        toast(`生成失败：${t.error || "未知错误"}`, 3600);
        updateEmptyProgress();
      }
    })
  );
  if (structuralChange) renderList();
  else updateGeneratingItems();
  updateEmptyProgress();
}

function updateGeneratingItems() {
  for (const [id, t] of state.tasks) {
    const el = $(`.v-item[data-task="${id}"]`);
    if (!el) continue;
    const sub = el.querySelector(".v-sub");
    if (sub) sub.textContent = `${t.fmt || ""} · ${t.message || ""} · ${Math.round((t.progress || 0) * 100)}%`;
    const bar = el.querySelector(".v-prog i");
    if (bar) bar.style.width = `${Math.round((t.progress || 0) * 100)}%`;
  }
}

/* ---------------- 生成中占位界面 ---------------- */

function updateEmptyProgress() {
  const empty = $("#playerEmpty");
  if (state.currentId) return;
  // 优先显示点选的生成任务；未点选时默认显示最近提交的一条
  let t = null;
  if (state.activeTaskId && state.tasks.has(state.activeTaskId)) {
    t = state.tasks.get(state.activeTaskId);
  } else if (state.tasks.size) {
    t = [...state.tasks.values()][state.tasks.size - 1];
  }
  if (t) {
    $("#peText").textContent = "生成的视频将在这里播放";
    $("#peProgress").hidden = false;
    const pct = Math.round((t.progress || 0) * 100);
    $("#peBarFill").style.width = `${pct}%`;
    $("#pePct").textContent = `${pct}%`;
    $("#peMsg").hidden = false;
    $("#peMsg").textContent = `${t.fmt || "生成中"} · ${t.message || "排队中"}`;
  } else {
    $("#peText").textContent = "生成的视频将在这里播放";
    $("#peProgress").hidden = true;
    $("#peMsg").hidden = true;
  }
  empty.style.display = "";
}

/* ---------------- 视频列表 ---------------- */

async function refreshVideos() {
  const data = await api("/api/videos");
  state.videos = data.videos || [];
}

function renderList() {
  const list = $("#libraryList");
  const parts = [];

  for (const [id, t] of state.tasks) {
    parts.push(`
      <li class="v-item ${id === state.activeTaskId ? "active" : ""}" data-task="${esc(id)}">
        <div class="v-spin"><i></i></div>
        <div class="v-info">
          <p class="v-prompt">${esc(t.prompt)}</p>
          <p class="v-sub">${esc(t.fmt || "")} · ${esc(t.message || "排队中")} · ${Math.round((t.progress || 0) * 100)}%</p>
          <div class="v-prog"><i style="width:${Math.round((t.progress || 0) * 100)}%"></i></div>
        </div>
      </li>`);
  }

  for (const v of state.videos) {
    parts.push(`
      <li class="v-item ${v.id === state.currentId ? "active" : ""} ${state.selected.has(v.id) ? "checked" : ""}" data-id="${esc(v.id)}">
        <span class="v-check" data-check title="选择">
          <svg viewBox="0 0 24 24" width="11" height="11"><path d="M20 6.5 9 17.5l-5-5" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <div class="v-thumb">
          <video src="/api/videos/${esc(v.id)}/file#t=0.1" preload="metadata" muted playsinline></video>
          <span class="v-dur">${fmtTime(v.duration)}</span>
          ${v.kind === "trim" ? '<span class="v-badge">裁剪</span>' : v.kind === "merge" ? '<span class="v-badge">拼接</span>' : ""}
        </div>
        <div class="v-info">
          <p class="v-prompt">${esc(v.prompt)}</p>
          <p class="v-sub">${esc(v.model_name)} · ${v.width}×${v.height} · ${new Date(v.created_at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}${v.inputs && v.inputs.length ? ` · 附件 ${v.inputs.length}` : ""}</p>
        </div>
        <button class="v-del" data-del title="删除">
          <svg viewBox="0 0 24 24" width="14" height="14"><path d="M4 7h16M9.5 7V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2m3.5 0-.8 12.1a2 2 0 0 1-2 1.9H8.8a2 2 0 0 1-2-1.9L6 7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </li>`);
  }

  list.innerHTML = parts.length
    ? parts.join("")
    : `<div class="lib-empty">
         <svg viewBox="0 0 24 24" width="26" height="26"><rect x="3" y="5" width="18" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M10 9.5v5l4.5-2.5z" fill="currentColor"/></svg>
         暂无生成记录
       </div>`;

  $("#libCount").textContent = state.videos.length
    ? `· ${state.videos.length}${state.tasks.size ? `+${state.tasks.size}` : ""}`
    : state.tasks.size ? `· ${state.tasks.size} 生成中` : "";
  updateDownloadBtn();
}

function bindLibrary() {
  $("#libraryList").addEventListener("click", async (e) => {
    const item = e.target.closest(".v-item");
    if (!item) return;

    if (e.target.closest("[data-check]")) {
      const id = item.dataset.id;
      if (!id) return;
      if (state.selected.has(id)) {
        state.selected.delete(id);
        item.classList.remove("checked");
      } else {
        state.selected.add(id);
        item.classList.add("checked");
      }
      updateDownloadBtn();
      return;
    }

    if (e.target.closest("[data-del]")) {
      const id = item.dataset.id;
      if (!id) return;
      try {
        await api(`/api/videos/${id}`, { method: "DELETE" });
        state.videos = state.videos.filter((v) => v.id !== id);
        state.selected.delete(id);
        if (state.currentId === id) clearPlayer();
        renderList();
        toast("已删除");
      } catch (err) {
        toast(`删除失败：${err.message}`);
      }
      return;
    }

    const taskId = item.dataset.task;
    if (taskId) {
      selectTask(taskId);
      return;
    }

    const id = item.dataset.id;
    if (id) {
      const v = state.videos.find((x) => x.id === id);
      if (v) loadVideo(v, { autoplay: true });
    }
  });

  $("#selectAllBtn").addEventListener("click", () => {
    const allSelected = state.videos.length && state.videos.every((v) => state.selected.has(v.id));
    state.selected = allSelected ? new Set() : new Set(state.videos.map((v) => v.id));
    renderList();
  });

  $("#downloadBtn").addEventListener("click", () => {
    if (!state.selected.size) return;
    const a = document.createElement("a");
    a.href = `/api/download?ids=${encodeURIComponent([...state.selected].join(","))}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(`开始下载 ${state.selected.size} 个视频（ZIP）`);
  });
}

function updateDownloadBtn() {
  const btn = $("#downloadBtn");
  const n = state.selected.size;
  btn.disabled = n === 0;
  $("#downloadLabel").textContent = n ? `批量下载 ${n}` : "批量下载";
}

/* 点选生成中的任务：播放器切换为黑色「生成的视频将在这里播放」
   + 生成进度条界面（生成完毕后自动清除并播放视频） */
function selectTask(taskId) {
  if (!state.tasks.has(taskId)) return;
  if (state.currentId) clearPlayer();
  state.activeTaskId = taskId;
  renderList();
  updateEmptyProgress();
}

/* ---------------- 播放器 ---------------- */

function loadVideo(meta, { autoplay = true } = {}) {
  state.currentId = meta.id;
  state.activeTaskId = null; // 播放器离开「生成中」占位界面
  exitTrim(true);
  videoEl.src = `/api/videos/${meta.id}/file`;
  $("#playerEmpty").style.display = "none";
  $("#peProgress").hidden = true; // 复位占位进度条与状态文案
  $("#peMsg").hidden = true;
  renderPlayerMeta(meta);

  $$(".v-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.id === meta.id));

  if (autoplay) videoEl.play().catch(() => {});
  syncPlayState();
}

function clearPlayer() {
  state.currentId = null;
  exitTrim(true);
  videoEl.pause();
  videoEl.removeAttribute("src");
  videoEl.load();
  $("#playerEmpty").style.display = "";
  $("#playOverlay").hidden = true;
  $("#timeLabel").textContent = "0:00 / 0:00";
  $("#seekPlayed").style.width = "0";
  $("#seekHead").style.left = "0";
  $("#pmModel").hidden = true;
  $("#pmSpec").hidden = true;
  $("#pmPrompt").textContent = "尚未生成视频";
  $$(".v-item").forEach((el) => el.classList.remove("active"));
  updateEmptyProgress();
}

function renderPlayerMeta(v) {
  $("#pmModel").hidden = false;
  $("#pmModel").textContent = v.kind === "trim" ? `${v.model_name} · 裁剪片段` : v.model_name;
  $("#pmPrompt").textContent = v.prompt;
  $("#pmSpec").hidden = false;
  $("#pmSpec").textContent = `${v.width}×${v.height} · ${fmtTime(v.duration)} · ${fmtBytes(v.size)}`;
}

function duration() {
  const d = videoEl.duration;
  return isFinite(d) ? d : 0;
}

function syncPlayState() {
  const paused = videoEl.paused || videoEl.ended;
  $("#playBtn .ic-play").classList.toggle("show", paused);
  $("#playBtn .ic-pause").classList.toggle("show", !paused);
  playerCard.classList.toggle("paused", paused);
  const hasSrc = !!videoEl.getAttribute("src");
  $("#playOverlay").hidden = !(paused && hasSrc);
}

function togglePlay() {
  if (!videoEl.getAttribute("src")) return;
  if (videoEl.paused) videoEl.play().catch(() => {});
  else videoEl.pause();
}

function bindPlayer() {
  videoEl.addEventListener("play", syncPlayState);
  videoEl.addEventListener("pause", syncPlayState);
  videoEl.addEventListener("ended", syncPlayState);
  videoEl.addEventListener("error", () => {
    if (videoEl.getAttribute("src")) toast("视频加载失败");
  });

  videoEl.addEventListener("loadedmetadata", () => {
    $("#timeLabel").textContent = `0:00 / ${fmtTime(duration())}`;
  });

  videoEl.addEventListener("timeupdate", () => {
    const d = duration();
    const t = videoEl.currentTime || 0;
    const f = d ? (t / d) * 100 : 0;
    $("#seekPlayed").style.width = `${f}%`;
    $("#seekHead").style.left = `${f}%`;
    $("#timeLabel").textContent = `${fmtTime(t)} / ${fmtTime(d)}`;
    if (state.trim.previewing && state.trim.on && t >= state.trim.end - 0.03) {
      videoEl.currentTime = state.trim.start;
    }
  });

  $("#playBtn").addEventListener("click", togglePlay);
  $("#playOverlay").addEventListener("click", () => videoEl.play().catch(() => {}));
  $("#playerSurface").addEventListener("click", (e) => {
    if (e.target === videoEl) togglePlay();
  });

  $("#muteBtn").addEventListener("click", () => {
    videoEl.muted = !videoEl.muted;
    syncMuteIcon();
  });

  $("#fsBtn").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else playerCard.requestFullscreen().catch(() => {});
  });

  // 键盘：空格播放暂停，左右方向键 ±1s
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "textarea" || tag === "input" || e.target.isContentEditable) return;
    if (e.code === "Space" && videoEl.getAttribute("src")) {
      e.preventDefault();
      togglePlay();
    } else if (e.key === "ArrowRight" && videoEl.getAttribute("src")) {
      videoEl.currentTime = Math.min(duration(), videoEl.currentTime + 1);
    } else if (e.key === "ArrowLeft" && videoEl.getAttribute("src")) {
      videoEl.currentTime = Math.max(0, videoEl.currentTime - 1);
    }
  });
}

function syncMuteIcon() {
  $("#muteBtn .ic-vol").classList.toggle("show", !videoEl.muted);
  $("#muteBtn .ic-muted").classList.toggle("show", videoEl.muted);
}

/* ---------------- 进度条 / 裁剪 ---------------- */

function timeFromX(clientX) {
  const rect = $("#seekTrack").getBoundingClientRect();
  const f = (clientX - rect.left) / rect.width;
  return Math.max(0, Math.min(1, f)) * duration();
}

function showTip(x, text) {
  const tip = $("#seekTip");
  const rect = $("#seek").getBoundingClientRect();
  tip.hidden = false;
  tip.textContent = text;
  tip.style.left = `${Math.max(18, Math.min(rect.width - 18, x - rect.left))}px`;
}

function hideTip() {
  $("#seekTip").hidden = true;
}

function renderTrim() {
  const d = duration() || 1;
  const { start, end } = state.trim;
  $("#trimRange").style.left = `${(start / d) * 100}%`;
  $("#trimRange").style.width = `${((end - start) / d) * 100}%`;
  $("#handleL").style.left = `${(start / d) * 100}%`;
  $("#handleR").style.left = `${(end / d) * 100}%`;
  $("#trimTimes").textContent =
    `${start.toFixed(1)}s – ${end.toFixed(1)}s · ${(end - start).toFixed(1)}s`;
}

function bindSeek() {
  const seek = $("#seek");
  let scrubbing = false;

  seek.addEventListener("pointerdown", (e) => {
    if (!duration()) return;
    if (e.target.closest(".seek-handle")) return; // 手柄有自己的拖拽逻辑
    scrubbing = true;
    seek.setPointerCapture(e.pointerId);
    videoEl.currentTime = timeFromX(e.clientX);
    showTip(e.clientX, fmtTime(videoEl.currentTime));
  });
  seek.addEventListener("pointermove", (e) => {
    if (scrubbing) {
      videoEl.currentTime = timeFromX(e.clientX);
      showTip(e.clientX, fmtTime(videoEl.currentTime));
    }
  });
  seek.addEventListener("pointerup", () => {
    scrubbing = false;
    hideTip();
  });
  seek.addEventListener("pointerleave", () => {
    if (!scrubbing) hideTip();
  });
}

function bindTrimHandles() {
  const bind = (el, which) => {
    el.addEventListener("pointerdown", (e) => {
      if (!duration()) return;
      e.stopPropagation();
      state.trim.dragging = which;
      el.classList.add("dragging");
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener("pointermove", (e) => {
      if (state.trim.dragging !== which) return;
      const t = timeFromX(e.clientX);
      const d = duration();
      if (which === "l") {
        state.trim.start = Math.max(0, Math.min(t, state.trim.end - 0.2));
      } else {
        state.trim.end = Math.min(d, Math.max(t, state.trim.start + 0.2));
      }
      renderTrim();
      showTip(e.clientX, which === "l" ? state.trim.start.toFixed(1) + "s" : state.trim.end.toFixed(1) + "s");
    });
    const end = () => {
      state.trim.dragging = null;
      el.classList.remove("dragging");
      hideTip();
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  };
  bind($("#handleL"), "l");
  bind($("#handleR"), "r");
}

function enterTrim() {
  if (!videoEl.getAttribute("src")) {
    toast("请先生成或选择一个视频");
    return;
  }
  state.trim.on = true;
  state.trim.previewing = false;
  state.trim.start = 0;
  state.trim.end = duration();
  playerCard.classList.add("trimming");
  $("#trimBtn").classList.add("on");
  renderTrim();
}

function exitTrim(silent = false) {
  state.trim.on = false;
  state.trim.previewing = false;
  playerCard.classList.remove("trimming");
  $("#trimBtn").classList.remove("on");
  $("#previewTrimBtn").classList.remove("primary");
  if (!silent && !videoEl.getAttribute("src")) toast("已退出裁剪");
}

function bindTrimPanel() {
  $("#trimBtn").addEventListener("click", () => {
    if (state.trim.on) exitTrim();
    else enterTrim();
  });
  $("#closeTrimBtn").addEventListener("click", () => exitTrim());

  $("#markStartBtn").addEventListener("click", () => {
    state.trim.start = Math.max(0, Math.min(videoEl.currentTime, state.trim.end - 0.2));
    renderTrim();
  });
  $("#markEndBtn").addEventListener("click", () => {
    state.trim.end = Math.min(duration(), Math.max(videoEl.currentTime, state.trim.start + 0.2));
    renderTrim();
  });

  $("#previewTrimBtn").addEventListener("click", () => {
    if (!state.trim.on) return;
    state.trim.previewing = !state.trim.previewing;
    $("#previewTrimBtn").classList.toggle("primary", state.trim.previewing);
    if (state.trim.previewing) {
      videoEl.currentTime = state.trim.start;
      videoEl.play().catch(() => {});
    }
  });

  $("#exportTrimBtn").addEventListener("click", async () => {
    if (!state.trim.on || state.exporting) return;
    const { start, end } = state.trim;
    if (end - start < 0.2) {
      toast("裁剪区间至少 0.2 秒");
      return;
    }
    state.exporting = true;
    const btn = $("#exportTrimBtn");
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.innerHTML = "导出中…";
    try {
      const meta = await api(`/api/videos/${state.currentId}/trim`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start, end }),
      });
      await refreshVideos();
      renderList();
      const el = $(`.v-item[data-id="${meta.id}"]`);
      if (el) {
        el.classList.add("pulse");
        setTimeout(() => el.classList.remove("pulse"), 1700);
      }
      toast(`已导出 ${(end - start).toFixed(1)}s 片段至右侧列表`);
    } catch (err) {
      toast(`导出失败：${err.message}`);
    } finally {
      state.exporting = false;
      btn.disabled = false;
      btn.innerHTML = label;
    }
  });
}

/* ---------------- 视频拼接弹窗 ---------------- */

/* 双缓冲连播：两个 video 交替展示，空闲的一个提前加载下一段并停在起点，
   段末切换即时完成，避免单元素换源造成的黑场卡顿 */
const mmVideos = [$("#mmVideoA"), $("#mmVideoB")];
const mmLoaded = [-1, -1];   // 各元素当前已加载的片段序号
let mmCur = 0;                // 当前展示/播放的元素下标
let mmEpoch = 0;              // 装载代号，防止并发装载时的过期回调
const mmEl = () => mmVideos[mmCur];
const merge = {
  segs: [],        // {id, meta, start, end}
  active: 0,
  dragIdx: null,
  saving: false,
};

const mmSegDur = (s) => s.end - s.start;
const mmTotalDur = () => merge.segs.reduce((a, s) => a + mmSegDur(s), 0);

function openMerge() {
  const metas = [...state.selected]
    .map((id) => state.videos.find((v) => v.id === id))
    .filter(Boolean);
  if (metas.length < 2) {
    toast("请先在右侧勾选至少 2 个视频");
    return;
  }
  merge.segs = metas.map((m) => ({ id: m.id, meta: m, start: 0, end: m.duration || 0 }));
  merge.active = 0;
  $("#mergeModal").hidden = false;
  mmLoadSeg(0);
}

function mmTeardown() {
  mmVideos.forEach((el) => {
    el.pause();
    el.removeAttribute("src");
    el.load();
  });
  mmLoaded[0] = mmLoaded[1] = -1;
}

function closeMerge() {
  if (merge.saving) return;
  $("#mergeModal").hidden = true;
  mmTeardown();
  merge.segs = [];
  merge.active = 0;
}

function mmShow(slot) {
  if (slot !== mmCur) mmVideos[mmCur].pause();  // 换下的元素停播，避免声音残留
  mmCur = slot;
  mmVideos[0].classList.toggle("off", slot !== 0);
  mmVideos[1].classList.toggle("off", slot !== 1);
}

/* 段末连播等非手势上下文会被自动播放策略挂起（play() 长时间处于待定、
   既不成功也不拒绝）：先以静音起播（不受策略限制），起播后恢复原声音 */
function mmPlay(el) {
  const m = el.muted;
  el.muted = true;
  el.play().then(() => { el.muted = m; }).catch(() => { el.muted = m; });
}

/* 把第 i 段装载到指定元素并就位到 target（元数据与 seek 就绪后 resolve） */
function mmPrepare(el, slot, i, target) {
  return new Promise((resolve) => {
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    const timer = setTimeout(done, 4000);  // 兜底：加载异常时不悬挂
    const settle = () => { clearTimeout(timer); done(); };
    const onMeta = () => {
      if (Math.abs(el.currentTime - target) > 0.02) {
        el.addEventListener("seeked", settle, { once: true });
        el.currentTime = target;
      } else {
        settle();
      }
    };
    if (mmLoaded[slot] === i && el.readyState >= 1) {
      onMeta();
      return;
    }
    el.addEventListener("loadedmetadata", onMeta, { once: true });
    mmLoaded[slot] = i;
    el.src = `/api/videos/${merge.segs[i].id}/file`;
    el.load();
  });
}

/* 空闲元素提前加载下一段并停在起点，保证段末切换无黑场 */
function mmPreloadNext() {
  const next = merge.active + 1;
  if (next >= merge.segs.length) return;
  const slot = 1 - mmCur;
  if (mmLoaded[slot] === next) return;
  mmVideos[slot].pause();
  mmPrepare(mmVideos[slot], slot, next, merge.segs[next].start);
}

/* 装载并展示第 i 段：spare 已预载时即时切换；随后预载下一段 */
async function mmLoadSeg(i, { autoplay = false, seekTo = null } = {}) {
  const s = merge.segs[i];
  if (!s) return;
  const my = ++mmEpoch;
  merge.active = i;
  const target = seekTo !== null ? seekTo : s.start;
  let el = mmEl();
  if (mmLoaded[mmCur] !== i) {
    const slot = 1 - mmCur;
    const spare = mmVideos[slot];
    await mmPrepare(spare, slot, i, target);
    if (my !== mmEpoch) return;  // 已被更新的装载请求取代
    mmShow(slot);
    el = spare;
  } else if (Math.abs(el.currentTime - target) > 0.02) {
    el.currentTime = target;
  }
  if (autoplay) mmPlay(el);
  mmRenderTimeline();
  mmUpdateTotal();
  mmUpdateProgress();
  mmPreloadNext();
}

/* 全局时间轴位置：前面各段时长 + 当前段内偏移 */
function mmGlobalPos() {
  const s = merge.segs[merge.active];
  if (!s) return 0;
  let before = 0;
  for (let i = 0; i < merge.active; i++) before += mmSegDur(merge.segs[i]);
  return before + Math.max(0, Math.min(mmSegDur(s), mmEl().currentTime - s.start));
}

function mmUpdateProgress() {
  const total = mmTotalDur();
  const pos = mmGlobalPos();
  const f = total ? (pos / total) * 100 : 0;
  $("#mmPlayed").style.width = `${f}%`;
  $("#mmHead").style.left = `${f}%`;
  $("#mmTime").textContent = `${fmtTime(pos)} / ${fmtTime(total)}`;
}

function mmRenderMarks() {
  const total = mmTotalDur();
  const parts = [];
  let acc = 0;
  for (let i = 0; i < merge.segs.length - 1; i++) {
    acc += mmSegDur(merge.segs[i]);
    if (total > 0) parts.push(`<span class="mm-seg-mark" style="left:${(acc / total) * 100}%"></span>`);
  }
  $("#mmSegMarks").innerHTML = parts.join("");
}

function mmRenderTimeline() {
  $("#mmTimeline").innerHTML = merge.segs
    .map((s, i) => `
      <div class="mm-card ${i === merge.active ? "active" : ""}" data-idx="${i}" draggable="true">
        <span class="mm-idx">${i + 1}</span>
        <div class="mm-thumb">
          <video src="/api/videos/${esc(s.id)}/file#t=0.1" preload="metadata" muted playsinline></video>
          <span class="mm-dur">${mmSegDur(s).toFixed(1)}s</span>
        </div>
        <p class="mm-name" title="${esc(s.meta.prompt)}">${esc(s.meta.prompt)}</p>
      </div>`)
    .join("");
  mmRenderMarks();
  mmUpdateTotal();
}

function mmUpdateTotal() {
  $("#mmTotal").textContent = `共 ${merge.segs.length} 段 · 总长 ${mmTotalDur().toFixed(1)} 秒`;
  $("#mergeSaveBtn").disabled = merge.segs.length < 2 || merge.saving;
}

/* 按素材身份保持选中段不变 */
function mmMove(from, to) {
  if (to < 0 || to >= merge.segs.length || from === to) return;
  const activeSeg = merge.segs[merge.active];
  const [seg] = merge.segs.splice(from, 1);
  merge.segs.splice(to, 0, seg);
  merge.active = Math.max(0, merge.segs.indexOf(activeSeg));
  mmRenderTimeline();
  mmPreloadNext();  // 顺序变化后重载空闲元素的下一段预载
}

function mmSeekFromX(clientX) {
  const rect = $("#mmSeekTrack").getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const target = f * mmTotalDur();
  let acc = 0;
  for (let i = 0; i < merge.segs.length; i++) {
    const d = mmSegDur(merge.segs[i]);
    if (target <= acc + d || i === merge.segs.length - 1) {
      const offset = Math.max(0, Math.min(d, target - acc));
      if (i !== merge.active) {
        mmLoadSeg(i, { seekTo: merge.segs[i].start + offset });
      } else {
        mmEl().currentTime = merge.segs[i].start + offset;
      }
      mmUpdateProgress();
      return;
    }
    acc += d;
  }
}

function mmSyncPlayIcon() {
  const el = mmEl();
  const paused = el.paused || el.ended;
  $("#mmPlayBtn .ic-play").classList.toggle("show", paused);
  $("#mmPlayBtn .ic-pause").classList.toggle("show", !paused);
}

function bindMerge() {
  $("#mergeBtn").addEventListener("click", openMerge);
  $("#mergeCancelBtn").addEventListener("click", closeMerge);
  $("#mergeMask").addEventListener("click", closeMerge);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#mergeModal").hidden) closeMerge();
  });

  // 时间线：点击选中 / 拖拽排序
  $("#mmTimeline").addEventListener("click", (e) => {
    const card = e.target.closest(".mm-card");
    if (!card) return;
    const idx = parseInt(card.dataset.idx, 10);
    if (idx !== merge.active) mmLoadSeg(idx);
  });

  $("#mmTimeline").addEventListener("dragstart", (e) => {
    const card = e.target.closest(".mm-card");
    if (!card) return;
    merge.dragIdx = parseInt(card.dataset.idx, 10);
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", String(merge.dragIdx)); } catch { /* ignore */ }
  });
  $("#mmTimeline").addEventListener("dragend", (e) => {
    const card = e.target.closest(".mm-card");
    if (card) card.classList.remove("dragging");
    $$(".mm-card").forEach((c) => c.classList.remove("drop-target"));
    merge.dragIdx = null;
  });
  $("#mmTimeline").addEventListener("dragover", (e) => {
    const card = e.target.closest(".mm-card");
    if (!card || merge.dragIdx === null) return;
    e.preventDefault();
    $$(".mm-card").forEach((c) => c.classList.remove("drop-target"));
    card.classList.add("drop-target");
  });
  $("#mmTimeline").addEventListener("drop", (e) => {
    const card = e.target.closest(".mm-card");
    if (!card || merge.dragIdx === null) return;
    e.preventDefault();
    mmMove(merge.dragIdx, parseInt(card.dataset.idx, 10));
    merge.dragIdx = null;
  });

  // 预览播放器：播放控制 + 连播 + 全局进度
  $("#mmPlayBtn").addEventListener("click", () => {
    const el = mmEl();
    if (el.paused) el.play().catch(() => {});
    else el.pause();
  });
  mmVideos.forEach((el) => {
    el.addEventListener("play", mmSyncPlayIcon);
    el.addEventListener("pause", mmSyncPlayIcon);
    el.addEventListener("ended", mmSyncPlayIcon);
  });
  mmVideos.forEach((el) => el.addEventListener("timeupdate", () => {
    if (el !== mmEl()) return;
    const s = merge.segs[merge.active];
    if (!s) return;
    if (el.currentTime >= s.end - 0.03 && !el.seeking) {
      if (merge.active < merge.segs.length - 1) {
        // 自然播完时 paused 已为 true，但应视为仍在播放以保持连播
        mmLoadSeg(merge.active + 1, { autoplay: !el.paused || el.ended });
        return;
      }
      el.pause();
    }
    mmUpdateProgress();
  }));
  // 裁剪终点等于原片时长时，ended 可能先于 timeupdate 触发：同样进入下一段并保持连播
  mmVideos.forEach((el) => el.addEventListener("ended", () => {
    if (el !== mmEl()) return;
    const s = merge.segs[merge.active];
    if (!s) return;
    if (merge.active < merge.segs.length - 1 && el.currentTime >= s.end - 0.1) {
      mmLoadSeg(merge.active + 1, { autoplay: true });
    }
  }));

  let mmScrubbing = false;
  $("#mmSeek").addEventListener("pointerdown", (e) => {
    if (!merge.segs.length) return;
    mmScrubbing = true;
    $("#mmSeek").setPointerCapture(e.pointerId);
    mmSeekFromX(e.clientX);
  });
  $("#mmSeek").addEventListener("pointermove", (e) => {
    if (mmScrubbing) mmSeekFromX(e.clientX);
  });
  $("#mmSeek").addEventListener("pointerup", () => { mmScrubbing = false; });

  // 保存：提交拼接任务，完成后由任务管线写入生成记录
  $("#mergeSaveBtn").addEventListener("click", async () => {
    if (merge.saving || merge.segs.length < 2) return;
    merge.saving = true;
    mmUpdateTotal();
    const btn = $("#mergeSaveBtn");
    const label = btn.textContent;
    btn.textContent = "提交中…";
    try {
      const body = {
        segments: merge.segs.map((s) => ({
          id: s.id, start: +s.start.toFixed(3), end: +s.end.toFixed(3),
        })),
      };
      const { task_id } = await api("/api/videos/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.tasks.set(task_id, {
        status: "queued", progress: 0, message: "已提交",
        prompt: "视频拼接", fmt: "拼接",
      });
      closeMergeForce();
      renderList();
      startPolling();
      toast("拼接任务已提交，完成后保存至生成记录");
    } catch (err) {
      toast(`提交失败：${err.message}`);
      merge.saving = false;
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  mmSyncPlayIcon();
}

function closeMergeForce() {
  $("#mergeModal").hidden = true;
  mmTeardown();
  merge.segs = [];
  merge.active = 0;
  merge.saving = false;
  // 复位提交按钮：否则成功提交一次后会一直显示“提交中…”
  const btn = $("#mergeSaveBtn");
  btn.disabled = false;
  btn.textContent = "保存至生成记录";
}

/* ---------------- 播放器尺寸拖拽 ---------------- */

function paneBounds() {
  const pane = $("#playerPane").getBoundingClientRect();
  return {
    maxW: Math.max(320, pane.width - 24),
    maxH: Math.max(220, pane.height - 82), // 预留下方 meta 信息与温馨提示
  };
}

function applySize(w, h) {
  const { maxW, maxH } = paneBounds();
  w = Math.round(Math.max(320, Math.min(w, maxW)));
  h = Math.round(Math.max(220, Math.min(h, maxH)));
  playerCard.style.width = `${w}px`;
  playerCard.style.height = `${h}px`;
  state.playerSize = [w, h];
}

function initPlayerSize() {
  if (state.playerSize) return;
  const { maxW, maxH } = paneBounds();
  let w = Math.min(maxW, 880);
  let h = (w * 9) / 16;
  if (h > maxH) {
    h = maxH;
    w = (h * 16) / 9;
  }
  applySize(w, h);
}

function bindResize() {
  const handle = $("#resizeHandle");
  let start = null;

  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    start = {
      x: e.clientX,
      y: e.clientY,
      w: playerCard.offsetWidth,
      h: playerCard.offsetHeight,
    };
    handle.setPointerCapture(e.pointerId);
    document.body.style.cursor = "nwse-resize";
    document.body.style.userSelect = "none";
  });
  handle.addEventListener("pointermove", (e) => {
    if (!start) return;
    applySize(start.w + (e.clientX - start.x), start.h + (e.clientY - start.y));
  });
  const end = () => {
    start = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);

  window.addEventListener("resize", () => {
    if (state.playerSize) applySize(state.playerSize[0], state.playerSize[1]);
  });
}

/* ---------------- 启动 ---------------- */

async function init() {
  bindNav();
  bindSettings();
  bindComposer();
  bindAttachments();
  bindModeSwitcher();
  bindLibrary();
  bindPlayer();
  bindSeek();
  bindTrimHandles();
  bindTrimPanel();
  bindMerge();
  bindResize();
  syncMuteIcon();
  videoEl.muted = false;

  try {
    const data = await api("/api/models");
    state.formats = data.formats || [];
    const d = data.defaults || {};
    state.ratios = d.ratios || state.ratios;
    state.settings = {
      format: d.format || "t2va",
      apiUrl: "",
      apiKey: "",
      aspect: d.aspect || "16:9",
      resolution: d.resolution || "720p",
      duration: d.duration || 8,
    };
    renderFormatSeg();
    renderRatioRow();
    $$("#resSeg button").forEach((b) =>
      b.classList.toggle("on", b.dataset.res === state.settings.resolution));
    $("#durationRange").value = state.settings.duration;
    $("#durationVal").textContent = `${state.settings.duration} 秒`;
    // 同步默认 API URL（按当前 format 填入）
    syncDefaultApiUrl(state.settings.format);
    updateSettingsSummary();
    updateQuickChips();
  } catch (err) {
    toast(`生成设置加载失败：${err.message}`, 4000);
  }
  // 槽位与模式切换下拉按当前 format 渲染一次（默认 t2va 时不显示）
  renderAttachSlots();
  refreshAttachSlots();
  renderModeSwitcher();
  updateAttachHint();

  try {
    await refreshVideos();
  } catch { /* ignore */ }
  renderList();

  // 已有历史视频则直接进入工作区
  if (state.videos.length) {
    setMode("workspace");
    loadVideo(state.videos[0], { autoplay: false });
  }
}

init();

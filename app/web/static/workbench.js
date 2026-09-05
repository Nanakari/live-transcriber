"use strict";
const $ = id => document.getElementById(id);
const state = {source: "url", items: [], selected: "", tab: "summary", activeJob: "", statuses: new Map(), running: false, status: null, readerRequest: 0, closing: false};
const SETTINGS = "liveTranscriber.settings.v2";
const KEY = "liveTranscriber.geminiKey.v2";
const clientId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const fields = ["device", "proxy", "analysisModel", "fallbackModel", "asrModel", "cookies", "language", "quality"];
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function storageGet(storage, key) { try { return storage.getItem(key); } catch { return null; } }
function storageSet(storage, key, value) { try { if (value) storage.setItem(key, value); else storage.removeItem(key); } catch { /* Storage may be disabled. */ } }
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无效响应（${response.status}）`); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `请求失败（${response.status}）`);
  return data;
}
const post = (path, payload = {}) => api(path, {method: "POST", body: JSON.stringify(payload)});
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error", error);
}
function safe(action) { return async (...args) => { try { await action(...args); } catch (error) { notice(error.message, true); } }; }
function settings(open) { $("settingsPanel").hidden = !open; $("settingsBtn").setAttribute("aria-expanded", String(open)); }
function hasKey() { return Boolean($("apiKey").value.trim() || state.status?.gemini_api_key_detected); }
function updateKeyHint() {
  $("setupHint").textContent = hasKey() ? "已配置 Gemini，可生成全部核心结果。" : "首次使用请在设置中填写 API Key，也可以先仅转写。";
  $("keyStatus").textContent = state.status?.gemini_api_key_detected ? "服务端已配置密钥；填写此处可为当前任务覆盖。" : "密钥默认仅保留在当前标签页；记住密钥会保存到本机浏览器。";
}
function saveSettings() {
  storageSet(localStorage, SETTINGS, JSON.stringify(Object.fromEntries(fields.map(id => [id, $(id).value]))));
  storageSet(sessionStorage, KEY, $("apiKey").value.trim());
  storageSet(localStorage, KEY, $("rememberKey").checked ? $("apiKey").value.trim() : null);
  updateKeyHint();
}
async function loadStatus(initial = false) {
  const status = state.status = await api("/api/status");
  if (initial) {
    const defaults = status.defaults || {};
    let saved = {};
    try { saved = JSON.parse(storageGet(localStorage, SETTINGS) || "{}"); } catch {}
    const values = {device: defaults.device || "auto", proxy: defaults.proxy || "", language: defaults.language || "auto", quality: defaults.quality || "fast", analysisModel: status.gemini_model, fallbackModel: status.gemini_fallback_model, ...saved};
    fields.forEach(id => { if (values[id] != null) $(id).value = values[id]; });
    $("characterProfile").checked = Boolean(defaults.features?.character_profile);
    const remembered = storageGet(localStorage, KEY);
    $("apiKey").value = storageGet(sessionStorage, KEY) || remembered || "";
    $("rememberKey").checked = Boolean(remembered);
  }
  $("browseBtn").hidden = !status.file_picker;
  $("environment").textContent = `${status.cuda ? "GPU 可用" : "CPU 模式"} · ${status.ffmpeg ? "ffmpeg 就绪" : "缺少 ffmpeg，请运行安装脚本"} · ${status.faster_whisper ? "Whisper 就绪" : "缺少 Whisper"} · ${status.yt_dlp ? "视频下载工具就绪" : "缺少 yt-dlp"}`;
  $("dataPath").textContent = `数据目录：${status.data_dir}`;
  $("modelPath").textContent = `模型缓存：${status.models_dir}`;
  updateKeyHint();
}
function selectSource(source) {
  state.source = source;
  for (const kind of ["url", "file"]) {
    $(`${kind}Pane`).hidden = kind !== source;
    $(`${kind}Tab`).setAttribute("aria-selected", String(kind === source));
  }
}
function taskOptions() {
  return {gemini_api_key: $("apiKey").value.trim(), analysis_model: $("analysisModel").value.trim(), analysis_fallback_model: $("fallbackModel").value.trim(), device: $("device").value, proxy: $("proxy").value.trim(), cookies: $("cookies").value.trim(), language: $("language").value, quality: $("quality").value, model: $("asrModel").value.trim(), character_profile: $("characterProfile").checked, summary: true, study_notes: true, resume: true};
}
function setRunning(running) {
  state.running = running;
  document.querySelectorAll("[data-heavy]").forEach(button => { button.disabled = running; });
  updateActions();
}
async function startJob(path, payload) {
  if (state.running) return;
  saveSettings(); setRunning(true); notice("");
  try {
    const job = await post(path, payload);
    state.activeJob = job.job_id;
    state.statuses.set(job.job_id, job.status);
    $("jobPanel").hidden = false;
    await loadJobs();
  } catch (error) { setRunning(false); throw error; }
}
async function startTask(transcribeOnly = false) {
  if (!transcribeOnly && !hasKey()) { settings(true); $("apiKey").focus(); throw new Error("请填写 Gemini API Key，或点击“仅转写”。"); }
  const input = state.source === "file" ? $("inputPath").value.trim() : "";
  const url = state.source === "url" ? $("url").value.trim() : "";
  if (!input && !url) throw new Error("请先输入视频链接或选择本地文件。");
  await startJob("/api/pipeline", {...taskOptions(), input, url, module_transcribe: true, module_analyze: !transcribeOnly, module_preview: false, download_start: $("downloadStart").value.trim(), download_end: $("downloadEnd").value.trim()});
}
const statusNames = {pending: "等待中", running: "处理中", stopping: "正在取消", stopped: "已取消", succeeded: "已完成", partial: "部分完成", failed: "失败"};
async function loadJobs() {
  const jobs = await api("/api/jobs");
  let changed = false;
  for (const job of jobs) {
    const previous = state.statuses.get(job.job_id);
    if (previous && previous !== job.status && !["pending", "running", "stopping"].includes(job.status)) changed = true;
    state.statuses.set(job.job_id, job.status);
  }
  setRunning(jobs.some(job => ["pending", "running", "stopping"].includes(job.status)));
  const job = jobs.find(job => job.job_id === state.activeJob) || jobs[0];
  if (job) {
    $("jobPanel").hidden = false;
    $("jobLabel").textContent = `${statusNames[job.status] || job.status} · ${job.progress?.label || ""}`;
    $("jobProgress").value = Math.min(100, Math.max(0, Number(job.progress?.percent || 0)));
    const elapsed = Math.round((job.elapsed_seconds || 0) / 60);
    $("jobDetail").textContent = [job.progress?.detail, elapsed ? `已用时 ${elapsed} 分钟` : "", job.analysis_status?.fallback_lines ? `${job.analysis_status.fallback_lines} 条翻译待补全` : ""].filter(Boolean).join(" · ");
    $("stopBtn").hidden = !["pending", "running", "stopping"].includes(job.status);
    $("stopBtn").disabled = job.status === "stopping";
    $("stopBtn").dataset.jobId = job.job_id;
    $("logText").textContent = (job.recent_logs || []).join("\n");
    $("jobWarning").hidden = !["failed", "partial"].includes(job.status);
    $("jobWarning").textContent = job.status === "partial" ? "部分翻译未完成，已有结果已保存。可从更多操作中重试。" : "任务未能完成。请展开日志查看原因；已有转写可在历史任务中继续分析。";
    if (job.status === "failed") $("logDetails").open = true;
  }
  if (changed) await loadFiles(true);
}
function selected() { return state.items.find(item => item.run_id === state.selected); }
async function loadFiles(selectLatest = false) {
  const data = await api("/api/files/recent");
  state.items = data.artifact_sets || [];
  if (selectLatest || !selected()) state.selected = state.items[0]?.run_id || "";
  renderHistory(); await renderResults();
}
function renderHistory() {
  $("historyCount").textContent = `(${state.items.length})`;
  const list = $("historyList"); list.replaceChildren();
  if (!state.items.length) { list.append(node("p", "暂无历史任务。", "hint")); return; }
  for (const item of state.items) {
    const button = node("button", null, "history-item"); button.type = "button";
    button.setAttribute("aria-current", String(item.run_id === state.selected));
    button.append(node("strong", item.label), node("small", item.modified?.replace("T", " ") || ""));
    button.addEventListener("click", safe(async () => { state.selected = item.run_id; state.tab = "summary"; renderHistory(); await renderResults(); }));
    list.append(button);
  }
}
function resultViews(item) {
  const files = item.analysis?.files || {};
  return [{key: "transcript", label: "转写", path: item.transcript?.path?.replace(/\.json$/i, ".md")}, {key: "translation", label: "翻译", path: files["bilingual.md"]?.path}, {key: "summary", label: "总结", path: files["video_summary.md"]?.path}, {key: "study", label: "学习笔记", path: files["study_notes.md"]?.path}, {key: "profile", label: "人物档案", path: files["character_profile.md"]?.path}].filter(view => view.path);
}
async function renderResults() {
  const item = selected();
  $("resultActions").hidden = !item; $("exportDetails").hidden = !item; $("resultTabs").replaceChildren();
  if (!item) { state.readerRequest++; $("reader").replaceChildren(node("p", "添加媒体开始处理，或从历史任务中打开已有结果。", "empty")); $("resultTitle").textContent = "处理完成后，结果会显示在这里。"; return; }
  $("resultTitle").textContent = item.label;
  const views = resultViews(item);
  if (!views.some(view => view.key === state.tab)) state.tab = views.some(view => view.key === "summary") ? "summary" : views[0]?.key;
  for (const view of views) {
    const button = node("button", view.label); button.type = "button"; button.id = `result-tab-${view.key}`;
    button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(view.key === state.tab)); button.setAttribute("aria-controls", "reader");
    button.addEventListener("click", safe(async () => { state.tab = view.key; await renderResults(); }));
    $("resultTabs").append(button);
  }
  renderExports(item); updateActions();
  const view = views.find(view => view.key === state.tab);
  if (view) { $("reader").setAttribute("aria-labelledby", `result-tab-${view.key}`); await readDocument(view.path); }
}
async function readDocument(path) {
  const request = ++state.readerRequest;
  $("reader").replaceChildren(node("p", "正在读取…", "hint"));
  const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
  if (request !== state.readerRequest) return;
  const reader = $("reader"); reader.replaceChildren();
  if (data.truncated) reader.append(node("p", "文档较长，当前显示末尾部分；导出可查看完整内容。", "warning"));
  if (!/\.md$/i.test(path)) { reader.append(node("pre", data.content)); return; }
  for (const line of data.content.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const heading = /^(#{1,3})\s+(.+)/.exec(line);
    if (heading) reader.append(node(`h${heading[1].length}`, heading[2]));
    else reader.append(node("p", line.replace(/\*\*/g, ""), line.startsWith(">") ? "md-note" : line.startsWith("-") ? "md-bullet" : ""));
  }
}
function renderExports(item) {
  const links = $("exportLinks"); links.replaceChildren();
  const all = node("a", "全部文档（ZIP）"); all.href = `/api/artifacts/${encodeURIComponent(item.run_id)}/export`; links.append(all);
  const files = {...item.analysis?.files};
  if (item.transcript) { files["原文字幕.srt"] = {path: item.transcript.path.replace(/\.json$/i, ".srt")}; files["转写数据.json"] = item.transcript; }
  const labels = {"translation_zh.srt": "中文字幕（SRT）", "bilingual.md": "双语稿", "video_summary.md": "总结", "study_notes.md": "学习笔记", "character_profile.md": "人物档案", "review.md": "复查清单", "analysis.json": "分析数据（JSON）"};
  for (const [name, file] of Object.entries(files)) {
    if (!file || name.endsWith(".log")) continue;
    const link = node("a", labels[name] || name); link.href = `/api/download?path=${encodeURIComponent(file.path)}`; links.append(link);
  }
}
function updateActions() {
  const item = selected(); if (!item) return;
  $("reanalyzeBtn").disabled = state.running || !item.transcript;
  $("addProfileBtn").disabled = state.running || !item.transcript;
  $("addProfileBtn").hidden = Boolean(item.analysis?.files?.["character_profile.md"]);
  $("previewBtn").disabled = state.running || !(item.audio || item.clean_audio) || !item.analysis?.files?.["translation_zh.srt"];
  $("deleteBtn").disabled = state.running || !item.deletable;
  $("playBtn").hidden = !item.preview?.files?.["live_preview.mp4"];
  $("storageHint").textContent = `文件占用约 ${((item.storage_bytes || 0) / 1024 / 1024).toFixed(1)} MB。重新分析会复用相同参数下成功的分段；人物档案采用独立缓存。`;
}
async function reanalyze(profile = null) {
  const item = selected(); if (!item?.transcript) return;
  if (!hasKey()) { settings(true); throw new Error("请先填写 Gemini API Key。"); }
  const options = taskOptions();
  await startJob("/api/analyze", {...options, input: item.transcript.path, model: options.analysis_model, fallback_model: options.analysis_fallback_model, character_profile: profile ?? Boolean(item.analysis?.files?.["character_profile.md"])});
}
$("settingsBtn").addEventListener("click", () => settings($("settingsPanel").hidden));
$("closeSettingsBtn").addEventListener("click", () => { saveSettings(); settings(false); });
fields.forEach(id => $(id).addEventListener("change", saveSettings));
["apiKey", "rememberKey"].forEach(id => $(id).addEventListener("input", saveSettings));
$("urlTab").addEventListener("click", () => selectSource("url"));
$("fileTab").addEventListener("click", () => selectSource("file"));
$("browseBtn").addEventListener("click", safe(async () => { const data = await api("/api/browse-file?kind=media"); if (data.path) $("inputPath").value = data.path; }));
$("taskForm").addEventListener("submit", safe(async event => { event.preventDefault(); await startTask(); }));
$("transcribeOnlyBtn").addEventListener("click", safe(() => startTask(true)));
$("stopBtn").addEventListener("click", safe(async () => { $("stopBtn").disabled = true; await post(`/api/jobs/${encodeURIComponent($("stopBtn").dataset.jobId)}/stop`); await loadJobs(); }));
$("refreshStatusBtn").addEventListener("click", safe(() => loadStatus()));
$("openDataBtn").addEventListener("click", safe(() => post("/api/open-folder", {path: state.status.data_dir})));
$("folderBtn").addEventListener("click", safe(() => post("/api/open-folder", {path: selected()?.group_path || selected()?.transcript?.path})));
$("reanalyzeBtn").addEventListener("click", safe(() => reanalyze()));
$("addProfileBtn").addEventListener("click", safe(() => reanalyze(true)));
$("previewBtn").addEventListener("click", safe(async () => { const item = selected(); await startJob("/api/preview", {audio: (item.audio || item.clean_audio).path, subtitle: item.analysis.files["translation_zh.srt"].path, artifact_run_id: item.run_id}); }));
$("playBtn").addEventListener("click", safe(() => { const files = selected().preview.files; return post("/api/open-potplayer", {video: files["live_preview.mp4"].path, subtitle: (files["live_preview.bilingual.srt"] || files["live_preview.zh.srt"])?.path}); }));
$("deleteBtn").addEventListener("click", safe(async () => {
  const item = selected();
  if (!item || !confirm(`删除“${item.label}”的处理结果、缓存音频和预览文件？原始输入文件不会删除。`)) return;
  await api(`/api/artifacts/${encodeURIComponent(item.run_id)}`, {method: "DELETE"}); await loadFiles();
}));
$("shutdownBtn").addEventListener("click", safe(async () => {
  if (state.running && !confirm("有任务正在运行，关闭服务会取消任务。继续关闭？")) return;
  state.closing = true;
  try { await post("/api/lifecycle/shutdown"); notice("服务已关闭，可以关闭此标签页。"); document.querySelectorAll("button").forEach(button => { button.disabled = true; }); }
  catch (error) { state.closing = false; throw error; }
}));
async function poll() {
  if (state.closing) return;
  try { await loadJobs(); } catch (error) { notice(`连接服务失败：${error.message}。请确认程序仍在运行。`, true); }
  if (!state.closing) setTimeout(poll, 2000);
}
async function heartbeat() {
  if (state.closing) return;
  try { await post("/api/lifecycle/heartbeat", {client_id: clientId}); } catch {}
  if (!state.closing) setTimeout(heartbeat, 5000);
}
window.addEventListener("pagehide", () => { navigator.sendBeacon?.("/api/lifecycle/close", new Blob([JSON.stringify({client_id: clientId})], {type: "application/json"})); });
safe(async () => { await loadStatus(true); await loadFiles(); await loadJobs(); heartbeat(); poll(); })();

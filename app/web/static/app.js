const state = {
  running: false,
  activeJobId: null,
  files: null,
  selectedArtifactRunId: "",
  currentPreviewPath: "",
  jobStatuses: new Map(),
  clientId: (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`),
};

const $ = (id) => document.getElementById(id);
const GEMINI_KEY_STORAGE = "liveJapaneseTranscriber.geminiApiKey";
const THEME_STORAGE = "mediaInsight.theme";
const QUALITY_DEFAULTS = {
  high: { model: "large-v3-turbo", compute_type: "int8_float16", beam_size: "5" },
  accurate: { model: "medium", compute_type: "int8_float16", beam_size: "8" },
  cpu_safe: { model: "medium", compute_type: "int8", beam_size: "5" },
  fast: { model: "small", compute_type: "int8_float16", beam_size: "5" },
};

function applyTheme(theme) {
  const selected = theme === "calm" ? "calm" : "fubuki";
  document.documentElement.dataset.theme = selected;
  localStorage.setItem(THEME_STORAGE, selected);
  if ($("themeSelect")) $("themeSelect").value = selected;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function startLifecycle() {
  const heartbeat = () => {
    api("/api/lifecycle/heartbeat", {
      method: "POST",
      body: JSON.stringify({ client_id: state.clientId }),
    }).catch(() => {});
  };
  heartbeat();
  setInterval(heartbeat, 5000);

  const closePayload = JSON.stringify({ client_id: state.clientId });
  window.addEventListener("pagehide", () => {
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/lifecycle/close", new Blob([closePayload], { type: "application/json" }));
    } else {
      fetch("/api/lifecycle/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: closePayload,
        keepalive: true,
      }).catch(() => {});
    }
  });
}

function formPayload(form) {
  const data = {};
  new FormData(form).forEach((value, key) => { data[key] = value; });
  for (const field of form.querySelectorAll("select:disabled[name], input:disabled[name]")) {
    if (field.value) data[field.name] = field.value;
  }
  for (const input of form.querySelectorAll('input[type="checkbox"]')) data[input.name] = input.checked;
  Object.keys(data).forEach((key) => { if (data[key] === "") delete data[key]; });
  return data;
}

function loadLocalSecrets() {
  for (const keyInput of [$("geminiApiKey"), $("pipelineGeminiApiKey")].filter(Boolean)) {
    keyInput.value = localStorage.getItem(GEMINI_KEY_STORAGE) || "";
    keyInput.addEventListener("change", () => {
      const value = keyInput.value.trim();
      if (value) localStorage.setItem(GEMINI_KEY_STORAGE, value);
      else localStorage.removeItem(GEMINI_KEY_STORAGE);
      for (const peer of [$("geminiApiKey"), $("pipelineGeminiApiKey")].filter(Boolean)) {
        if (peer !== keyInput) peer.value = value;
      }
    });
  }
}

function setRunning(value) {
  state.running = value;
  document.querySelectorAll("[data-heavy]").forEach((btn) => { btn.disabled = value; });
}

async function loadStatus() {
  const status = await api("/api/status");
  $("geminiModel").value = status.gemini_model || "";
  if ($("pipelineGeminiModel")) $("pipelineGeminiModel").value = status.gemini_model || "";
  const items = [
    ["环境", `${status.cuda ? "CUDA" : "CPU"} · ${status.faster_whisper ? "Whisper 就绪" : "Whisper 缺失"}`],
    ["Gemini", status.gemini_api_key_detected ? "Key 已检测" : "Key 未检测"],
    ["模型", status.gemini_model || "未设置"],
    ["工具", `${status.ffmpeg ? "ffmpeg" : "缺 ffmpeg"} · ${status.yt_dlp ? "yt-dlp" : "缺 yt-dlp"}`],
    ["默认转写", `ASR ${status.default_asr_profile || "-"}`],
  ];
  $("statusPanel").innerHTML = items.map(([name, value]) => `
    <div class="status-item"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(String(value || ""))}</span></div>
  `).join("");
}

async function loadFiles({ keepPreview = true } = {}) {
  state.files = await api("/api/files/recent");
  fillArtifactSets(state.files.artifact_sets || []);
  fillGroupedSelects();
  renderQuickResults({ updatePreview: !keepPreview });
}

function fillArtifactSets(items) {
  const select = $("artifactSetSelect");
  const previous = state.selectedArtifactRunId;
  select.innerHTML = `<option value="">自动选择最新文件组</option>` + items.map((item) => (
    `<option value="${escapeAttr(item.run_id)}" title="${escapeAttr(artifactOptionTitle(item))}">${escapeHtml(artifactOptionLabel(item))}</option>`
  )).join("");
  const next = items.some((item) => item.run_id === previous) ? previous : (items[0] ? items[0].run_id : "");
  state.selectedArtifactRunId = next;
  select.value = next;
}

function fillGroupedSelects() {
  const items = state.files?.artifact_sets || [];
  const transcriptFiles = items.filter((item) => item.transcript).map((item) => ({
    ...item.transcript,
    label: `${shortArtifactLabel(item)} / 转写稿`,
  }));
  const audioFiles = items.filter((item) => item.audio).map((item) => ({
    ...item.audio,
    label: `${shortArtifactLabel(item)} / 音频`,
  }));
  const subtitleFiles = items.filter((item) => item.analysis?.files?.["translation_zh.srt"]).map((item) => ({
    ...item.analysis.files["translation_zh.srt"],
    label: `${shortArtifactLabel(item)} / 中文字幕`,
  }));
  fillSelect("transcriptSelect", mergeFileOptions(transcriptFiles, (state.files.transcripts || []).filter((f) => f.name.endsWith("_transcript.json")), "未归组 transcript"));
  fillSelect("audioSelect", mergeFileOptions(audioFiles, state.files.audio || [], "未归组音频"));
  fillSelect("subtitleSelect", mergeFileOptions(subtitleFiles, state.files.translation_srt || [], "未归组字幕"));
  fillSelect("coverSelect", (state.files.covers || []).map((file) => ({ ...file, label: `封面 · ${file.name}` })));
}

function mergeFileOptions(primary, fallback, fallbackPrefix) {
  const seen = new Set(primary.map((file) => file.path));
  const extras = fallback.filter((file) => !seen.has(file.path)).map((file) => ({ ...file, label: `${fallbackPrefix} · ${file.name}` }));
  return [...primary, ...extras];
}

function fillSelect(id, files) {
  const select = $(id);
  const current = select.value;
  select.innerHTML = `<option value="">请选择</option>` + files.map((file) => (
    `<option value="${escapeAttr(file.path)}" title="${escapeAttr(file.path)}">${escapeHtml(file.label || file.name)}</option>`
  )).join("");
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}

function selectedArtifact() {
  const items = state.files?.artifact_sets || [];
  return items.find((item) => item.run_id === state.selectedArtifactRunId) || items[0] || null;
}

function setPathControl(id, path, label = null) {
  const el = $(id);
  if (!el) return;
  if (el.tagName === "SELECT") {
    if (![...el.options].some((option) => option.value === path)) el.add(new Option(label || fileNameFromPath(path), path), 1);
    el.value = path;
  } else {
    el.value = path;
  }
}

function renderQuickResults({ updatePreview = false } = {}) {
  const item = selectedArtifact();
  renderAnalysisQuick(item?.analysis || null, item);
  renderPreviewQuick(item?.preview || null, item);
  if (updatePreview && item) previewPreferredFile(item);
}

function previewPreferredFile(item) {
  const analysisFiles = item.analysis?.files || {};
  const previewFiles = item.preview?.files || {};
  const preferred = analysisFiles["character_profile.md"] || analysisFiles["video_summary.md"] || analysisFiles["study_notes.md"] || analysisFiles["bilingual.md"] || previewFiles["live_preview.bilingual.srt"] || analysisFiles["translation_zh.srt"] || item.transcript;
  if (preferred?.path) previewPath(preferred.path);
}

function renderAnalysisQuick(run, item = null) {
  const title = item ? shortArtifactLabel(item) : "当前文件组";
  if (!run) {
    $("analysisQuick").innerHTML = `<p>${escapeHtml(title)} 暂无研析结果。请先运行“翻译与学习分析”。</p>`;
    return;
  }
  const files = run.files || {};
  const primaryActions = [
    ["人物 Profile", files["character_profile.md"], "primary-action"],
    ["内容总结", files["video_summary.md"], "primary-action"],
    ["完整学习笔记", files["study_notes.md"] || files["bilingual.md"], "primary-action"],
    ["中文字幕", files["translation_zh.srt"], "secondary-action"],
    ["打开分析目录", { path: run.path, action: "folder" }, "secondary-action"],
  ];
  const extraActions = [
    ["双语稿", files["bilingual.md"]],
    ["生词表", files["vocabulary.md"]],
    ["语法笔记", files["grammar.md"]],
    ["复查清单", files["review.md"]],
  ].filter(([, file]) => file);
  $("analysisQuick").innerHTML = `
    <div class="result-meta"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(run.name || "")}</span></div>
    <div class="quick-actions">
      ${primaryActions.map(([label, target, cls]) => renderQuickAction(label, target, cls)).join("")}
    </div>
    ${extraActions.length ? `
      <details class="quick-more">
        <summary>更多文件</summary>
        <div class="quick-actions">
          ${extraActions.map(([label, file]) => renderQuickAction(label, file, "secondary-action")).join("")}
        </div>
      </details>
    ` : ""}
  `;
}

function renderPreviewQuick(run, item = null) {
  const title = item ? shortArtifactLabel(item) : "当前文件组";
  if (!run) {
    $("previewQuick").innerHTML = `<p>${escapeHtml(title)} 暂无媒体预览包。请先生成 PotPlayer 预览。</p>`;
    return;
  }
  const files = run.files || {};
  const video = files["live_preview.mp4"];
  const study = files["live_preview.study.srt"];
  const bilingual = files["live_preview.bilingual.srt"];
  const zh = files["live_preview.zh.srt"];
  const ja = files["live_preview.ja.srt"];
  const readme = files["README_play.txt"];
  const primaryActions = [
    video ? ["播放预览视频", { path: video.path, subtitle: (bilingual || study || zh)?.path || "", action: "potplayer" }, "primary-action"] : null,
    [bilingual ? "查看双语字幕" : "查看中文字幕", bilingual || zh, "secondary-action"],
    ["打开预览目录", { path: run.path, action: "folder" }, "secondary-action"],
  ].filter(Boolean);
  const extraActions = [
    ["学习字幕", study],
    ["原文字幕", ja],
    ["播放说明", readme],
  ].filter(([, file]) => file);
  $("previewQuick").innerHTML = `
    <div class="result-meta"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(run.name || "")}</span></div>
    <div class="quick-actions">
      ${primaryActions.map(([label, target, cls]) => renderQuickAction(label, target, cls)).join("")}
    </div>
    ${extraActions.length ? `
      <details class="quick-more">
        <summary>更多文件</summary>
        <div class="quick-actions">
          ${extraActions.map(([label, file]) => renderQuickAction(label, file, "secondary-action")).join("")}
        </div>
      </details>
    ` : ""}
  `;
}

function renderQuickAction(label, target, cls) {
  if (!target) return "";
  if (target.action === "folder") {
    return `<button class="${escapeAttr(cls)}" type="button" onclick="openFolder('${escapeAttr(target.path)}', this)">${escapeHtml(label)}</button>`;
  }
  if (target.action === "potplayer") {
    return `<button class="${escapeAttr(cls)}" type="button" onclick="openPotPlayer('${escapeAttr(target.path)}', '${escapeAttr(target.subtitle || "")}', this)">${escapeHtml(label)}</button>`;
  }
  return `<button class="${escapeAttr(cls)}" type="button" onclick="previewPath('${escapeAttr(target.path)}', this)">${escapeHtml(label)}</button>`;
}

function statusLabel(status) {
  return { pending: "等待中", running: "运行中", succeeded: "已完成", failed: "失败", stopped: "已停止" }[status] || status;
}

function moduleLabel(module) {
  return { pipeline: "完整处理", transcribe: "语音转写", analyze: "翻译与学习分析", preview: "媒体预览" }[module] || module;
}

async function startJob(path, payload) {
  try {
    const job = await api(path, { method: "POST", body: JSON.stringify(payload) });
    state.activeJobId = job.job_id;
    setRunning(true);
    await loadJobs();
  } catch (err) {
    alert(err.message);
  }
}

async function loadJobs() {
  const jobs = await api("/api/jobs");
  const anyRunning = jobs.some((job) => job.status === "running" || job.status === "pending");
  const completedNow = jobs.some((job) => {
    const previous = state.jobStatuses.get(job.job_id);
    state.jobStatuses.set(job.job_id, job.status);
    return previous && previous !== job.status && job.status === "succeeded";
  });
  setRunning(anyRunning);
  const active = jobs.find((job) => job.job_id === state.activeJobId) || jobs[0];
  const visibleJobs = active ? [active] : [];
  $("jobs").innerHTML = visibleJobs.map((job) => `
    <div class="job ${escapeAttr(job.status)}">
      <div class="job-title">
        <strong>${escapeHtml(moduleLabel(job.module))}</strong>
        <span class="badge ${escapeAttr(job.status)}">${escapeHtml(statusLabel(job.status))}</span>
      </div>
      ${renderJobProgress(job)}
      ${job.error ? `<div class="job-meta path-text">错误: ${escapeHtml(job.error)}</div>` : ""}
      <div class="job-actions">
        <button type="button" onclick="showJobLog('${escapeAttr(job.job_id)}', true)">日志</button>
        ${job.status === "running" ? `<button type="button" class="stop-btn" onclick="stopJob('${escapeAttr(job.job_id)}')">停止</button>` : ""}
      </div>
      ${(job.output_dir || job.started_at || job.finished_at || job.pid) ? `
        <details class="job-details">
          <summary>更多信息</summary>
          <div class="job-meta">ID: ${escapeHtml(job.job_id)}${job.pid ? ` · PID: ${escapeHtml(String(job.pid))}` : ""}</div>
          <div class="job-meta">开始: ${escapeHtml(job.started_at || "-")} · 结束: ${escapeHtml(job.finished_at || "-")}</div>
          ${job.output_dir ? `<div class="job-meta path-text">输出: ${escapeHtml(job.output_dir)}</div>` : ""}
        </details>
      ` : ""}
    </div>
  `).join("") || "<p>暂无任务</p>";
  if (active && !$("logPanel").classList.contains("collapsed")) await showJobLog(active.job_id, false);
  if (completedNow) await loadFiles({ keepPreview: false });
}

function renderJobProgress(job) {
  const progress = job.progress || {};
  const rawPercent = Number(progress.percent || 0);
  const percent = Math.max(0, Math.min(100, Number.isFinite(rawPercent) ? rawPercent : 0));
  const label = progress.label || statusLabel(job.status);
  const detailParts = [];
  if (progress.detail) detailParts.push(progress.detail);
  if (job.status === "running" && job.last_log_at) detailParts.push(`更新于 ${relativeTime(job.last_log_at)}`);
  const runtimeParts = [];
  if (job.status === "running") {
    if (Number.isFinite(Number(job.elapsed_seconds))) runtimeParts.push(`总计已运行 ${formatDuration(job.elapsed_seconds)}`);
    if (Number.isFinite(Number(job.stage_elapsed_seconds))) runtimeParts.push(`本阶段 ${formatDuration(job.stage_elapsed_seconds)}`);
    if (Number.isFinite(Number(job.estimated_stage_remaining_seconds))) {
      const finish = formatClock(job.estimated_stage_finish_at);
      runtimeParts.push(`本阶段预计还需 ${formatDuration(job.estimated_stage_remaining_seconds)}${finish ? `（约 ${finish}）` : ""}`);
    } else if (Number(job.last_activity_seconds) >= 120) {
      runtimeParts.push(`注意：${formatDuration(job.last_activity_seconds)}没有新进度`);
    } else {
      runtimeParts.push("本阶段完成时间估算中");
    }
  }
  return `
    <div class="job-progress">
      <div class="progress-head">
        <span>${escapeHtml(label)}</span>
        <span>${escapeHtml(`${percent}%${detailParts.length ? ` · ${detailParts.join(" · ")}` : ""}`)}</span>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${escapeAttr(String(percent))}%"></div></div>
      ${runtimeParts.length ? `<div class="job-meta progress-runtime">${escapeHtml(runtimeParts.join(" · "))}</div>` : ""}
    </div>
  `;
}

async function showJobLog(jobId, expand = true) {
  if (expand) {
    state.activeJobId = jobId;
    setLogExpanded(true);
  }
  const data = await api(`/api/jobs/${jobId}/log`);
  const box = $("logBox");
  const shouldFollow = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
  box.textContent = data.lines.join("\n");
  if (shouldFollow || expand) box.scrollTop = box.scrollHeight;
}

function setLogExpanded(expanded) {
  $("logPanel").classList.toggle("collapsed", !expanded);
  $("toggleLogBtn").textContent = expanded ? "折叠" : "展开";
}

async function stopJob(jobId) {
  if (!confirm("确定要停止这个任务吗？长任务可能需要几秒钟退出。")) return;
  try {
    await api(`/api/jobs/${jobId}/stop`, { method: "POST", body: "{}" });
    await loadJobs();
  } catch (err) {
    alert(err.message);
  }
}

async function shutdownApp() {
  if (!confirm("确定要关闭网页并停止本地服务吗？正在运行的任务会被停止。")) return;
  try {
    await api("/api/lifecycle/shutdown", { method: "POST", body: "{}" });
  } catch {
    // The server may exit before the response is fully read.
  }
  document.body.innerHTML = `<main class="shell"><section class="panel"><h1>多语言影音研析已关闭</h1><p>如果当前标签页没有自动关闭，可以直接关掉这个页面。</p></section></main>`;
  setTimeout(() => window.close(), 150);
}

function setActiveAction(button) {
  if (!button) return;
  const group = button.closest(".quick-actions");
  if (!group) return;
  group.querySelectorAll("button").forEach((item) => item.classList.remove("active-action"));
  button.classList.add("active-action");
}

async function previewPath(path, button = null) {
  setActiveAction(button);
  state.currentPreviewPath = path;
  try {
    const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
    renderPreviewContent(data.content, path, data.truncated);
  } catch (err) {
    $("filePreview").textContent = err.message;
  }
}

async function openFolder(path, button = null) {
  setActiveAction(button);
  try { await api("/api/open-folder", { method: "POST", body: JSON.stringify({ path }) }); }
  catch (err) { alert(err.message); }
}

async function openFile(path, button = null) {
  setActiveAction(button);
  try { await api("/api/open-file", { method: "POST", body: JSON.stringify({ path }) }); }
  catch (err) { alert(err.message); }
}

async function openPotPlayer(video, subtitle, button = null) {
  setActiveAction(button);
  try { await api("/api/open-potplayer", { method: "POST", body: JSON.stringify({ video, subtitle }) }); }
  catch (err) { alert(err.message); }
}

async function browseFile(kind, targetId) {
  try {
    const result = await api(`/api/browse-file?kind=${encodeURIComponent(kind)}`);
    if (result.path) setPathControl(targetId, result.path);
  } catch (err) {
    alert(err.message);
  }
}

function copyText(text) { navigator.clipboard.writeText(text); }

function fileNameFromPath(path) { return String(path || "").split(/[\\/]/).pop() || "文本预览"; }

function artifactOptionLabel(item) {
  const states = [];
  if (item.transcript) states.push("转写");
  if (item.analysis) states.push("分析");
  if (item.preview) states.push("预览");
  const suffix = states.length ? ` [${states.join("/")}]` : "";
  return `${shortArtifactLabel(item)}${suffix}`;
}

function artifactOptionTitle(item) {
  const paths = [
    item.transcript?.path,
    item.audio?.path,
    item.analysis?.path,
    item.preview?.path,
  ].filter(Boolean);
  return `${String(item?.label || item?.run_id || "")}\n${paths.join("\n")}`;
}

function shortArtifactLabel(item) {
  const label = String(item?.label || item?.run_id || "未命名文件组").trim();
  return label.length > 56 ? `${label.slice(0, 56)}...` : label;
}

function shortRunId(runId) {
  const text = String(runId || "");
  const match = text.match(/^(\d{8})_(\d{6})/);
  if (!match) return text;
  return `${match[1].slice(4, 6)}-${match[1].slice(6, 8)} ${match[2].slice(0, 2)}:${match[2].slice(2, 4)}`;
}

function relativeTime(value) {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "刚刚";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  return `${Math.round(seconds / 60)} 分钟前`;
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  if (seconds < 60) return "不足 1 分钟";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

function formatClock(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function renderPreviewContent(content, path, truncated) {
  const target = $("filePreview");
  const suffix = fileNameFromPath(path).toLowerCase().split(".").pop();
  const notice = truncated ? `<p><strong>只显示文件尾部。</strong></p>` : "";
  if (suffix === "md") target.innerHTML = `${notice}${renderMarkdown(content)}`;
  else if (suffix === "srt") target.innerHTML = `${notice}${renderSrt(content)}`;
  else target.innerHTML = `<p>${escapeHtml(content).replace(/\n/g, "<br>")}</p>`;
}

function renderMarkdown(markdown) {
  const lines = markdown.split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let inList = false;
  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => { if (inList) { html.push("</ul>"); inList = false; } };
  for (const line of lines) {
    const text = line.trim();
    if (!text) { flushParagraph(); closeList(); continue; }
    if (text.startsWith("### ")) { flushParagraph(); closeList(); html.push(`<h3>${inlineMarkdown(text.slice(4))}</h3>`); }
    else if (text.startsWith("## ")) { flushParagraph(); closeList(); html.push(`<h2>${inlineMarkdown(text.slice(3))}</h2>`); }
    else if (text.startsWith("# ")) { flushParagraph(); closeList(); html.push(`<h1>${inlineMarkdown(text.slice(2))}</h1>`); }
    else if (text.startsWith("- ")) {
      flushParagraph();
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push(`<li>${inlineMarkdown(text.slice(2))}</li>`);
    } else paragraph.push(text);
  }
  flushParagraph();
  closeList();
  return html.join("\n");
}

function inlineMarkdown(text) { return escapeHtml(text).replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>"); }

function renderSrt(content) {
  const blocks = content.trim().split(/\n\s*\n/);
  return blocks.map((block) => {
    const lines = block.split(/\r?\n/).filter(Boolean);
    const timeLine = lines.find((line) => line.includes("-->")) || "";
    const textLines = lines.filter((line) => !/^\d+$/.test(line.trim()) && !line.includes("-->"));
    return `<div class="srt-block"><span class="srt-time">${escapeHtml(timeLine)}</span><p>${escapeHtml(textLines.join("\n")).replace(/\n/g, "<br>")}</p></div>`;
  }).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function escapeAttr(value) { return escapeHtml(value).replace(/\\/g, "\\\\"); }

function bindQualityDefaults(form) {
  const quality = form.querySelector('select[name="quality"]');
  if (!quality) return;
  quality.addEventListener("change", () => {
    const defaults = QUALITY_DEFAULTS[quality.value];
    if (!defaults) return;
    Object.entries(defaults).forEach(([name, value]) => {
      const field = form.querySelector(`[name="${name}"]`);
      if (field) field.value = value;
    });
  });
}

function bindForms() {
  applyTheme(localStorage.getItem(THEME_STORAGE) || "fubuki");
  $("themeSelect").addEventListener("change", (event) => applyTheme(event.currentTarget.value));
  bindQualityDefaults($("pipelineForm"));
  bindQualityDefaults($("transcribeForm"));
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".pane").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      const pane = document.getElementById(tab.dataset.tab);
      if (pane) pane.classList.add("active");
    });
  });

  $("pipelineForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = formPayload(event.currentTarget);
    const selected = selectedArtifact();
    if (!payload.module_transcribe && selected) {
      if (!payload.transcript && selected.transcript?.path) payload.transcript = selected.transcript.path;
      if (!payload.audio && selected.audio?.path) payload.audio = selected.audio.path;
      if (!payload.subtitle && selected.analysis?.files?.["translation_zh.srt"]?.path) {
        payload.subtitle = selected.analysis.files["translation_zh.srt"].path;
      }
    }
    startJob("/api/pipeline", payload);
  });
  $("transcribeForm").addEventListener("submit", (event) => { event.preventDefault(); startJob("/api/transcribe", formPayload(event.currentTarget)); });
  $("analyzeForm").addEventListener("submit", (event) => { event.preventDefault(); startJob("/api/analyze", formPayload(event.currentTarget)); });
  $("previewForm").addEventListener("submit", (event) => { event.preventDefault(); startJob("/api/preview", formPayload(event.currentTarget)); });
  $("dryRunBtn").addEventListener("click", () => { const payload = formPayload($("analyzeForm")); payload.dry_run = true; delete payload.limit_chunks; startJob("/api/analyze", payload); });
  $("oneChunkBtn").addEventListener("click", () => { const payload = formPayload($("analyzeForm")); payload.limit_chunks = 1; startJob("/api/analyze", payload); });
  $("threeChunkBtn").addEventListener("click", () => { const payload = formPayload($("analyzeForm")); payload.limit_chunks = 3; startJob("/api/analyze", payload); });
  $("toggleLogBtn").addEventListener("click", () => {
    const expanded = $("logPanel").classList.contains("collapsed");
    setLogExpanded(expanded);
    if (expanded && state.activeJobId) showJobLog(state.activeJobId, false);
  });
  const cookiesPath = $("cookiesPath");
  const cookiesFromBrowser = $("cookiesFromBrowser");
  if (cookiesPath && cookiesFromBrowser) {
    cookiesPath.addEventListener("change", () => {
      if (cookiesPath.value.trim()) cookiesFromBrowser.value = "";
    });
  }
  $("refreshBtn").addEventListener("click", refreshAll);
  $("shutdownBtn").addEventListener("click", shutdownApp);
  $("artifactSetSelect").addEventListener("change", (event) => {
    state.selectedArtifactRunId = event.currentTarget.value;
    renderQuickResults({ updatePreview: true });
  });
  document.querySelectorAll("[data-browse-kind]").forEach((button) => {
    button.addEventListener("click", () => browseFile(button.dataset.browseKind, button.dataset.browseTarget));
  });
}

async function refreshAll() { await Promise.all([loadStatus(), loadFiles({ keepPreview: false }), loadJobs()]); }

bindForms();
loadLocalSecrets();
startLifecycle();
refreshAll();
setInterval(loadJobs, 1000);




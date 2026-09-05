from __future__ import annotations

import os
import json
import signal
import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import project_root, resource_root
from ..utils import generate_run_id, hidden_process_flags


HEAVY_MODULES = {"transcribe", "analyze", "preview", "pipeline"}


@dataclass
class Job:
    job_id: str
    module: str
    command: list[str]
    env_overrides: dict[str, str] = field(default_factory=dict, repr=False)
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    output_dir: str | None = None
    pid: int | None = None
    returncode: int | None = None
    error: str | None = None
    log_path: str | None = None
    recent_logs: deque[str] = field(default_factory=lambda: deque(maxlen=800))
    progress: dict[str, Any] = field(default_factory=lambda: {"percent": 0, "label": "等待开始", "detail": ""})
    last_log_at: str | None = None
    stage_started_at: str | None = None
    progress_updated_at: str | None = None
    stage_samples: deque[tuple[datetime, float]] = field(default_factory=lambda: deque(maxlen=30), repr=False)
    proc: subprocess.Popen[str] | None = field(default=None, repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    analysis_status: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        now = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now()
        elapsed_seconds = _elapsed_seconds(self.started_at, now)
        stage_elapsed_seconds = _elapsed_seconds(self.stage_started_at, now)
        last_activity_seconds = _elapsed_seconds(self.last_log_at, now)
        estimated_stage_remaining_seconds = self._estimated_stage_remaining_seconds(now, last_activity_seconds)
        return {
            "job_id": self.job_id,
            "module": self.module,
            "command": self.command,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_dir": self.output_dir,
            "pid": self.pid,
            "returncode": self.returncode,
            "error": self.error,
            "analysis_status": self.analysis_status,
            "log_path": self.log_path,
            "recent_logs": list(self.recent_logs),
            "progress": self.progress,
            "last_log_at": self.last_log_at,
            "progress_updated_at": self.progress_updated_at,
            "elapsed_seconds": elapsed_seconds,
            "stage_elapsed_seconds": stage_elapsed_seconds,
            "last_activity_seconds": last_activity_seconds,
            "estimated_stage_remaining_seconds": estimated_stage_remaining_seconds,
            "estimated_stage_finish_at": (
                (now + timedelta(seconds=estimated_stage_remaining_seconds)).isoformat(timespec="seconds")
                if estimated_stage_remaining_seconds is not None
                else None
            ),
        }

    def _estimated_stage_remaining_seconds(
        self,
        now: datetime,
        last_activity_seconds: int | None,
    ) -> int | None:
        if self.status != "running" or last_activity_seconds is None or last_activity_seconds > 120:
            return None
        samples = [(at, value) for at, value in self.stage_samples if (now - at).total_seconds() <= 1800]
        if len(samples) < 2:
            return None
        first_at, first_value = samples[0]
        last_at, last_value = samples[-1]
        seconds = (last_at - first_at).total_seconds()
        advanced = last_value - first_value
        if seconds < 5 or advanced < 1 or last_value >= 100:
            return None
        remaining = (100 - last_value) / (advanced / seconds)
        return max(1, min(round(remaining), 24 * 60 * 60))


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._heavy_running: str | None = None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.public() for job in sorted(self._jobs.values(), key=lambda item: item.started_at or "", reverse=True)]

    def has_running_jobs(self) -> bool:
        with self._lock:
            return any(job.status in {"pending", "running", "stopping"} for job in self._jobs.values())

    def get_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            return job

    def start(
        self,
        module: str,
        command: list[str],
        output_dir: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> Job:
        with self._lock:
            if module in HEAVY_MODULES and self._heavy_running:
                raise HTTPException(
                    status_code=409,
                    detail="当前已有任务正在运行。请等待完成后再启动新任务。",
                )
            job_id = generate_run_id()
            log_dir = project_root() / "outputs" / "logs" / "web_jobs"
            log_dir.mkdir(parents=True, exist_ok=True)
            job = Job(
                job_id=job_id,
                module=module,
                command=command,
                env_overrides=env_overrides or {},
                output_dir=output_dir,
                log_path=str(log_dir / f"{job_id}_{module}.log"),
            )
            self._jobs[job_id] = job
            if module in HEAVY_MODULES:
                self._heavy_running = job_id

        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def stop(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        with self._lock:
            if job.status not in {"pending", "running", "stopping"}:
                return job.public()
            job.cancelled.set()
            job.status = "stopping"
            proc = job.proc
        if proc:
            terminate_process_tree(proc)
        return job.public()

    def stop_all(self) -> None:
        with self._lock:
            active = [job for job in self._jobs.values() if job.status in {"pending", "running", "stopping"}]
        for job in active:
            try:
                self.stop(job.job_id)
            except Exception:
                pass

    def _append_log(self, job: Job, text: str) -> None:
        line = text.rstrip("\r\n")
        if not line:
            return
        if line.startswith("ARTIFACT_RESULT "):
            try:
                job.output_dir = json.loads(line.split(" ", 1)[1]).get("output_dir") or job.output_dir
            except (ValueError, AttributeError):
                pass
        if line.startswith("ANALYSIS_STATUS "):
            try:
                job.analysis_status = json.loads(line.split(" ", 1)[1])
            except ValueError:
                pass
        now = datetime.now()
        job.last_log_at = now.isoformat(timespec="seconds")
        before = dict(job.progress)
        update_progress_from_log(job, line)
        if job.progress != before:
            job.progress_updated_at = job.last_log_at
            if job.progress.get("label") != before.get("label"):
                job.stage_started_at = job.last_log_at
                job.stage_samples.clear()
            stage_percent = job.progress.get("stage_percent")
            if isinstance(stage_percent, (int, float)):
                value = max(0.0, min(100.0, float(stage_percent)))
                if not job.stage_samples or value > job.stage_samples[-1][1]:
                    job.stage_samples.append((now, value))
        display_line = f"[{now.strftime('%H:%M:%S')}] {line}"
        job.recent_logs.append(display_line)
        if job.log_path:
            with Path(job.log_path).open("a", encoding="utf-8", errors="replace") as f:
                f.write(display_line + "\n")

    def _run_job(self, job: Job) -> None:
        start_time = datetime.now()
        job.started_at = start_time.isoformat(timespec="seconds")
        job.stage_started_at = job.started_at
        job.progress_updated_at = job.started_at
        job.status = "running"
        job.progress = {"percent": 2, "label": "启动任务", "detail": ""}
        self._append_log(job, "COMMAND: " + " ".join(job.command))
        env = os.environ.copy()
        env.update(job.env_overrides)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            with self._lock:
                if job.cancelled.is_set():
                    job.status = "stopped"
                    return
                job.proc = subprocess.Popen(
                    job.command, cwd=str(project_root()), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    env=env, creationflags=hidden_process_flags(), start_new_session=os.name != "nt",
                )
            job.pid = job.proc.pid
            assert job.proc.stdout is not None
            for line in job.proc.stdout:
                self._append_log(job, line)
            job.returncode = job.proc.wait()
            if job.cancelled.is_set():
                job.status = "stopped"
                job.progress = {**job.progress, "label": "已取消"}
            else:
                job.status = "succeeded" if job.returncode == 0 else "failed"
                if job.returncode == 2 and job.analysis_status:
                    job.status = "partial"
                if job.status == "succeeded":
                    job.progress = {"percent": 100, "label": "已完成", "detail": ""}
                elif job.status == "failed":
                    job.progress = {**job.progress, "label": "失败"}
                elif job.status == "partial":
                    job.progress = {"percent": 100, "label": "部分完成", "detail": "请重试缺失的翻译"}
            if job.returncode and job.status == "failed" and not job.error:
                job.error = f"process exited with code {job.returncode}"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.progress = {**job.progress, "label": "失败"}
            self._append_log(job, f"ERROR: {exc}")
        finally:
            job.env_overrides.clear()
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            if not job.output_dir:
                job.output_dir = guess_output_dir(job.module, start_time)
            with self._lock:
                if self._heavy_running == job.job_id:
                    self._heavy_running = None


def guess_output_dir(module: str, start_time: datetime) -> str | None:
    root = project_root()
    if module == "preview":
        base = root / "outputs" / "previews"
        return _latest_dir_after(base, start_time)
    if module == "pipeline":
        base = root / "outputs" / "media"
        return _latest_dir_after(base, start_time)
    if module == "analyze":
        base = root / "outputs" / "analysis"
        return _latest_dir_after(base, start_time)
    if module == "transcribe":
        return str((root / "outputs").resolve())
    return None


def _elapsed_seconds(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return max(0, round((now - parsed).total_seconds()))


def _latest_dir_after(base: Path, start_time: datetime) -> str | None:
    if not base.exists():
        return None
    candidates = [path for path in base.iterdir() if path.is_dir() and path.stat().st_mtime >= start_time.timestamp() - 2]
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve())


def cli_command_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable or "python", str(resource_root() / "main.py")]


def terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=hidden_process_flags(), timeout=15)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)


jobs = JobManager()


def update_progress_from_log(job: Job, line: str) -> None:
    if job.module == "transcribe":
        update_transcribe_progress(job, line)
    elif job.module == "analyze":
        update_analyze_progress(job, line)
    elif job.module == "preview":
        update_preview_progress(job, line)
    elif job.module == "pipeline":
        update_pipeline_progress(job, line)


def update_transcribe_progress(job: Job, line: str) -> None:
    model_match = re.search(r"model download percent=([0-9.]+)", line)
    if model_match:
        percent = min(100.0, float(model_match.group(1)))
        job.progress = {"percent": 40, "label": "下载语音模型", "detail": f"{percent:.0f}%", "stage_percent": percent}
        return
    download_match = re.search(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%", line)
    if download_match:
        downloaded = max(0.0, min(100.0, float(download_match.group(1))))
        job.progress = {
            "percent": round(8 + downloaded * 0.2),
            "label": "下载音频",
            "detail": f"{downloaded:.1f}%",
            "stage_percent": downloaded,
        }
        return

    whisper_match = re.search(r"whisper progress percent=([0-9]+(?:\.[0-9]+)?)\s+audio=([^\s]+)", line)
    if whisper_match:
        recognized = max(0.0, min(100.0, float(whisper_match.group(1))))
        job.progress = {
            "percent": round(45 + recognized * 0.45),
            "label": "Whisper 语音识别",
            "detail": f"{recognized:.1f}% · {whisper_match.group(2)}",
            "stage_percent": recognized,
        }
        return

    match = re.search(r"\[(\d)/6\]\s*(.+)", line)
    if not match:
        return
    step = int(match.group(1))
    labels = {
        1: "准备输入",
        2: "下载或定位音频",
        3: "转换音频格式",
        4: "加载 Whisper 模型",
        5: "语音识别中",
        6: "导出转写结果",
    }
    # Model loading and the first recognition sample share the same boundary;
    # keeping step 5 at 45 prevents the progress bar from moving backwards.
    percents = {1: 8, 2: 18, 3: 32, 4: 45, 5: 45, 6: 94}
    job.progress = {
        "percent": percents.get(step, min(95, step * 15)),
        "label": labels.get(step, match.group(2).strip()),
        "detail": f"{step}/6",
    }


def update_analyze_progress(job: Job, line: str) -> None:
    chunks_match = re.search(r"chunks=(\d+)", line)
    if chunks_match:
        total = int(chunks_match.group(1))
        job.progress = {"percent": 3, "label": "准备研析", "detail": f"0/{total} 个分段", "total": total}
        return

    if "Gemini request start" in line:
        job.progress = {**job.progress, "label": "等待 Gemini 返回"}
        return

    process_match = re.search(r"(?:process|skip validated cache)\s+chunk_\d+\s+\((\d+)/(\d+)\)", line)
    if process_match:
        current = int(process_match.group(1))
        total = int(process_match.group(2))
        percent = min(96, max(5, round((current - 1) / max(total, 1) * 100)))
        label = "复用已有结果" if "skip cached" in line else "研析内容分段"
        stage_percent = min(100.0, max(0.0, current / max(total, 1) * 100.0))
        job.progress = {
            "percent": percent,
            "label": label,
            "detail": f"{current}/{total} 个分段",
            "total": total,
            "stage_percent": stage_percent,
        }
        return

    if "retry sleep seconds" in line:
        job.progress = {**job.progress, "label": "Gemini 重试等待"}
    elif "json parse failed" in line:
        job.progress = {**job.progress, "label": "修复 JSON 响应"}
    elif "analysis finished" in line:
        job.progress = {"percent": 100, "label": "已完成", "detail": ""}


def update_preview_progress(job: Job, line: str) -> None:
    if "ffmpeg" in line.lower():
        job.progress = {"percent": 55, "label": "生成预览视频", "detail": "ffmpeg"}
    elif "PotPlayer" in line or "预览" in line:
        job.progress = {"percent": 20, "label": "准备预览文件", "detail": ""}


def update_pipeline_progress(job: Job, line: str) -> None:
    if "开始模块一" in line:
        job.progress = {"percent": 5, "label": "语音转写", "detail": ""}
        return
    if "开始模块二" in line:
        job.progress = {"percent": 42, "label": "翻译与学习分析", "detail": ""}
        return
    if "开始模块三" in line:
        job.progress = {"percent": 86, "label": "媒体预览", "detail": ""}
        return
    if "完整处理完成" in line:
        job.progress = {"percent": 100, "label": "已完成", "detail": ""}
        return
    if (
        re.search(r"\[(\d)/6\]", line)
        or re.search(r"\[download\]\s+[0-9]+(?:\.[0-9]+)?%", line)
        or "whisper progress percent=" in line
        or "model download percent=" in line
    ):
        before = dict(job.progress)
        update_transcribe_progress(job, line)
        step_percent = float(job.progress.get("percent", 0))
        job.progress = {
            **job.progress,
            "percent": min(40, round(5 + step_percent * 0.35)),
            "label": job.progress.get("label", "语音转写"),
            "detail": job.progress.get("detail", before.get("detail", "")),
        }
        return
    before = dict(job.progress)
    update_analyze_progress(job, line)
    if job.progress != before and before.get("percent", 0) >= 42:
        raw = float(job.progress.get("percent", 0))
        job.progress = {
            **job.progress,
            "percent": min(85, round(42 + raw * 0.43)),
        }






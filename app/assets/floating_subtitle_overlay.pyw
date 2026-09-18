#!/usr/bin/env python3
"""Standalone compact floating subtitle window.

The visual constants and interaction model intentionally mirror the compact
two-line window in gemini-live-translator/src/subtitle_window.py.

The generated copy lives beside a preview's subtitles directory and defaults
to display.bilingual.srt. It can also be launched with --srt and --start when
the subtitle source or playback position differs.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import os
import re
import shutil
import sys
import subprocess
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox


# Keep these values aligned with Gemini Live Translator's compact layout.
COMPACT_CORNER_RADIUS = 14
COMPACT_BORDER_COLOR = "#3b4654"
COMPACT_BOTTOM_PADDING = 9
COMPACT_HEIGHT = 68
AUDIO_CONTROL_HEIGHT = 36
AUDIO_WINDOW_HEIGHT = 112
COMPACT_WINDOW_WIDTH = 900
COMPACT_MIN_WINDOW_WIDTH = 380
# The reference project's config.yaml uses 20px for the compact overlay.
COMPACT_FONT_SIZE = 20
COMPACT_OPACITY = 0.90
COMPACT_BACKGROUND_COLOR = "#161a20"
SOURCE_COLOR = "#dbeafe"
TRANSLATION_COLOR = "#ffffff"
DEFAULT_SUBTITLE_RELATIVE = "__DEFAULT_SUBTITLE_RELATIVE__"
DEFAULT_AUDIO_RELATIVE = "__DEFAULT_AUDIO_RELATIVE__"
TIME_PATTERN = re.compile(
    r"(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})(?:[,.](?P<millis>\d{3}))?"
)


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    source: str
    translation: str


def parse_timestamp(value: str) -> float:
    match = TIME_PATTERN.search(value.strip())
    if not match:
        raise ValueError(f"无法解析字幕时间：{value}")
    return (
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + int(match.group("seconds"))
        + int(match.group("millis") or 0) / 1000
    )


def load_cues(path: Path) -> list[SubtitleCue]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if time_index < 0:
            continue
        start_text, _, end_text = lines[time_index].partition("-->")
        try:
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text)
        except ValueError:
            continue
        text_lines = lines[time_index + 1 :]
        if not text_lines or end <= start:
            continue
        if len(text_lines) == 1:
            source = ""
            translation = text_lines[0]
        else:
            source = text_lines[0]
            translation = "\n".join(text_lines[1:])
        cues.append(SubtitleCue(start, end, source, translation))
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


class AudioPlayback:
    """Control a hidden ffplay process by restarting it at the requested position."""

    def __init__(
        self,
        audio_path: Path,
        ffplay_path: Path,
        *,
        start_offset: float = 0.0,
        duration: float = 0.0,
        loop: bool = False,
    ) -> None:
        self.audio_path = audio_path
        self.ffplay_path = ffplay_path
        self.duration = max(0.0, duration)
        self.loop = loop
        self.position_value = max(0.0, start_offset)
        if self.duration:
            self.position_value = min(self.position_value, self.duration)
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.playing = False
        self.play()

    def position(self) -> float:
        if not self.playing or self.started_at is None:
            return self.position_value
        current = self.position_value + (time.monotonic() - self.started_at)
        if self.duration:
            return min(current, self.duration)
        return current

    def play(self) -> None:
        position = self.position()
        if self.duration and position >= self.duration - 0.05:
            position = 0.0
        self._stop_process()
        self.position_value = max(0.0, position)
        self.process = start_audio(self.audio_path, self.ffplay_path, self.position_value)
        self.started_at = time.monotonic()
        self.playing = True

    def pause(self) -> None:
        if not self.playing:
            return
        self.position_value = self.position()
        self.playing = False
        self.started_at = None
        self._stop_process()

    def toggle(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def seek(self, position: float) -> None:
        was_playing = self.playing
        self._stop_process()
        self.playing = False
        self.started_at = None
        self.position_value = max(0.0, position)
        if self.duration:
            self.position_value = min(self.position_value, self.duration)
        if was_playing:
            self.play()

    def nudge(self, seconds: float) -> None:
        self.seek(self.position() + seconds)

    def poll(self) -> None:
        if not self.playing or self.process is None or self.process.poll() is None:
            return
        self.position_value = self.duration or self.position()
        self.playing = False
        self.started_at = None
        self.process = None
        if self.loop:
            self.position_value = 0.0
            self.play()

    def close(self) -> None:
        self.playing = False
        self.started_at = None
        self._stop_process()

    def _stop_process(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.8)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


class FloatingSubtitleOverlay:
    def __init__(
        self,
        cues: list[SubtitleCue],
        *,
        start_offset: float = 0.0,
        loop: bool = False,
        audio_player: AudioPlayback | None = None,
        title: str = "Gemini Live Translator",
    ) -> None:
        self.cues = cues
        self.starts = [cue.start for cue in cues]
        self.start_offset = max(0.0, start_offset)
        self.loop = loop
        self.audio_player = audio_player
        self.timeline_end = max(cue.end for cue in cues)
        self.started_at = time.monotonic()
        self._last_index: int | None = None
        self._closing = False
        self._drag_offset: tuple[int, int] | None = None
        self._resize_start: tuple[int, int, int, int] | None = None
        self._resize_edges: tuple[bool, bool, bool, bool] | None = None
        self._compact_region_handle: int | None = None

        self.root = tk.Tk()
        self.root.title(title)
        window_height = AUDIO_WINDOW_HEIGHT if self.audio_player else COMPACT_HEIGHT
        if DEFAULT_AUDIO_RELATIVE:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            window_x = max(0, (screen_width - COMPACT_WINDOW_WIDTH) // 2)
            window_y = max(0, screen_height - window_height - 80)
        else:
            window_x, window_y = 180, 80
        self.root.geometry(
            f"{COMPACT_WINDOW_WIDTH}x{window_height}+{window_x}+{window_y}"
        )
        self.root.resizable(True, True)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", COMPACT_OPACITY)
        self.root.configure(bg=COMPACT_BACKGROUND_COLOR)

        self.source_font = tkfont.Font(
            self.root,
            family="Segoe UI",
            size=-(COMPACT_FONT_SIZE - 1),
            weight="normal",
        )
        self.translation_font = tkfont.Font(
            self.root,
            family="Segoe UI",
            size=-COMPACT_FONT_SIZE,
            weight="normal",
        )
        self.root.minsize(COMPACT_MIN_WINDOW_WIDTH, self._minimum_height())

        self.frame = tk.Frame(
            self.root,
            bg=COMPACT_BACKGROUND_COLOR,
            padx=0,
            pady=0,
            bd=0,
            relief="flat",
            highlightbackground=COMPACT_BORDER_COLOR,
            highlightthickness=1,
        )
        self.frame.pack(fill="both", expand=True)

        self.close_button = tk.Button(
            self.frame,
            text="×",
            command=self.close,
            fg="#dddddd",
            bg=COMPACT_BACKGROUND_COLOR,
            activebackground="#2a2f37",
            activeforeground="#ffffff",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            takefocus=0,
            font=("Segoe UI", -16),
        )
        self.close_button.place(relx=1.0, x=-3, y=1, anchor="ne", width=22, height=20)

        self.resize_handle = tk.Canvas(
            self.frame,
            width=14,
            height=14,
            bg=COMPACT_BACKGROUND_COLOR,
            highlightthickness=0,
            cursor="size_nw_se",
        )
        self.resize_handle.create_line(4, 13, 13, 13, fill="#a0a0a0", width=1)
        self.resize_handle.create_line(13, 4, 13, 13, fill="#a0a0a0", width=1)
        self.resize_handle.place(relx=1.0, rely=1.0, x=-2, y=-2, anchor="se")

        self.source_label = tk.Label(
            self.frame,
            bg=COMPACT_BACKGROUND_COLOR,
            fg=SOURCE_COLOR,
            anchor="w",
            justify="left",
            font=self.source_font,
        )
        self.translation_label = tk.Label(
            self.frame,
            bg=COMPACT_BACKGROUND_COLOR,
            fg=TRANSLATION_COLOR,
            anchor="w",
            justify="left",
            font=self.translation_font,
        )
        self._create_audio_controls()
        self.frame.bind("<Configure>", self._place_labels)
        self._place_labels()

        for widget in (self.frame, self.source_label, self.translation_label):
            widget.bind("<ButtonPress-1>", self._start_pointer)
            widget.bind("<B1-Motion>", self._move_pointer)
            widget.bind("<ButtonRelease-1>", self._end_pointer)
            widget.bind("<Motion>", self._update_cursor)
        self.resize_handle.bind("<ButtonPress-1>", self._start_resize)
        self.resize_handle.bind("<B1-Motion>", self._resize)
        self.resize_handle.bind("<ButtonRelease-1>", self._end_pointer)
        self.root.bind("<Escape>", self.close)
        if self.audio_player:
            self.root.bind_all("<space>", self._handle_space, add="+")
            self.root.bind_all("<Left>", lambda _event: self._seek_by(-5), add="+")
            self.root.bind_all("<Right>", lambda _event: self._seek_by(5), add="+")
            self.root.bind_all("<Shift-Left>", lambda _event: self._seek_by(-30), add="+")
            self.root.bind_all("<Shift-Right>", lambda _event: self._seek_by(30), add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.update_idletasks()
        self._apply_rounded_region()
        self.root.after(100, self._tick)

    def run(self) -> None:
        self.root.mainloop()

    def close(self, _event: object | None = None) -> None:
        if self._closing:
            return
        self._closing = True
        self._clear_rounded_region()
        if self.audio_player is not None:
            self.audio_player.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        if self._closing:
            return
        if self.audio_player is not None:
            self.audio_player.poll()
            timeline = self.audio_player.position()
            self._update_audio_controls()
        else:
            timeline = self.start_offset + (time.monotonic() - self.started_at)
            if self.loop and self.timeline_end > 0 and timeline >= self.timeline_end:
                timeline %= self.timeline_end
        index = bisect.bisect_right(self.starts, timeline) - 1
        if index < 0 or timeline >= self.cues[index].end:
            index = -1
        if index != self._last_index:
            self._last_index = index
            if index < 0:
                self.source_label.configure(text="")
                self.translation_label.configure(text="")
            else:
                cue = self.cues[index]
                available_width = max(1, self.root.winfo_width() - 26)
                self.source_label.configure(
                    text=self._fit_tail(cue.source, self.source_font, available_width)
                )
                self.translation_label.configure(
                    text=self._fit_tail(cue.translation, self.translation_font, available_width)
                )
        self.root.after(100, self._tick)

    def _create_audio_controls(self) -> None:
        self.controls_frame: tk.Frame | None = None
        self.play_button: tk.Button | None = None
        self.progress_var: tk.DoubleVar | None = None
        self.progress_scale: ttk.Scale | None = None
        self.time_var: tk.StringVar | None = None
        self._seeking = False
        if self.audio_player is None:
            return

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Compact.Horizontal.TScale",
            background=COMPACT_BACKGROUND_COLOR,
            troughcolor="#3b4654",
            bordercolor=COMPACT_BORDER_COLOR,
            lightcolor="#8ab4f8",
            darkcolor="#8ab4f8",
        )
        self.controls_frame = tk.Frame(
            self.frame,
            bg=COMPACT_BACKGROUND_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self.controls_frame.place(
            x=8,
            y=-2,
            relx=0,
            rely=1.0,
            relwidth=1.0,
            width=-16,
            height=AUDIO_CONTROL_HEIGHT,
            anchor="sw",
        )
        button_options = {
            "fg": "#e5e7eb",
            "bg": COMPACT_BACKGROUND_COLOR,
            "activebackground": "#2a2f37",
            "activeforeground": "#ffffff",
            "bd": 0,
            "highlightthickness": 0,
            "font": ("Segoe UI", -9),
            "padx": 5,
            "pady": 1,
            "takefocus": 0,
        }
        self.play_button = tk.Button(
            self.controls_frame,
            text="暂停",
            command=self._toggle_playback,
            **button_options,
        )
        self.play_button.pack(side="left", padx=(2, 2), pady=6)
        tk.Button(
            self.controls_frame,
            text="-10s",
            command=lambda: self._seek_by(-10),
            **button_options,
        ).pack(side="left", padx=2, pady=6)
        tk.Button(
            self.controls_frame,
            text="+10s",
            command=lambda: self._seek_by(10),
            **button_options,
        ).pack(side="left", padx=2, pady=6)
        self.progress_var = tk.DoubleVar(self.root, value=self.audio_player.position())
        self.progress_scale = ttk.Scale(
            self.controls_frame,
            from_=0.0,
            to=max(1.0, self.audio_player.duration),
            orient="horizontal",
            variable=self.progress_var,
            command=self._on_progress_changed,
            style="Compact.Horizontal.TScale",
        )
        self.progress_scale.pack(side="left", fill="x", expand=True, padx=(9, 8), pady=10)
        self.progress_scale.bind("<ButtonPress-1>", self._begin_progress_seek)
        self.progress_scale.bind("<ButtonRelease-1>", self._commit_progress_seek)
        self.time_var = tk.StringVar(self.root, value="00:00 / 00:00")
        tk.Label(
            self.controls_frame,
            textvariable=self.time_var,
            bg=COMPACT_BACKGROUND_COLOR,
            fg="#cbd5e1",
            font=("Segoe UI", -9),
            width=13,
            anchor="e",
        ).pack(side="right", padx=(0, 7), pady=7)

    def _toggle_playback(self) -> None:
        if self.audio_player is not None:
            self.audio_player.toggle()
            self._update_audio_controls()

    def _handle_space(self, _event: tk.Event) -> str:
        self._toggle_playback()
        return "break"

    def _seek_by(self, seconds: float) -> None:
        if self.audio_player is not None:
            self.audio_player.nudge(seconds)
            self._update_audio_controls()

    def _begin_progress_seek(self, _event: tk.Event) -> None:
        self._seeking = True

    def _on_progress_changed(self, value: str) -> None:
        if self._seeking and self.time_var is not None:
            self.time_var.set(
                f"{self._format_clock(float(value))} / "
                f"{self._format_clock(self.audio_player.duration if self.audio_player else 0)}"
            )

    def _commit_progress_seek(self, _event: tk.Event) -> None:
        if self.audio_player is not None and self.progress_var is not None:
            self.audio_player.seek(float(self.progress_var.get()))
        self._seeking = False
        self._update_audio_controls()

    def _update_audio_controls(self) -> None:
        if self.audio_player is None:
            return
        position = self.audio_player.position()
        if self._seeking:
            if self.play_button is not None:
                self.play_button.configure(text="暂停" if self.audio_player.playing else "播放")
            return
        if not self._seeking and self.progress_var is not None:
            self.progress_var.set(position)
        if self.time_var is not None:
            self.time_var.set(
                f"{self._format_clock(position)} / "
                f"{self._format_clock(self.audio_player.duration)}"
            )
        if self.play_button is not None:
            self.play_button.configure(text="暂停" if self.audio_player.playing else "播放")

    @staticmethod
    def _format_clock(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _fit_tail(text: str, font: tkfont.Font, available_width: int) -> str:
        if not text or font.measure(text) <= available_width:
            return text
        ellipsis = "…"
        if font.measure(ellipsis) >= available_width:
            return ellipsis
        low, high = 0, len(text)
        while low < high:
            size = (low + high + 1) // 2
            candidate = ellipsis + text[-size:]
            if font.measure(candidate) <= available_width:
                low = size
            else:
                high = size - 1
        return ellipsis + text[-low:] if low else ellipsis

    def _minimum_height(self) -> int:
        top = 4
        source_height = max(18, self.source_font.metrics("linespace") + 3)
        translation_height = max(19, self.translation_font.metrics("linespace") + 3)
        controls_height = AUDIO_CONTROL_HEIGHT + 2 if self.audio_player else 0
        return max(
            AUDIO_WINDOW_HEIGHT if self.audio_player else COMPACT_HEIGHT,
            top + source_height + translation_height + controls_height + COMPACT_BOTTOM_PADDING,
        )

    def _place_labels(self, _event: tk.Event | None = None) -> None:
        top = 4
        source_height = max(18, self.source_font.metrics("linespace") + 3)
        translation_height = max(19, self.translation_font.metrics("linespace") + 3)
        self.source_label.place(
            x=13,
            y=top,
            relwidth=1,
            width=-26,
            height=source_height,
        )
        self.translation_label.place(
            x=13,
            y=top + source_height,
            relwidth=1,
            width=-26,
            height=translation_height,
        )

    def _start_pointer(self, event: tk.Event) -> None:
        edges = self._resize_edges_at(event)
        if any(edges):
            self._resize_edges = edges
            self._resize_start = (
                event.x_root,
                event.y_root,
                self.root.winfo_width(),
                self.root.winfo_height(),
            )
            self._drag_offset = (self.root.winfo_x(), self.root.winfo_y())
            return
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _move_pointer(self, event: tk.Event) -> None:
        if self._resize_start and self._resize_edges:
            self._resize_from_pointer(event)
            return
        if self._drag_offset:
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.root.geometry(f"+{x}+{y}")

    def _end_pointer(self, _event: tk.Event) -> str:
        self._drag_offset = None
        self._resize_start = None
        self._resize_edges = None
        return "break"

    def _start_resize(self, event: tk.Event) -> str:
        self._resize_edges = (False, True, False, True)
        self._resize_start = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        self._drag_offset = (self.root.winfo_x(), self.root.winfo_y())
        return "break"

    def _resize(self, event: tk.Event) -> str:
        self._resize_from_pointer(event)
        return "break"

    def _resize_edges_at(self, event: tk.Event) -> tuple[bool, bool, bool, bool]:
        margin = 10
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        return (
            x <= margin,
            x >= self.root.winfo_width() - margin,
            y <= margin,
            y >= self.root.winfo_height() - margin,
        )

    def _update_cursor(self, event: tk.Event) -> None:
        if self._resize_start:
            return
        left, right, top, bottom = self._resize_edges_at(event)
        if (left or right) and (top or bottom):
            cursor = "size_nw_se" if left == top else "size_ne_sw"
        elif left or right:
            cursor = "size_we"
        elif top or bottom:
            cursor = "size_ns"
        else:
            cursor = "arrow"
        for widget in (self.frame, self.source_label, self.translation_label):
            widget.configure(cursor=cursor)

    def _resize_from_pointer(self, event: tk.Event) -> None:
        if not self._resize_start or not self._resize_edges or not self._drag_offset:
            return
        start_x, start_y, start_width, start_height = self._resize_start
        left, right, top, bottom = self._resize_edges
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        width, height = start_width, start_height
        window_x, window_y = self._drag_offset
        if right:
            width += dx
        if bottom:
            height += dy
        if left:
            width -= dx
            window_x += dx
        if top:
            height -= dy
            window_y += dy
        minimum_height = self._minimum_height()
        if width < COMPACT_MIN_WINDOW_WIDTH:
            if left:
                window_x -= COMPACT_MIN_WINDOW_WIDTH - width
            width = COMPACT_MIN_WINDOW_WIDTH
        if height < minimum_height:
            if top:
                window_y -= minimum_height - height
            height = minimum_height
        self.root.geometry(f"{width}x{height}+{window_x}+{window_y}")
        self.root.after_idle(self._apply_rounded_region)

    def _apply_rounded_region(self) -> None:
        """Clip only this borderless top-level window to a rounded rectangle."""
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.SetWindowRgn.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_bool,
            ]
            user32.SetWindowRgn.restype = ctypes.c_int
            gdi32.CreateRoundRectRgn.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
            ]
            gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
            widget_handle = self.root.winfo_id()
            window_handle = int(
                user32.GetAncestor(ctypes.c_void_p(widget_handle), 2) or widget_handle
            )
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            region = gdi32.CreateRoundRectRgn(
                0,
                0,
                width + 1,
                height + 1,
                COMPACT_CORNER_RADIUS * 2,
                COMPACT_CORNER_RADIUS * 2,
            )
            if user32.SetWindowRgn(ctypes.c_void_p(window_handle), region, True):
                self._compact_region_handle = window_handle
        except (AttributeError, OSError, tk.TclError):
            # Other Tk builds still retain the dark compact rectangle.
            pass

    def _clear_rounded_region(self) -> None:
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowRgn.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_bool,
            ]
            user32.SetWindowRgn.restype = ctypes.c_int
            if self._compact_region_handle:
                user32.SetWindowRgn(ctypes.c_void_p(self._compact_region_handle), None, True)
            self._compact_region_handle = None
        except (AttributeError, OSError):
            pass


def find_ffplay(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())
    environment_path = os.environ.get("LIVE_TRANSCRIBER_FFPLAY", "").strip()
    if environment_path:
        candidates.append(Path(environment_path))
    discovered = shutil.which("ffplay")
    if discovered:
        candidates.append(Path(discovered))
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        candidates.append(Path(ffmpeg).with_name("ffplay.exe"))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "未找到 ffplay 音频引擎。请安装包含 ffplay 的 FFmpeg，或使用 --ffplay 指定 ffplay.exe。"
    )


def find_ffprobe(ffplay_path: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    environment_path = os.environ.get("LIVE_TRANSCRIBER_FFPROBE", "").strip()
    if environment_path:
        candidates.append(Path(environment_path))
    if ffplay_path:
        candidates.append(ffplay_path.with_name("ffprobe.exe"))
    discovered = shutil.which("ffprobe")
    if discovered:
        candidates.append(Path(discovered))
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        candidates.append(Path(ffmpeg).with_name("ffprobe.exe"))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def probe_audio_duration(audio_path: Path, ffplay_path: Path, fallback: float) -> float:
    ffprobe_path = find_ffprobe(ffplay_path)
    if ffprobe_path is None:
        return max(0.0, fallback)
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        duration = float((result.stdout or "").strip())
        if duration > 0:
            return duration
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return max(0.0, fallback)


def start_audio(audio_path: Path, ffplay_path: Path, start_offset: float) -> subprocess.Popen:
    command = [
        str(ffplay_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nodisp",
        "-autoexit",
        "-vn",
    ]
    if start_offset > 0:
        command.extend(["-ss", f"{start_offset:.3f}"])
    command.append(str(audio_path))
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_subtitle = script_dir / DEFAULT_SUBTITLE_RELATIVE
    default_audio = script_dir / DEFAULT_AUDIO_RELATIVE if DEFAULT_AUDIO_RELATIVE else None
    parser = argparse.ArgumentParser(description="显示带时间轴的悬挂双语字幕框，可选隐藏音频播放")
    parser.add_argument(
        "--srt",
        type=Path,
        default=default_subtitle,
        help="字幕文件，默认读取同目录 subtitles/display.bilingual.srt",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=0.0,
        help="从字幕时间轴的指定秒数开始显示，例如 --start 125",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=default_audio,
        help="音频文件；音频模式默认读取脚本同目录下的音频文件",
    )
    parser.add_argument(
        "--ffplay",
        type=Path,
        default=None,
        help="ffplay.exe 路径；仅音频模式需要",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="禁用音频模式脚本中的默认音频",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="播完后从时间轴开头循环",
    )
    return parser.parse_args()


def show_error(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("悬挂字幕框", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    args = parse_args()
    subtitle_path = args.srt.expanduser().resolve()
    audio_player: AudioPlayback | None = None
    try:
        if not subtitle_path.exists():
            raise FileNotFoundError(f"字幕文件不存在：{subtitle_path}")
        cues = load_cues(subtitle_path)
        if not cues:
            raise ValueError(f"字幕文件没有可显示的时间轴：{subtitle_path}")
        audio_path = None if args.no_audio or args.audio is None else args.audio.expanduser().resolve()
        if audio_path is not None:
            if not audio_path.exists():
                raise FileNotFoundError(f"音频文件不存在：{audio_path}")
            ffplay_path = find_ffplay(args.ffplay)
            duration = probe_audio_duration(audio_path, ffplay_path, max(cue.end for cue in cues))
            audio_player = AudioPlayback(
                audio_path,
                ffplay_path,
                start_offset=args.start,
                duration=duration,
                loop=args.loop,
            )
        FloatingSubtitleOverlay(
            cues,
            start_offset=args.start,
            loop=args.loop,
            audio_player=audio_player,
        ).run()
        return 0
    except Exception as exc:
        show_error(str(exc))
        return 1
    finally:
        if audio_player is not None:
            audio_player.close()


if __name__ == "__main__":
    raise SystemExit(main())

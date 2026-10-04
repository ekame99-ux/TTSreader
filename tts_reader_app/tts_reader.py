# -*- coding: utf-8 -*-
"""
Text-to-Speech Book Reader
Reads txt/epub files and converts them to speech using edge-tts.
Supports bookmarks, resume from last position, and segmented playback.

Usage: python tts_reader.py
Or double-click start.bat on Windows.
"""

import os
import sys
import re
import io
import json
import asyncio
import queue
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import edge_tts
import soundfile as sf
import sounddevice as sd

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_NAME = "TTS Book Reader"
SEGMENT_MAX_LEN = 200
ENDERS_PAT = re.compile(r'[\.\!\?!。！？]+[\"\')\]}\u201d\u2019\u3001]*')

# Config dir: store next to the script (or in user home if frozen)
if getattr(sys, 'frozen', False):
    _config_dir = os.path.join(os.path.dirname(sys.executable), ".tts_reader_config")
else:
    _config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tts_reader_config")
os.makedirs(_config_dir, exist_ok=True)

BOOKMARKS_FILE = os.path.join(_config_dir, "bookmarks.json")
SETTINGS_FILE = os.path.join(_config_dir, "settings.json")

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def split_text(text, max_len=SEGMENT_MAX_LEN):
    """Split text into segments at sentence boundaries near max_len chars."""
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []

    segments = []
    current = ''

    for para in text.split('\n'):
        para = para.strip()
        if not para:
            continue
        current += para + ' '

        while len(current) >= max_len:
            search_region = current[:int(max_len * 1.5)]
            last_end = None
            for m in ENDERS_PAT.finditer(search_region):
                last_end = m.end()

            if last_end and last_end >= max_len // 2:
                seg = current[:last_end].strip()
                if seg:
                    segments.append(seg)
                current = current[last_end:].strip()
            else:
                break_at = current.rfind(' ', max_len, int(max_len * 1.5))
                if break_at > max_len // 2:
                    segments.append(current[:break_at].strip())
                    current = current[break_at:].strip()
                else:
                    segments.append(current[:max_len].strip())
                    current = current[max_len:].strip()

    if current.strip():
        segments.append(current.strip())
    return segments


def extract_text_from_txt(filepath):
    """Extract text from a .txt file, trying common encodings."""
    for enc in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("Cannot decode file with any known encoding")


def extract_text_from_epub(filepath):
    """Extract readable text from an .epub file."""
    book = epub.read_epub(filepath, options={'ignore_ncx': True})
    parts = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        name = item.get_name().lower()
        if 'nav' in name or 'toc' in name:
            continue
        try:
            content = item.get_content().decode('utf-8')
        except UnicodeDecodeError:
            content = item.get_content().decode('latin-1')
        soup = BeautifulSoup(content, 'lxml')
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        if text:
            parts.append(text)
    return '\n\n'.join(parts)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# TTS Background Worker
# ---------------------------------------------------------------------------

class TTSWorker:
    """Runs edge-tts generation in a background thread with its own event loop."""

    def __init__(self):
        self._queue = queue.Queue()
        self._results = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = None
        self._voice = 'en-US-JennyNeural'
        self._rate = '+0%'

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.run(self._async_loop())

    async def _async_loop(self):
        while not self._stop_event.is_set():
            try:
                req = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if req is None:
                break
            await self._process(req)

    async def _process(self, req):
        text = req['text']
        key = req['key']
        try:
            communicate = edge_tts.Communicate(text, self._voice, rate=self._rate)
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk['type'] == 'audio':
                    buf.write(chunk['data'])
            buf.seek(0)
            audio_data = buf.read()
            if not audio_data:
                self._results.put({'key': key, 'error': 'empty audio'})
                return
            audio, sr = sf.read(io.BytesIO(audio_data))
            self._results.put({'key': key, 'audio': audio, 'sr': int(sr)})
        except Exception as exc:
            self._results.put({'key': key, 'error': str(exc)})

    def generate(self, text):
        key = id(text)
        self._queue.put({'text': text, 'key': key})
        return key

    def get_result(self, key, timeout=10):
        try:
            return self._results.get(timeout=timeout)
        except queue.Empty:
            return {'key': key, 'error': 'timeout'}

    def stop(self):
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def set_voice(self, voice):
        self._voice = voice

    def set_rate(self, rate):
        self._rate = rate


# ---------------------------------------------------------------------------
# Audio Player
# ---------------------------------------------------------------------------

class AudioPlayer:
    """Plays audio arrays via sounddevice in a background thread."""

    def __init__(self):
        self._playing = False
        self._paused = False
        self._done_callback = None
        self._play_thread = None

    def play_async(self, audio_data, sample_rate, done_callback=None):
        """Start playback in a background thread. Callback is scheduled on main thread via after(0)."""
        self.stop()
        self._playing = True
        self._paused = False
        self._done_callback = done_callback

        def _run():
            if audio_data is None or len(audio_data) == 0:
                if self._done_callback:
                    self._done_callback()
                return
            try:
                sd.play(audio_data.astype('float32'), int(sample_rate), blocking=True)
            except Exception:
                pass
            self._playing = False
            if self._done_callback:
                # Schedule callback on main thread to avoid Tkinter thread-safety issues
                try:
                    self._done_callback()
                except Exception:
                    pass

        self._play_thread = threading.Thread(target=_run, daemon=True)
        self._play_thread.start()
        return self._play_thread

    def pause(self):
        try:
            sd.stop()
        except Exception:
            pass
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        try:
            sd.stop()
        except Exception:
            pass
        self._playing = False
        self._paused = False
        self._done_callback = None
        # Wait briefly for play thread to finish
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=1)


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class TTSReaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("820x600")
        self.minsize(700, 500)

        # State
        self._segments = []
        self._segment_texts = []
        self._current_idx = 0
        self._total = 0
        self._file_path = None
        self._book_title = ''
        self._is_playing = False
        self._is_paused = False
        self._generating = False
        self._audio_cache = {}
        self._pending_key = None
        self._bookmarked = set()
        self._bookmarks = []
        self._auto_resume = True

        # Settings
        self._settings = load_json(SETTINGS_FILE)
        self._default_voice = self._settings.get('voice', 'en-US-JennyNeural')
        self._default_rate = self._settings.get('rate', '+0%')

        # Workers
        self._tts = TTSWorker()
        self._tts.start()
        self._player = AudioPlayer()

        # Load persisted data
        self._bookmarks = load_json(BOOKMARKS_FILE, [])

        self._build_ui()
        self._update_voice_list()

        # Check for auto-resume after UI is ready
        self.after(600, self._check_resume)

        # Poll TTS results periodically
        self.after(150, self._poll_results)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # --- Toolbar ---
        toolbar = ttk.Frame(self, padding="6")
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="Open File", command=self._open_file).pack(side=tk.LEFT, padx=3)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self._prev_btn = ttk.Button(toolbar, text="Prev", command=self._prev_seg, state=tk.DISABLED)
        self._prev_btn.pack(side=tk.LEFT, padx=3)

        self._play_btn = ttk.Button(toolbar, text="Play", command=self._toggle_play, state=tk.DISABLED)
        self._play_btn.pack(side=tk.LEFT, padx=3)

        self._stop_btn = ttk.Button(toolbar, text="Stop", command=self._stop, state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=3)

        self._next_btn = ttk.Button(toolbar, text="Next", command=self._next_seg, state=tk.DISABLED)
        self._next_btn.pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(toolbar, text="Bookmark", command=self._toggle_bookmark).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Bookmarks", command=self._show_bookmarks).pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(toolbar, text="Voice:").pack(side=tk.LEFT, padx=(10, 2))
        self._voice_var = tk.StringVar(value=self._default_voice)
        self._voice_cb = ttk.Combobox(toolbar, textvariable=self._voice_var, width=32, state='readonly')
        self._voice_cb.pack(side=tk.LEFT, padx=2)
        self._voice_cb.bind('<<ComboboxSelected>>', self._on_voice_change)

        ttk.Label(toolbar, text="Speed:").pack(side=tk.LEFT, padx=(10, 2))
        self._rate_var = tk.StringVar(value=self._default_rate)
        self._rate_cb = ttk.Combobox(toolbar, textvariable=self._rate_var,
                                      values=['-20%', '-10%', '+0%', '+10%', '+20%'],
                                      width=8, state='readonly')
        self._rate_cb.pack(side=tk.LEFT, padx=2)
        self._rate_cb.bind('<<ComboboxSelected>>', self._on_rate_change)

        # --- Text area ---
        text_frame = ttk.Frame(self)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        self._text_widget = scrolledtext.ScrolledText(
            text_frame, wrap=tk.WORD, font=("Consolas", 11),
            state=tk.DISABLED, bg="#fafafa", fg="#222",
            selectbackground="#d0e8ff", selectforeground="#111"
        )
        self._text_widget.pack(fill=tk.BOTH, expand=True)

        # Tag for current segment highlight
        self._text_widget.tag_configure("curr", background="#fff3cd")
        self._text_widget.tag_configure("bm", foreground="#cc6600", font=("Consolas", 11, "bold"))

        # --- Status bar ---
        self._status = ttk.Label(
            self, text="Ready  |  Open a txt or epub file to begin.",
            relief=tk.SUNKEN, anchor=tk.W
        )
        self._status.pack(fill=tk.X, side=tk.BOTTOM)

        # --- Menu bar ---
        menu_bar = tk.Menu(self)
        self.config(menu=menu_bar)

        file_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open...", command=self._open_file, accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)

        edit_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Bookmarks", command=self._show_bookmarks, accelerator="Ctrl+B")
        edit_menu.add_command(label="Settings", command=self._show_settings)

        help_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Keyboard Shortcuts", command=self._show_help)

    # ------------------------------------------------------------------
    # Voice list
    # ------------------------------------------------------------------

    def _update_voice_list(self):
        try:
            voices = asyncio.run(edge_tts.list_voices())
            # Filter to English and Chinese voices only
            names = sorted(set(v['ShortName'] for v in voices 
                             if v['Locale'].startswith('en') or v['Locale'].startswith('zh')))
            self._voice_cb['values'] = names
            if self._default_voice in names:
                self._voice_var.set(self._default_voice)
            else:
                # Fallback: find voice matching saved locale prefix
                prefix = self._default_voice.split('-')[0] if '-' in self._default_voice else ''
                for n in names:
                    if n.startswith(prefix):
                        self._voice_var.set(n)
                        break
        except Exception:
            pass

    def _on_voice_change(self, event=None):
        self._default_voice = self._voice_var.get()
        self._tts.set_voice(self._default_voice)
        self._save_settings()

    def _on_rate_change(self, event=None):
        self._default_rate = self._rate_var.get()
        self._tts.set_rate(self._default_rate)
        self._save_settings()

    def _save_settings(self):
        save_json(SETTINGS_FILE, {'voice': self._default_voice, 'rate': self._default_rate})

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _open_file(self):
        filepath = filedialog.askopenfilename(
            title="Open Book File",
            filetypes=[
                ("Text files", "*.txt"),
                ("EPUB files", "*.epub"),
                ("All files", "*.*"),
            ],
        )
        if not filepath:
            return
        self._load_book(os.path.abspath(filepath))

    def _load_book(self, filepath):
        self._file_path = filepath
        self._audio_cache = {}
        self._bookmarked = set()
        self._current_idx = 0
        self._is_playing = False
        self._is_paused = False
        self._generating = False
        self._player.stop()

        try:
            if filepath.lower().endswith('.epub'):
                raw = extract_text_from_epub(filepath)
            else:
                raw = extract_text_from_txt(filepath)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read file:\n{e}")
            return

        self._segment_texts = split_text(raw)
        self._total = len(self._segment_texts)
        self._book_title = os.path.splitext(os.path.basename(filepath))[0]

        self._set_controls(True)
        self._display_segment()
        self._refresh_status()

        # Try auto-resume
        self.after(200, self._check_resume)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _display_segment(self):
        if not self._segment_texts:
            return
        idx = self._current_idx
        total = self._total

        # Show current segment with context
        start = max(0, idx - 1)
        end = min(total, idx + 2)

        self._text_widget.configure(state=tk.NORMAL)
        self._text_widget.delete('1.0', tk.END)

        lines = []
        for i in range(start, end):
            marker = "[CUR]" if i == idx else "   "
            bm_marker = "[B]" if i in self._bookmarked else "   "
            text = self._segment_texts[i]
            line = f"{marker} {bm_marker} [{i+1}/{total}]  {text}"
            lines.append(line)

        self._text_widget.insert(tk.END, '\n\n' + '\n---\n'.join(lines) + '\n')

        # Highlight current line
        curr_line = f'{1.0 + (idx - start)}'
        try:
            self._text_widget.tag_add("curr", curr_line, f'{curr_line}+1c')
        except tk.TclError:
            pass

        self._text_widget.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    def _toggle_play(self):
        if not self._segment_texts:
            return
        if self._is_playing and not self._is_paused:
            self._pause()
        elif self._is_playing and self._is_paused:
            self._resume()
        else:
            self._play()

    def _play(self):
        self._is_playing = True
        self._is_paused = False
        self._update_play_btn()

        seg_text = self._segment_texts[self._current_idx]
        cache_key = (self._current_idx, self._default_voice, self._default_rate)

        if cache_key in self._audio_cache:
            audio_data, sr = self._audio_cache[cache_key]
            self._do_play(audio_data, sr)
        else:
            self._generating = True
            self._update_play_btn()
            self._refresh_status()
            key = self._tts.generate(seg_text)
            self._pending_key = key

    def _do_play(self, audio_data, sr, cache_key=None):
        if cache_key is None:
            cache_key = (self._current_idx, self._default_voice, self._default_rate)

        def on_done():
            self.after(0, self._on_play_done)

        self._audio_cache[cache_key] = (audio_data, sr)
        self._player.play_async(audio_data, sr, done_callback=on_done)
        self._save_resume()
        self._update_play_btn()
        self._refresh_status()

    def _on_play_done(self):
        """Called from main thread after audio playback completes."""
        self._is_playing = False
        self._is_paused = False
        self._update_play_btn()
        self._refresh_status()
        # Auto-advance to next segment immediately
        if self._current_idx < self._total - 1:
            self._current_idx += 1
            self._display_segment()
            self._refresh_status()
            self._play()
        else:
            self._after_book_end()
    def _pause(self):
        self._player.pause()
        self._is_paused = True
        self._update_play_btn()
        self._refresh_status()

    def _resume(self):
        self._is_paused = False
        self._update_play_btn()
        self._refresh_status()

    def _stop(self):
        self._player.stop()
        self._is_playing = False
        self._is_paused = False
        self._generating = False
        self._update_play_btn()
        self._refresh_status()



    def _after_book_end(self):
        # Save final position as resume point
        self._save_resume()
        messagebox.showinfo("Finished", "Reached the end of the book.")

    def _prev_seg(self):
        if self._current_idx > 0:
            self._current_idx -= 1
            self._display_segment()
            self._refresh_status()

    def _next_seg(self):
        if self._current_idx < self._total - 1:
            self._current_idx += 1
            self._display_segment()
            self._refresh_status()

    # ------------------------------------------------------------------
    # Results polling (called from main thread periodically)
    # ------------------------------------------------------------------

    def _poll_results(self):
        while True:
            try:
                result = self._tts._results.get_nowait()
            except queue.Empty:
                break

            key = result.get('key')
            if result.get('error'):
                self._generating = False
                self._update_play_btn()
                self._refresh_status()
                continue

            audio_data = result.get('audio')
            sr = result.get('sr')
            if audio_data is not None and self._pending_key == key:
                self._generating = False
                self._update_play_btn()
                # Use the cache key based on current state
                cache_key = (self._current_idx, self._default_voice, self._default_rate)
                self._do_play(audio_data, sr)

        self.after(200, self._poll_results)

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def _toggle_bookmark(self):
        if not self._segment_texts:
            return
        idx = self._current_idx
        if idx in self._bookmarked:
            self._bookmarked.discard(idx)
            self._bookmarks = [b for b in self._bookmarks if not (
                b.get('file') == self._file_path and b.get('segment') == idx
            )]
            self._refresh_status()
        else:
            self._bookmarked.add(idx)
            bm = {
                'file': self._file_path,
                'title': self._book_title,
                'segment': idx,
                'text': self._segment_texts[idx][:80] if idx < len(self._segment_texts) else '',
                'time': time.time(),
            }
            self._bookmarks = [b for b in self._bookmarks if not (
                b.get('file') == self._file_path and b.get('segment') == idx
            )]
            self._bookmarks.append(bm)
            self._save_bookmarks()
            self._refresh_status()

    def _save_bookmarks(self):
        save_json(BOOKMARKS_FILE, self._bookmarks)

    def _show_bookmarks(self):
        if not self._file_path:
            messagebox.showinfo("Bookmarks", "No book loaded.")
            return

        file_bms = [b for b in self._bookmarks if b.get('file') == self._file_path]
        if not file_bms:
            messagebox.showinfo("Bookmarks", "No bookmarks for this book.")
            return

        win = tk.Toplevel(self)
        win.title("Bookmarks")
        win.geometry("520x320")
        win.transient(self)
        win.grab_set()

        listbox = tk.Listbox(win, font=("Consolas", 10), width=60, height=10)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        for bm in file_bms:
            seg = bm.get('segment', 0)
            display = f"[{seg + 1}] {bm.get('text', '')[:70]}"
            listbox.insert(tk.END, display)

        def on_dbl(event):
            sel = listbox.curselection()
            if sel:
                bm_idx = sel[0]
                self._jump_to(file_bms[bm_idx].get('segment', 0))
                win.destroy()

        listbox.bind('<Double-Button-1>', on_dbl)

        ttk.Button(win, text="Select", command=lambda: (
            on_dbl(type('E', (), {'widget': listbox})()), win.destroy()
        )).pack(side=tk.LEFT, padx=10, pady=5)
        ttk.Button(win, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=10, pady=5)

    def _jump_to(self, index):
        if 0 <= index < self._total:
            self._current_idx = index
            self._display_segment()
            self._refresh_status()

    def _save_resume(self):
        """Save current position so we can resume next time."""
        bm = {
            'file': self._file_path,
            'title': self._book_title,
            'segment': self._current_idx,
            'text': self._segment_texts[self._current_idx][:80] if self._current_idx < len(self._segment_texts) else '',
            'time': time.time(),
            '_resume': True,
        }
        self._bookmarks = [b for b in self._bookmarks if not b.get('_resume')]
        self._bookmarks.append(bm)
        self._save_bookmarks()

    def _check_resume(self):
        if not self._auto_resume or not self._file_path:
            return
        for bm in reversed(self._bookmarks):
            if bm.get('file') == self._file_path:
                seg = bm.get('segment', 0)
                if 0 <= seg < len(self._segment_texts):
                    self._current_idx = seg
                    self._display_segment()
                    self._refresh_status()
                    return

    # ------------------------------------------------------------------
    # Status & helpers
    # ------------------------------------------------------------------

    def _update_play_btn(self):
        if self._is_playing and not self._is_paused:
            self._play_btn.configure(text="Pause")
        elif self._is_playing and self._is_paused:
            self._play_btn.configure(text="Resume")
        else:
            self._play_btn.configure(text="Play")

    def _refresh_status(self):
        if not self._segment_texts:
            self._status.configure(text="Ready  |  Open a txt or epub file to begin.")
            return

        idx = self._current_idx
        total = self._total
        seg_text = self._segment_texts[idx] if idx < total else ''
        info = f"Segment {idx + 1}/{total}  |  {len(seg_text)} chars  |  Bookmarked: {len(self._bookmarked)}/{total}"

        if self._is_playing and not self._is_paused:
            info += "  [Playing]"
        elif self._is_playing and self._is_paused:
            info += "  [Paused]"
        elif self._generating:
            info += "  [Generating audio...]"

        self._status.configure(text=info)

    def _set_controls(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self._prev_btn.configure(state=state)
        self._play_btn.configure(state=state)
        self._stop_btn.configure(state=state)
        self._next_btn.configure(state=state)

    def _show_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("400x180")
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="Default Voice:").grid(row=0, column=0, padx=10, pady=12, sticky=tk.W)
        ttk.Entry(win, textvariable=self._voice_var, width=35).grid(row=0, column=1, padx=10, pady=12)

        ttk.Label(win, text="Playback Speed:").grid(row=1, column=0, padx=10, pady=8, sticky=tk.W)
        ttk.Combobox(win, textvariable=self._rate_var,
                      values=['-20%', '-10%', '+0%', '+10%', '+20%'],
                      width=10, state='readonly').grid(row=1, column=1, padx=10, pady=8)

        var = tk.BooleanVar(value=self._auto_resume)
        ttk.Checkbutton(win, text="Auto-resume last position on next open", variable=var,
                         command=lambda: setattr(self, '_auto_resume', var.get())).grid(row=2, column=0, columnspan=2, pady=10)

        ttk.Button(win, text="Save & Close", command=win.destroy).grid(row=3, column=0, columnspan=2, pady=10)

    def _show_help(self):
        msg = (
            "Keyboard Shortcuts:\n\n"
            "  Ctrl+O    Open file\n"
            "  Space     Play / Pause / Resume\n"
            "  ->        Next segment\n"
            "  <-        Previous segment\n"
            "  B         Toggle bookmark at current segment\n\n"
            "Bookmarks are saved per-file and persist between sessions.\n"
            "The reader will auto-resume from the last played position."
        )
        messagebox.showinfo("Keyboard Shortcuts", msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def destroy(self):
        self._tts.stop()
        self._player.stop()
        self._save_settings()
        super().destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = TTSReaderApp()

    # Keyboard shortcuts
    app.bind('<Control-o>', lambda e: app._open_file())
    app.bind('<Control-O>', lambda e: app._open_file())
    app.bind('<space>', lambda e: app._toggle_play())
    app.bind('<Right>', lambda e: app._next_seg())
    app.bind('<Left>', lambda e: app._prev_seg())
    app.bind('<b>', lambda e: app._toggle_bookmark())
    app.bind('<B>', lambda e: app._toggle_bookmark())

    app.mainloop()


if __name__ == '__main__':
    main()

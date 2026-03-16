"""
gui.py — Desktop control panel
  • Start/stop bot
  • Process session (diarization + transcription)
  • View logs (HTML + TXT)
  • Search across sessions
"""

import logging
import os
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from pathlib import Path

log = logging.getLogger(__name__)
from datetime import datetime

from db import init_db, get_sessions, get_segments, search, get_conn

# ── Theme colours (dark) ──────────────────────────────────────────────────────
BG      = "#1e1e2e"
BG2     = "#313244"
ACCENT  = "#cba6f7"
FG      = "#cdd6f4"
FG2     = "#6c7086"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"

SPEAKER_COLORS = ["#4F8EF7","#E06C75","#56B6C2","#E5C07B",
                  "#98C379","#C678DD","#61AFEF","#D19A66"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Discord Transcriber")
        self.geometry("1100x720")
        self.configure(bg=BG)
        self.resizable(True, True)
        init_db()
        self._bot_thread = None
        self._bot_discord = None  # reference to the discord Bot instance
        self._build_ui()
        self._refresh_sessions()
        if os.getenv("DISCORD_TOKEN", ""):
            self.after(500, self._safe_start_bot)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._style()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_sessions  = ttk.Frame(self.notebook, style="Dark.TFrame")
        self.tab_process   = ttk.Frame(self.notebook, style="Dark.TFrame")
        self.tab_log       = ttk.Frame(self.notebook, style="Dark.TFrame")
        self.tab_search    = ttk.Frame(self.notebook, style="Dark.TFrame")
        self.tab_bot       = ttk.Frame(self.notebook, style="Dark.TFrame")

        self.notebook.add(self.tab_sessions, text="  📁 Sessions  ")
        self.notebook.add(self.tab_process,  text="  ⚙️ Processing  ")
        self.notebook.add(self.tab_log,      text="  📄 Log  ")
        self.notebook.add(self.tab_search,   text="  🔍 Search  ")
        self.notebook.add(self.tab_bot,      text="  🤖 Bot  ")

        self._build_sessions_tab()
        self._build_process_tab()
        self._build_log_tab()
        self._build_search_tab()
        self._build_bot_tab()

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Dark.TFrame",      background=BG)
        s.configure("Dark.TLabel",      background=BG,  foreground=FG, font=("Segoe UI", 10))
        s.configure("Title.TLabel",     background=BG,  foreground=ACCENT, font=("Segoe UI", 12, "bold"))
        s.configure("Dark.TButton",     background=BG2, foreground=FG, font=("Segoe UI", 10))
        s.configure("Accent.TButton",   background=ACCENT, foreground=BG, font=("Segoe UI", 10, "bold"))
        s.configure("Dark.TEntry",      fieldbackground=BG2, foreground=FG, insertcolor=FG)
        s.configure("Dark.TCombobox",   fieldbackground=BG2, foreground=FG)
        s.configure("Dark.Treeview",    background=BG2, foreground=FG,
                    fieldbackground=BG2, rowheight=26, font=("Segoe UI", 10))
        s.configure("Dark.Treeview.Heading", background=BG, foreground=ACCENT,
                    font=("Segoe UI", 10, "bold"))
        s.configure("TNotebook",        background=BG, borderwidth=0)
        s.configure("TNotebook.Tab",    background=BG2, foreground=FG2,
                    padding=[12, 6], font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
              background=[("selected", BG)],
              foreground=[("selected", ACCENT)])
        s.configure("green.Horizontal.TProgressbar", troughcolor=BG2, background=GREEN)

    # ── Sessions tab ──────────────────────────────────────────────────────────

    def _build_sessions_tab(self):
        f = self.tab_sessions
        ttk.Label(f, text="Recorded Sessions", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12,4))

        cols = ("id", "name", "started_at", "ended_at", "channel")
        self.sessions_tree = ttk.Treeview(f, columns=cols, show="headings", style="Dark.Treeview")
        for col, w, label in [
            ("id",         40, "ID"),
            ("name",      240, "Name"),
            ("started_at",160, "Started"),
            ("ended_at",  160, "Ended"),
            ("channel",   120, "Channel"),
        ]:
            self.sessions_tree.heading(col, text=label)
            self.sessions_tree.column(col, width=w, anchor="w")
        self.sessions_tree.pack(fill="both", expand=True, padx=12, pady=4)
        self.sessions_tree.bind("<Double-1>", lambda _: self._send_to_process())

        btns = ttk.Frame(f, style="Dark.TFrame")
        btns.pack(fill="x", padx=12, pady=8)
        ttk.Button(btns, text="🔄 Refresh",      style="Dark.TButton",
                   command=self._refresh_sessions).pack(side="left", padx=4)
        ttk.Button(btns, text="📄 Open Log",     style="Accent.TButton",
                   command=self._open_selected_log).pack(side="left", padx=4)
        ttk.Button(btns, text="⚙️ Process",      style="Dark.TButton",
                   command=self._send_to_process).pack(side="left", padx=4)

    def _refresh_sessions(self):
        self.sessions_tree.delete(*self.sessions_tree.get_children())
        for row in get_sessions():
            self.sessions_tree.insert("", "end", values=(
                row["id"], row["name"],
                row["started_at"][:16] if row["started_at"] else "",
                row["ended_at"][:16]   if row["ended_at"]   else "—",
                row["channel"] or ""
            ))

    def _selected_session_id(self):
        sel = self.sessions_tree.selection()
        if not sel:
            messagebox.showwarning("Selection", "Select a session from the list")
            return None
        return int(self.sessions_tree.item(sel[0])["values"][0])

    def _open_selected_log(self):
        sid = self._selected_session_id()
        if sid:
            self._load_log(sid)

    def _send_to_process(self):
        sid = self._selected_session_id()
        if sid:
            self.notebook.select(self.tab_process)
            self._process_session_id.set(sid)
            with get_conn() as conn:
                row = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
            if row:
                wav = Path("data/recordings") / row["name"] / "mixed_mono.wav"
                if wav.exists():
                    self._audio_path.set(str(wav))

    # ── Process tab ───────────────────────────────────────────────────────────

    def _build_process_tab(self):
        f = self.tab_process
        ttk.Label(f, text="Process Session", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12,4))

        row1 = ttk.Frame(f, style="Dark.TFrame")
        row1.pack(fill="x", padx=12, pady=4)
        ttk.Label(row1, text="Audio file (mixed_mono.wav):", style="Dark.TLabel").pack(side="left")
        self._audio_path = tk.StringVar()
        ttk.Entry(row1, textvariable=self._audio_path, width=50, style="Dark.TEntry").pack(side="left", padx=6)
        ttk.Button(row1, text="📂 Browse", style="Dark.TButton",
                   command=self._browse_audio).pack(side="left")

        row2 = ttk.Frame(f, style="Dark.TFrame")
        row2.pack(fill="x", padx=12, pady=4)
        ttk.Label(row2, text="Session ID:", style="Dark.TLabel").pack(side="left")
        self._process_session_id = tk.IntVar(value=0)
        ttk.Entry(row2, textvariable=self._process_session_id, width=6, style="Dark.TEntry").pack(side="left", padx=6)

        self._progress_var = tk.DoubleVar(value=0)
        self._progress_label = ttk.Label(f, text="", style="Dark.TLabel")
        self._progress_label.pack(anchor="w", padx=12)
        ttk.Progressbar(f, variable=self._progress_var, maximum=100,
                        style="green.Horizontal.TProgressbar",
                        length=600).pack(padx=12, pady=4, anchor="w")

        self._process_btn = ttk.Button(f, text="▶ Start Processing",
                                       style="Accent.TButton",
                                       command=self._start_processing)
        self._process_btn.pack(padx=12, pady=8, anchor="w")

        self._process_log = scrolledtext.ScrolledText(
            f, height=16, bg=BG2, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat"
        )
        self._process_log.pack(fill="both", expand=True, padx=12, pady=4)

    def _browse_audio(self):
        path = filedialog.askopenfilename(
            title="Select a WAV file",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
            initialdir="data/recordings"
        )
        if path:
            self._audio_path.set(path)

    def _start_processing(self):
        audio = self._audio_path.get().strip()
        sid   = self._process_session_id.get()

        if not audio or not Path(audio).exists():
            messagebox.showerror("Error", "Specify an existing WAV file")
            return
        if not sid:
            messagebox.showerror("Error", "Specify a Session ID")
            return

        self._process_btn.config(state="disabled")
        self._process_log.delete("1.0", "end")
        self._progress_var.set(0)

        def run():
            from processor import process_session, export_txt, export_html
            out_dir = Path(audio).parent

            def progress(step, pct):
                def _update():
                    self._progress_var.set(pct)
                    self._progress_label.config(text=step)
                    self._process_log.insert("end", f"[{pct:3d}%] {step}\n")
                    self._process_log.see("end")
                self.after(0, _update)

            try:
                segments = process_session(audio, sid, progress_cb=progress)

                # export
                txt_path  = out_dir / "transcript.txt"
                html_path = out_dir / "transcript.html"
                export_txt(segments, str(txt_path))
                with get_conn() as conn:
                    name = conn.execute(
                        "SELECT name FROM sessions WHERE id=?", (sid,)
                    ).fetchone()["name"]
                export_html(segments, str(html_path), session_name=name)

                self._process_log.insert("end",
                    f"\n✅ Done! Exported:\n  {txt_path}\n  {html_path}\n"
                )
                self._process_log.see("end")
                self._refresh_sessions()
                self._load_log(sid)

            except Exception as e:
                log.exception("Processing failed for session %s", sid)
                self._process_log.insert("end", f"\n❌ Error: {e}\n")
            finally:
                self._process_btn.config(state="normal")

        threading.Thread(target=run, daemon=True).start()

    # ── Log tab ───────────────────────────────────────────────────────────────

    def _build_log_tab(self):
        f = self.tab_log
        ttk.Label(f, text="Session Log", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12,4))

        top = ttk.Frame(f, style="Dark.TFrame")
        top.pack(fill="x", padx=12, pady=4)
        ttk.Label(top, text="Session ID:", style="Dark.TLabel").pack(side="left")
        self._log_sid = tk.IntVar(value=0)
        ttk.Entry(top, textvariable=self._log_sid, width=6, style="Dark.TEntry").pack(side="left", padx=6)
        ttk.Button(top, text="📄 Load",         style="Accent.TButton",
                   command=lambda: self._load_log(self._log_sid.get())).pack(side="left", padx=4)
        ttk.Button(top, text="🌐 Open HTML in browser", style="Dark.TButton",
                   command=self._open_html).pack(side="left", padx=4)

        self._log_text = scrolledtext.ScrolledText(
            f, bg=BG2, fg=FG, insertbackground=FG,
            font=("Consolas", 10), relief="flat"
        )
        self._log_text.pack(fill="both", expand=True, padx=12, pady=4)
        # tag colors per speaker slot
        for i, color in enumerate(SPEAKER_COLORS):
            self._log_text.tag_config(f"sp{i}", foreground=color)

        self._current_html_path = None

    def _load_log(self, session_id: int):
        if not session_id:
            return
        self._log_sid.set(session_id)
        segs = get_segments(session_id)
        self._log_text.delete("1.0", "end")

        speakers = sorted(set(s["speaker"] for s in segs))
        sp_tag   = {sp: f"sp{i % len(SPEAKER_COLORS)}" for i, sp in enumerate(speakers)}

        from processor import fmt_time
        for s in segs:
            time_str = f"[{fmt_time(s['start_sec'])}]  "
            name_str = f"{s['speaker']}: "
            text_str = f"{s['text']}\n"
            self._log_text.insert("end", time_str, "dim")
            self._log_text.insert("end", name_str, sp_tag[s["speaker"]])
            self._log_text.insert("end", text_str)

        self._log_text.tag_config("dim", foreground=FG2)

        # store html path for browser button
        with get_conn() as conn:
            row = conn.execute("SELECT name FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row:
            self._current_html_path = (Path(__file__).parent / "data/recordings" / row["name"] / "transcript.html").resolve()

    def _open_html(self):
        if self._current_html_path and self._current_html_path.exists():
            webbrowser.open(self._current_html_path.as_uri())
        else:
            messagebox.showinfo("HTML", "HTML file not found. Run processing first.")

    # ── Search tab ────────────────────────────────────────────────────────────

    def _build_search_tab(self):
        f = self.tab_search
        ttk.Label(f, text="Search Across All Sessions", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12,4))

        row = ttk.Frame(f, style="Dark.TFrame")
        row.pack(fill="x", padx=12, pady=4)
        self._search_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self._search_var, width=44, style="Dark.TEntry", font=("Segoe UI", 11))
        e.pack(side="left", padx=(0,8))
        e.bind("<Return>", lambda _: self._do_search())
        ttk.Button(row, text="🔍 Find", style="Accent.TButton", command=self._do_search).pack(side="left")

        cols = ("session", "time", "speaker", "text")
        self.search_tree = ttk.Treeview(f, columns=cols, show="headings", style="Dark.Treeview")
        for col, w, label in [
            ("session",  180, "Session"),
            ("time",      80, "Time"),
            ("speaker",  120, "Speaker"),
            ("text",     600, "Text"),
        ]:
            self.search_tree.heading(col, text=label)
            self.search_tree.column(col, width=w, anchor="w")
        self.search_tree.pack(fill="both", expand=True, padx=12, pady=4)

        self._search_count = ttk.Label(f, text="", style="Dark.TLabel")
        self._search_count.pack(anchor="w", padx=12)

    def _do_search(self):
        q = self._search_var.get().strip()
        if not q:
            return
        from processor import fmt_time
        results = search(q)
        self.search_tree.delete(*self.search_tree.get_children())
        for r in results:
            self.search_tree.insert("", "end", values=(
                r["session_name"],
                fmt_time(r["start_sec"]),
                r["speaker"],
                r["text"]
            ))
        self._search_count.config(text=f"Found: {len(results)} results")

    # ── Bot tab ───────────────────────────────────────────────────────────────

    def _build_bot_tab(self):
        f = self.tab_bot
        ttk.Label(f, text="Discord Bot", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12,4))

        row = ttk.Frame(f, style="Dark.TFrame")
        row.pack(fill="x", padx=12, pady=4)
        ttk.Label(row, text="Discord Token:", style="Dark.TLabel").pack(side="left")
        self._token_var = tk.StringVar(value=os.getenv("DISCORD_TOKEN",""))
        ttk.Entry(row, textvariable=self._token_var, width=55, show="•", style="Dark.TEntry").pack(side="left", padx=6)

        row2 = ttk.Frame(f, style="Dark.TFrame")
        row2.pack(fill="x", padx=12, pady=4)
        self._bot_status = ttk.Label(row2, text="● Stopped", style="Dark.TLabel", foreground=RED)
        self._bot_status.pack(side="left", padx=(0,12))
        self._start_bot_btn = ttk.Button(row2, text="▶ Start Bot",
                                         style="Accent.TButton",
                                         command=self._start_bot)
        self._start_bot_btn.pack(side="left", padx=4)

        ttk.Label(f, text="Bot commands in Discord:", style="Dark.TLabel").pack(anchor="w", padx=12, pady=(16,4))
        cmds = (
            ("/join",          "Join a voice channel and start recording"),
            ("/leave",         "Stop recording and save files (returns session ID)"),
            ("/sessions",      "List all sessions for this server with ID and date"),
            ("/transcript",    "Get transcript for last session (or /transcript <id>)"),
            ("/save N",        "Save the last N minutes as an audio file (1–10)"),
        )
        for cmd, desc in cmds:
            row = ttk.Frame(f, style="Dark.TFrame")
            row.pack(fill="x", padx=24, pady=2)
            ttk.Label(row, text=cmd,  width=12, style="Dark.TLabel", foreground=ACCENT).pack(side="left")
            ttk.Label(row, text=desc, style="Dark.TLabel", foreground=FG2).pack(side="left")

        self._bot_log = scrolledtext.ScrolledText(
            f, height=10, bg=BG2, fg=FG, insertbackground=FG,
            font=("Consolas", 9), relief="flat"
        )
        self._bot_log.pack(fill="both", expand=True, padx=12, pady=(16,4))

        # redirect bot stdout to log widget
        self._orig_stdout = sys.stdout

    def _safe_start_bot(self):
        try:
            self._start_bot()
        except Exception:
            log.exception("Auto-start bot failed")

    def _start_bot(self):
        token = self._token_var.get().strip()
        if not token:
            messagebox.showerror("Error", "Enter a Discord Bot Token")
            return

        # Stop any previously running bot thread before starting a new one
        if self._bot_thread and self._bot_thread.is_alive():
            if self._bot_discord is not None:
                try:
                    import asyncio
                    loop = self._bot_discord.loop
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._bot_discord.close(), loop
                        )
                except Exception:
                    pass
            self._bot_thread.join(timeout=5)
            if self._bot_thread.is_alive():
                log.warning("Old bot thread did not stop cleanly — process restart recommended")

        self._start_bot_btn.config(state="disabled")
        self._bot_status.config(text="● Starting...", foreground=YELLOW)

        import importlib
        import bot as discord_bot
        importlib.reload(discord_bot)

        def session_started(sid, name):
            def _update():
                self._bot_log.insert("end", f"🎙️ Session started: {name} (ID={sid})\n")
                self._bot_log.see("end")
            self.after(0, _update)

        def session_stopped(sid, wav_path, speaker_map):
            def _update():
                self._bot_log.insert("end", f"⏹️ Session {sid} finished. WAV: {wav_path}\n")
                self._bot_log.see("end")
                self._audio_path.set(wav_path)
                self._process_session_id.set(sid)
                self._refresh_sessions()
            self.after(0, _update)

        discord_bot.on_session_started = session_started
        discord_bot.on_session_stopped = session_stopped

        def run():
            try:
                self._bot_discord = discord_bot.bot
                self.after(0, lambda: self._bot_status.config(
                    text="● Running", foreground=GREEN))
                discord_bot.run_bot(token)
            except Exception as e:
                log.exception("Bot crashed")
                self.after(0, lambda: self._bot_log.insert("end", f"❌ {e}\n"))
                self.after(0, lambda: self._bot_status.config(
                    text="● Error", foreground=RED))
                self.after(0, lambda: self._start_bot_btn.config(state="normal"))
            finally:
                self._bot_discord = None

        self._bot_thread = threading.Thread(target=run, daemon=True)
        self._bot_thread.start()
        self._bot_log.insert("end", "Bot is starting...\n")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

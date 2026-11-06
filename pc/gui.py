"""
ReelDS - simple desktop GUI for the encoder.

Pick your video folders and the SD card, press Build. The encode runs reelds.py in a
child process (so the window stays responsive and Cancel can stop everything), and
the log, a progress bar and a preview of the DSi menu are shown as it goes.

Started by the launchers with no arguments (double-click reelds.cmd / ReelDS.command),
or with:  reelds.sh gui
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # system Python without Tk (bundled runtime always has it)
    sys.exit("The GUI needs Tkinter. Use reelds.cmd / reelds.sh (bundled Python), or install "
             "python3-tk, or run the command line: reelds.py build --help")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "reelds.py"

QUALITY = [("Small files", "low"), ("Balanced", "normal"), ("Best picture", "high")]
FPS = [("30 fps (smooth)", 30), ("24 fps", 24), ("15 fps (smallest)", 15)]
AUDIO = [("Good audio (32 kHz)", 32768), ("Smaller audio (16 kHz)", 16384)]


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reelds" / "gui.json"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.n_jobs = 0
        self.file_pct = {}
        self.preview_img = None
        self.preview_page = 0
        self.cancelled = False

        root.title("ReelDS")
        root.minsize(560, 760)
        self.cfg = self.load_cfg()
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)

    # ---------------------------------------------------------------- settings
    def load_cfg(self):
        try:
            return json.loads(config_path().read_text())
        except Exception:
            return {}

    def save_cfg(self):
        cfg = {"folders": list(self.folders.get(0, "end")), "games": list(self.games.get(0, "end")),
               "sd": self.sd.get(),
               "quality": self.quality.get(), "fps": self.fps.get(), "audio": self.audio.get(),
               "prune": self.prune.get()}
        try:
            config_path().parent.mkdir(parents=True, exist_ok=True)
            config_path().write_text(json.dumps(cfg, indent=1))
        except OSError:
            pass

    # ---------------------------------------------------------------- layout
    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        main = ttk.Frame(self.root, padding=6)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # 1. folders
        f1 = ttk.LabelFrame(main, text="1. Folders")
        f1.grid(row=0, column=0, sticky="nsew", **pad)
        f1.columnconfigure(0, weight=1)
        ttk.Label(f1, text="Videos  (sub-folders become rows, show folders become pages)").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        self.folders = tk.Listbox(f1, height=4, activestyle="none", selectmode="extended")
        self.folders.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(8, 4), pady=(2, 6))
        for f in self.cfg.get("folders", []):
            self.folders.insert("end", f)
        ttk.Button(f1, text="Add folder…", command=self.add_folder).grid(row=1, column=1, sticky="ew", padx=8, pady=(2, 2))
        ttk.Button(f1, text="Remove", command=self.remove_folder).grid(row=2, column=1, sticky="new", padx=8, pady=2)
        ttk.Label(f1, text="Games  (.nds files; folders already on the SD card are used in place)").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))
        self.games = tk.Listbox(f1, height=2, activestyle="none", selectmode="extended")
        self.games.grid(row=4, column=0, rowspan=2, sticky="nsew", padx=(8, 4), pady=(2, 8))
        for f in self.cfg.get("games", []):
            self.games.insert("end", f)
        ttk.Button(f1, text="Add folder…", command=self.add_games).grid(row=4, column=1, sticky="ew", padx=8, pady=(2, 2))
        ttk.Button(f1, text="Remove", command=self.remove_games).grid(row=5, column=1, sticky="new", padx=8, pady=(2, 8))

        # 2. SD card
        f2 = ttk.LabelFrame(main, text="2. SD card (or any folder you'll copy to it)")
        f2.grid(row=1, column=0, sticky="ew", **pad)
        f2.columnconfigure(0, weight=1)
        self.sd = tk.StringVar(value=self.cfg.get("sd", ""))
        ttk.Entry(f2, textvariable=self.sd).grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)
        ttk.Button(f2, text="Browse…", command=self.pick_sd).grid(row=0, column=1, padx=8, pady=8)

        # 3. options
        f3 = ttk.LabelFrame(main, text="3. Options")
        f3.grid(row=2, column=0, sticky="ew", **pad)
        self.quality = tk.StringVar(value=self.cfg.get("quality", "normal"))
        self.fps = tk.IntVar(value=self.cfg.get("fps", 30))
        self.audio = tk.IntVar(value=self.cfg.get("audio", 32768))
        self.prune = tk.BooleanVar(value=self.cfg.get("prune", True))
        self.force = tk.BooleanVar(value=False)
        self._combo(f3, "Quality", QUALITY, self.quality, 0)
        self._combo(f3, "Frame rate", FPS, self.fps, 1)
        self._combo(f3, "Audio", AUDIO, self.audio, 2)
        ttk.Checkbutton(f3, text="Delete videos from the SD card that are no longer in my folders",
                        variable=self.prune).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))
        ttk.Checkbutton(f3, text="Re-encode everything (only needed after changing options)",
                        variable=self.force).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 8))

        # 4. kid settings (settings.ini on the card; takes effect next time ReelDS starts)
        f4 = ttk.LabelFrame(main, text="4. Kid settings  (saved to the SD card right away, no rebuild needed)")
        f4.grid(row=3, column=0, sticky="ew", **pad)
        self.kid = {k: tk.BooleanVar(value=v) for k, v in (("autoplay", True), ("lock_buttons", False),
                                                           ("lock_touch", False), ("shuffle_tile", True),
                                                           ("show_games", True))}
        for row, (key, text) in enumerate((
                ("autoplay", "Play the next episode automatically"),
                ("lock_buttons", "Lock buttons while playing: only A (play/pause) works\n"
                                 "      grown-ups hold START + SELECT for 4 seconds to go back to the menu"),
                ("lock_touch", "Lock the touchscreen while playing"),
                ("shuffle_tile", "Show a Shuffle tile (plays random videos; random episodes inside a show)"),
                ("show_games", "Show games"))):
            ttk.Checkbutton(f4, text=text, variable=self.kid[key], command=self.save_kid).grid(
                row=row, column=0, sticky="w", padx=8, pady=(6 if row == 0 else 2, 8 if row == 4 else 2))
        self.sd.trace_add("write", lambda *_: self.load_kid())
        self.load_kid()

        # buttons
        bar = ttk.Frame(main)
        bar.grid(row=4, column=0, sticky="ew", **pad)
        self.build_btn = ttk.Button(bar, text="Build SD card", command=self.start_build)
        self.build_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.preview_btn = ttk.Button(bar, text="Preview DSi menu", command=self.open_preview)
        self.preview_btn.pack(side="right")
        self.test_btn = ttk.Button(bar, text="Check setup", command=self.start_selftest)
        self.test_btn.pack(side="right", padx=6)

        # progress + log
        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", padx=10, pady=(4, 0))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(main, textvariable=self.status).grid(row=6, column=0, sticky="w", padx=10)
        logf = ttk.Frame(main)
        logf.grid(row=7, column=0, sticky="nsew", padx=10, pady=(4, 8))
        main.rowconfigure(7, weight=1)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, height=8, wrap="none", state="disabled", font=("TkFixedFont", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = sb.set

    def _combo(self, parent, label, choices, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=3)
        names = [n for n, _ in choices]
        values = [v for _, v in choices]
        shown = tk.StringVar(value=names[values.index(var.get())] if var.get() in values else names[0])
        cb = ttk.Combobox(parent, textvariable=shown, values=names, state="readonly", width=26)
        cb.grid(row=row, column=1, sticky="w", padx=8, pady=3)
        cb.bind("<<ComboboxSelected>>", lambda _e: var.set(values[names.index(shown.get())]))
        var.set(values[names.index(shown.get())])

    # ---------------------------------------------------------------- kid settings
    def _reelds(self):
        sys.path.insert(0, str(HERE))
        import reelds
        return reelds

    def load_kid(self):
        sd = self.sd.get().strip()
        if sd and Path(sd).is_dir():
            for k, v in self._reelds().read_settings(sd).items():
                if k in self.kid:           # skip non-checkbox entries such as reelds_path
                    self.kid[k].set(v)

    def save_kid(self):
        sd = self.sd.get().strip()
        if not sd or not Path(sd).is_dir():
            self.status.set("Choose the SD card first - kid settings are saved on it.")
            return
        try:
            self._reelds().write_settings(sd, **{k: v.get() for k, v in self.kid.items()})
            on = [n for k, n in (("autoplay", "autoplay"), ("lock_buttons", "buttons locked"),
                                 ("lock_touch", "touch locked"), ("shuffle_tile", "shuffle tile"),
                                 ("show_games", "games shown"))
                  if self.kid[k].get()]
            self.status.set("Saved to the SD card: " + (", ".join(on) if on else "all kid settings off") + ".")
        except OSError as exc:
            self.status.set(f"Could not save settings: {exc}")

    # ---------------------------------------------------------------- actions
    def add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of videos")
        if d and d not in self.folders.get(0, "end"):
            self.folders.insert("end", d)
            self.save_cfg()

    def remove_folder(self):
        for i in reversed(self.folders.curselection()):
            self.folders.delete(i)
        self.save_cfg()

    def add_games(self):
        d = filedialog.askdirectory(title="Choose a folder of .nds games")
        if d and d not in self.games.get(0, "end"):
            self.games.insert("end", d)
            self.save_cfg()

    def remove_games(self):
        for i in reversed(self.games.curselection()):
            self.games.delete(i)
        self.save_cfg()

    def pick_sd(self):
        d = filedialog.askdirectory(title="Choose the SD card")
        if d:
            self.sd.set(d)
            self.save_cfg()

    def write_log(self, text):
        self.log["state"] = "normal"
        self.log.insert("end", text)
        self.log.see("end")
        self.log["state"] = "disabled"

    def run(self, args, title):
        if self.proc:
            return
        self.save_cfg()
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"
        self.progress["value"] = 0
        self.n_jobs, self.file_pct = 0, {}
        self.cancelled = False
        self.status.set(title)
        for b in (self.build_btn, self.test_btn):
            b["state"] = "disabled"
        self.cancel_btn["state"] = "normal"

        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-u", str(SCRIPT), *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.lines.put(line)
        proc.wait()
        self.lines.put(None)

    def start_build(self):
        folders = list(self.folders.get(0, "end"))
        games = list(self.games.get(0, "end"))
        sd = self.sd.get().strip()
        if not folders and not games:
            messagebox.showinfo("ReelDS", "Add at least one folder of videos or games first.")
            return
        if not sd or not Path(sd).is_dir():
            messagebox.showinfo("ReelDS", "Choose the SD card (or an output folder) first.")
            return
        args = ["build", *folders, "--out", sd, "--quality", self.quality.get(),
                "--fps", str(self.fps.get()), "--audio-rate", str(self.audio.get())]
        if games:
            args += ["--games", *games]
        if self.prune.get():
            args.append("--prune")
        if self.force.get():
            args.append("--force")
        self.run(args, "Scanning folders…")

    def start_selftest(self):
        self.run(["selftest"], "Checking setup…")

    def cancel(self):
        p = self.proc
        if not p:
            return
        self.cancelled = True
        self.status.set("Cancelling…")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            p.kill()

    def on_close(self):
        if self.proc and not messagebox.askyesno("ReelDS", "An encode is running. Stop it and quit?"):
            return
        self.cancel()
        self.save_cfg()
        self.root.destroy()

    # ---------------------------------------------------------------- output parsing
    def pump(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.finished()
                    break
                self.write_log(line)
                self.parse(line)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def parse(self, line):
        m = re.match(r"encoding (\d+) video", line)
        if m:
            self.n_jobs = int(m.group(1))
            self.status.set(f"Encoding {self.n_jobs} video(s)…")
            return
        m = re.match(r"\s*\[(.+?)\] (\d+)%", line)
        if m:
            self.file_pct[m.group(1)] = int(m.group(2))
        m = re.match(r"\s*\[(.+?)\] done:", line)
        if m:
            self.file_pct[m.group(1)] = 100
        m = re.match(r"\s*!! failed: (.+?):", line)
        if m:
            self.file_pct[Path(m.group(1)).stem[:40]] = 100
        if self.n_jobs and self.file_pct:
            pct = sum(self.file_pct.values()) / self.n_jobs
            self.progress["value"] = min(99, pct)
            done = sum(1 for v in self.file_pct.values() if v >= 100)
            self.status.set(f"Encoding… {done} of {self.n_jobs} done ({pct:.0f}%)")
        if line.startswith("catalog:"):
            self.status.set("Writing menu…")

    def finished(self):
        rc = self.proc.returncode if self.proc else -1
        self.proc = None
        for b in (self.build_btn, self.test_btn):
            b["state"] = "normal"
        self.cancel_btn["state"] = "disabled"
        if rc == 0:
            self.progress["value"] = 100
            last = self.log.get("end-2l", "end-1c").strip()
            self.status.set("Done. " + (last if last.startswith(("catalog", "selftest")) else ""))
            if self.catalog_file().exists():
                self.refresh_preview()
        else:
            self.status.set("Cancelled." if self.cancelled else f"Failed (exit code {rc}) - see the log.")

    # ---------------------------------------------------------------- DSi menu preview
    def catalog_file(self) -> Path:
        return Path(self.sd.get()) / "reelds" / "catalog.bin"

    def open_preview(self):
        if not self.catalog_file().exists():
            messagebox.showinfo("ReelDS", "Build the SD card first - the preview shows its menu.")
            return
        if getattr(self, "pwin", None) and self.pwin.winfo_exists():
            self.pwin.lift()
        else:
            self.pwin = tk.Toplevel(self.root)
            self.pwin.title("DSi menu preview")
            self.pwin.resizable(False, False)
            nav = ttk.Frame(self.pwin, padding=4)
            nav.pack(fill="x")
            ttk.Button(nav, text="< Prev", command=lambda: self.step_page(-1)).pack(side="left")
            self.page_lbl = ttk.Label(nav, width=16, anchor="center")
            self.page_lbl.pack(side="left", expand=True)
            ttk.Button(nav, text="Next >", command=lambda: self.step_page(1)).pack(side="right")
            self.pcanvas = ttk.Label(self.pwin)
            self.pcanvas.pack(padx=6, pady=(0, 6))
        self.refresh_preview()

    def step_page(self, d):
        self.preview_page += d
        self.refresh_preview()

    def refresh_preview(self):
        if not (getattr(self, "pwin", None) and self.pwin.winfo_exists()):
            return
        try:
            sys.path.insert(0, str(HERE))
            import reelds  # same folder; only used for rendering the mock
            n = len(reelds.load_catalog(self.catalog_file())["pages"])
            self.preview_page %= n
            scale = 2 if self.root.winfo_screenheight() >= 950 else 1
            im = reelds.render_mock(self.sd.get(), page=self.preview_page, scale=scale)
            import base64
            import io
            buf = io.BytesIO()
            im.save(buf, "PNG")
            self.preview_img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
            self.pcanvas["image"] = self.preview_img
            self.page_lbl["text"] = "Home" if self.preview_page == 0 else f"Show {self.preview_page} of {n - 1}"
        except Exception as exc:
            self.page_lbl["text"] = f"(preview failed: {exc})"


def main():
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else
                                  "aqua" if sys.platform == "darwin" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

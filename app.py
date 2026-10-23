"""NetUsage: per-app internet usage for Windows. Run: python app.py"""
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

import customtkinter as ctk
import matplotlib
import pystray
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from PIL import Image

import data

BG, PANEL, TEXT, MUTED, GRID = "#14171c", "#1c2129", "#e6e8eb", "#8b93a1", "#2a303a"
DOWN, UP = "#4cc3d9", "#e8a54b"
SHARE, TRACK, TIP = "#7aa2f7", "#262c36", "#2f3743"
FONT, DISPLAY = "Segoe UI Variable Text", "Segoe UI Variable Display"
matplotlib.rcParams.update({"font.family": "Segoe UI", "text.color": TEXT})
# PyInstaller unpacks bundled files to _MEIPASS; running from source, the icon sits next to this file
ICON = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "netusage.ico")
SHOW_FLAG = os.path.join(tempfile.gettempdir(), "netusage-show")  # a second launch drops this to wake the first


def already_running():
    """True if another NetUsage holds the app mutex. Two copies would record the same traffic twice."""
    global _mutex  # keep the handle for the life of the process
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # windll's GetLastError can be stale
    _mutex = kernel32.CreateMutexW(None, False, "Local\\NetUsage")
    # 183: already exists; 5: exists but owned by the elevated copy
    return ctypes.get_last_error() in (183, 5)


def ensure_admin():
    """Relaunch elevated (UAC prompt). Returns False if the user said no."""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return True
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        args = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    if ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), None, 1) > 32:
        sys.exit(0)
    return False


def chart(parent, w, h):
    fig = Figure(figsize=(w, h), dpi=100, facecolor=PANEL)
    canvas = FigureCanvasTkAgg(fig, master=parent)
    canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
    return fig, canvas


def style(ax, suffix=""):
    ax.set_facecolor(PANEL)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: data.fmt_bytes(v) + suffix if v else "0"))


def panel(parent):
    return ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=10)


def stat(parent, caption, color, size=34):
    box = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkLabel(box, text=caption, text_color=MUTED, font=(FONT, 13)).pack(anchor="w")
    value = ctk.CTkLabel(box, text="–", text_color=color, font=(DISPLAY, size, "bold"))
    value.pack(anchor="w")
    return box, value


class Table(ctk.CTkFrame):
    """Fixed-row table; set() rewrites the label texts instead of rebuilding widgets.
    With bars=True, column 1 is a bar showing each row's share."""

    def __init__(self, parent, headers, colors, rows, bars=False):
        super().__init__(parent, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.bars = []
        cols = list(headers)
        if bars:
            cols.insert(1, "")
            colors = (colors[0], None) + tuple(colors[1:])
        for c, h in enumerate(cols):
            ctk.CTkLabel(self, text=h, text_color=MUTED, font=(FONT, 12), anchor="w" if c == 0 else "e"
                         ).grid(row=0, column=c, sticky="ew", padx=(0, 16), pady=(0, 4))
        self.cells = []
        for r in range(1, rows + 1):
            row = []
            for c in range(len(cols)):
                if bars and c == 1:
                    bar = ctk.CTkProgressBar(self, width=90, height=6, corner_radius=3,
                                             fg_color=TRACK, progress_color=SHARE)
                    bar.grid(row=r, column=c, padx=(0, 16))
                    self.bars.append(bar)
                    continue
                cell = ctk.CTkLabel(self, text="", text_color=colors[c], font=(FONT, 13),
                                    anchor="w" if c == 0 else "e")
                cell.grid(row=r, column=c, sticky="ew", padx=(0, 16), pady=1)
                row.append(cell)
            self.cells.append(row)

    def set(self, rows, shares=()):
        for r, row in enumerate(self.cells):
            for c, cell in enumerate(row):
                cell.configure(text=rows[r][c] if r < len(rows) else "")
        for r, bar in enumerate(self.bars):
            if r < len(rows):
                bar.grid()
                bar.set(shares[r])
            else:
                bar.grid_remove()


class HistoryTab:
    """Totals, a bar chart and the app list for one period. load() returns data.history()-shaped dicts;
    step is how many bars share one axis label; every is how often (seconds) to redraw."""

    def __init__(self, tab, load, period, step, every=60):
        self.load, self.period, self.step, self.every = load, period, step, every
        self.h, self.selected, self.tip = None, None, None
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(4, 12))
        self.values = []
        for caption, color in (("Downloaded", DOWN), ("Uploaded", UP), ("Total", TEXT)):
            box, value = stat(top, caption, color, 30)
            box.pack(side="left", padx=(0, 48))
            self.values.append(value)

        mid = ctk.CTkFrame(tab, fg_color="transparent")
        mid.pack(fill="both", expand=True)
        mid.grid_columnconfigure(0, weight=3, uniform="panels")  # widths stay put whatever the list holds
        mid.grid_columnconfigure(1, weight=2, uniform="panels")
        mid.grid_rowconfigure(0, weight=1)
        left, right = panel(mid), panel(mid)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew")
        self.bar_fig, self.bar_canvas = chart(left, 6, 2.8)
        self.bar_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)
        self.bar_canvas.mpl_connect("motion_notify_event", self.on_hover)
        self.bar_canvas.mpl_connect("button_press_event", self.on_click)

        self.caption = ctk.CTkLabel(right, text="", text_color=TEXT, font=(FONT, 14, "bold"))
        self.caption.pack(anchor="w", padx=16, pady=(14, 0))
        self.hint = ctk.CTkLabel(right, text="", text_color=MUTED, font=(FONT, 12))
        self.hint.pack(anchor="w", padx=16, pady=(0, 8))
        self.table = Table(right, ("App", "Download", "Upload"), (TEXT, DOWN, UP), 14, bars=True)
        self.table.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def span(self, i):
        """Bar i in words, e.g. '13:00–14:00' or 'Oct 07'."""
        return self.h["spans"][i]

    def draw(self):
        h = self.load()
        if self.selected is not None:  # bars shift as time passes; keep the selection on the same time
            label = self.h["labels"][self.selected]
            self.selected = h["labels"].index(label) if label in h["labels"] else None
        self.h = h
        self.n = len(h["labels"])
        down, up = sum(h["recv"]), sum(h["sent"])
        for label, v in zip(self.values, (down, up, down + up)):
            label.configure(text=data.fmt_bytes(v))

        self.bar_fig.clear()
        ax = self.bar_fig.add_subplot()
        style(ax)
        x = range(self.n)
        self.down_bars = ax.bar(x, h["recv"], color=DOWN, width=0.7, label="Download")
        self.up_bars = ax.bar(x, h["sent"], bottom=h["recv"], color=UP, width=0.7, label="Upload")
        ax.set_xticks(list(x)[::self.step], h["labels"][::self.step])
        ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=MUTED)
        # headroom above the tallest bar so its hover box fits
        ax.set_ylim(0, max(r + s for r, s in zip(h["recv"], h["sent"])) * 1.3 or 1024 ** 2)
        if not down + up:
            ax.text(0.5, 0.5, "No usage recorded for this period yet", transform=ax.transAxes,
                    ha="center", color=MUTED, fontsize=10)
        self.tip = ax.annotate("", (0, 0), xytext=(0, 8), textcoords="offset points", fontsize=8,
                               color=TEXT, bbox={"boxstyle": "round,pad=0.5", "fc": TIP, "ec": "none"},
                               annotation_clip=False, visible=False)
        self.bar_fig.tight_layout()
        self.show_selection()

    def bar_at(self, event):
        if self.h is None or event.inaxes is None or event.xdata is None:
            return None
        i = round(event.xdata)
        return i if 0 <= i < self.n and abs(event.xdata - i) <= 0.4 else None

    def on_hover(self, event):
        i = self.bar_at(event)
        if i is None:
            if self.tip and self.tip.get_visible():
                self.tip.set_visible(False)
                self.bar_canvas.draw_idle()
            return
        r, s = self.h["recv"][i], self.h["sent"][i]
        apps = self.h["buckets"][i]
        lines = [self.span(i), f"Download {data.fmt_bytes(r)}   Upload {data.fmt_bytes(s)}"]
        if apps:
            lines.append(f"Mostly {apps[0][0]} ({data.fmt_bytes(apps[0][1] + apps[0][2])})")
        self.tip.xy = (i, r + s)
        self.tip.set_text("\n".join(lines))
        self.tip.set_horizontalalignment("left" if i < self.n * 0.2 else "right" if i > self.n * 0.8 else "center")
        self.tip.set_visible(True)
        self.bar_canvas.draw_idle()

    def on_click(self, event):
        i = self.bar_at(event)
        self.selected = None if i is None or i == self.selected else i
        self.show_selection()

    def show_selection(self):
        sel = self.selected
        for i, (d, u) in enumerate(zip(self.down_bars, self.up_bars)):
            d.set_alpha(1 if sel is None or i == sel else 0.3)
            u.set_alpha(1 if sel is None or i == sel else 0.3)
        self.bar_canvas.draw_idle()

        apps = self.h["apps"] if sel is None else self.h["buckets"][sel]
        self.caption.configure(text=f"Apps, {self.period if sel is None else self.span(sel)}")
        if sel is None:
            self.hint.configure(text="Click a bar to see the apps behind it")
        else:
            self.hint.configure(text="Click the bar again to see the whole period" if apps
                                else "Nothing was recorded then")
        apps = apps[:14]
        biggest = max((r + s for _, r, s in apps), default=0) or 1
        self.table.set([(a if len(a) <= 30 else a[:29] + "…", data.fmt_bytes(r), data.fmt_bytes(s))
                        for a, r, s in apps],
                       [(r + s) / biggest for _, r, s in apps])


class App(ctk.CTk):
    def __init__(self, admin, hidden=False):
        super().__init__(fg_color=BG)
        self.admin = admin
        self.title("NetUsage")
        self.geometry("1180x760")
        self.minsize(980, 640)
        if os.path.exists(ICON):
            self.after(250, lambda: self.iconbitmap(ICON))  # CTk sets its own icon at ~200 ms; ours wins
        self._import_msg = None
        self._ticks = 0

        tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_fg_color=PANEL,
                              segmented_button_selected_color="#2f3743",
                              segmented_button_selected_hover_color="#38414f",
                              segmented_button_unselected_color=PANEL, text_color=TEXT,
                              command=lambda: self.draw_history())
        tabs.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self.tabs = tabs
        self.build_live(tabs.add("Live"))
        self.history = {
            # from the sampler's memory, so it only covers time since NetUsage started
            "5 minutes": HistoryTab(tabs.add("5 minutes"), lambda: self.sampler.recent(), "last 5 minutes", 6, every=5),
            "24 hours": HistoryTab(tabs.add("24 hours"), lambda: data.history(24, "hour"), "last 24 hours", 3),
            "7 days": HistoryTab(tabs.add("7 days"), lambda: data.history(7, "day"), "last 7 days", 1),
            "30 days": HistoryTab(tabs.add("30 days"), lambda: data.history(30, "day"), "last 30 days", 5),
        }

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=10)
        self.status = ctk.CTkLabel(bar, text="", text_color=MUTED, font=(FONT, 12))
        self.status.pack(side="left")
        self.refresh_btn = ctk.CTkButton(bar, text="Scan again", width=140, fg_color="#2f3743",
                                         hover_color="#38414f", command=self.refresh_history)
        self.refresh_btn.pack(side="right")

        self.sampler = data.LiveSampler()
        self.sampler.start()
        if admin:
            self.refresh_history()
        else:
            self.status.configure(text="Not running as administrator, so per-app data and Windows "
                                       "history are unavailable. Restart and accept the prompt.")
            self.refresh_btn.configure(state="disabled")
        self.draw_history()
        self.after(1000, self.tick)

        self._told_about_tray = hidden
        self.tray = pystray.Icon("NetUsage", Image.open(ICON), "NetUsage: recording internet usage",
                                 menu=pystray.Menu(
                                     pystray.MenuItem("Open NetUsage", lambda: self.after(0, self.show), default=True),
                                     pystray.MenuItem("Quit", lambda: self.after(0, self.quit_app))))
        self.tray.run_detached()
        self.protocol("WM_DELETE_WINDOW", self.hide)
        if hidden:
            self.withdraw()

    def hide(self):
        """Closing only hides the window; recording goes on until Quit in the tray menu."""
        self.withdraw()
        if not self._told_about_tray:
            self._told_about_tray = True
            self.tray.notify("NetUsage is still recording in the background. "
                             "Right-click its tray icon to open it or quit.", "NetUsage")

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.admin and self.refresh_btn.cget("state") == "normal":
            self.refresh_history()  # pick up whatever Windows saved while the window was hidden

    def quit_app(self):
        self.sampler.stop()  # ends the Windows event session; it would outlive us otherwise
        self.tray.stop()
        self.destroy()

    def build_live(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(4, 12))
        box, self.down_lbl = stat(top, "Download", DOWN, 44)
        box.pack(side="left", padx=(0, 56))
        box, self.up_lbl = stat(top, "Upload", UP, 44)
        box.pack(side="left")

        mid = ctk.CTkFrame(tab, fg_color="transparent")
        mid.pack(fill="both", expand=True)
        mid.grid_columnconfigure(0, weight=3, uniform="panels")  # widths stay put whatever the list holds
        mid.grid_columnconfigure(1, weight=2, uniform="panels")
        mid.grid_rowconfigure(0, weight=1)
        left, right = panel(mid), panel(mid)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew")
        self.live_fig, self.live_canvas = chart(left, 6, 3)
        self.live_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(right, text="Using the internet right now", text_color=TEXT,
                     font=(FONT, 14, "bold")).pack(anchor="w", padx=16, pady=(14, 8))
        self.live_table = Table(right, ("App", "Down", "Up"), (TEXT, DOWN, UP), 12)
        self.live_table.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def draw_live(self):
        with self.sampler.lock:
            speeds = list(self.sampler.speeds)
            top = self.sampler.top[:12]
        down, up = speeds[-1]
        self.down_lbl.configure(text=data.fmt_bytes(down) + "/s")
        self.up_lbl.configure(text=data.fmt_bytes(up) + "/s")
        self.live_table.set([(a, data.fmt_bytes(d) + "/s", data.fmt_bytes(u) + "/s") for a, d, u in top])

        self.live_fig.clear()
        ax = self.live_fig.add_subplot()
        style(ax, "/s")
        x = range(-len(speeds) + 1, 1)
        for i, color, label in ((0, DOWN, "Download"), (1, UP, "Upload")):
            y = [s[i] for s in speeds]
            ax.plot(x, y, color=color, linewidth=1.6, label=label)
            ax.fill_between(x, y, color=color, alpha=0.12)
        ax.set_xlim(x[0], 0)
        ax.set_ylim(0, max(max(max(s) for s in speeds) * 1.15, 64 * 1024))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{-int(v)}s ago" if v else "now"))
        ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=MUTED)
        self.live_fig.tight_layout()
        self.live_canvas.draw_idle()

    def draw_history(self):
        tab = self.history.get(self.tabs.get())
        if tab:
            tab.draw()

    def refresh_history(self):
        self.refresh_btn.configure(state="disabled")
        self.status.configure(text="Scanning Windows usage history and app logs…")
        threading.Thread(target=self._import, daemon=True).start()

    def _import(self):  # worker thread: only sets a message, tick() shows it
        try:
            n = data.import_srum()
            m = data.import_steam_log() + data.import_epic_log()
            now = datetime.now()
            until = data.history_until()
            until = "nothing yet" if until is None else until.strftime(
                "%H:%M" if until.date() == now.date() else "%b %d, %H:%M")
            found = "Up to date" if not n + m else f"Added {n:,} Windows records and {m:,} Steam/Epic entries"
            self._import_msg = (f"{found} (scanned at {now:%H:%M}). Windows history runs until {until}; "
                                f"NetUsage records the time after that while it's open.")
        except Exception as e:
            self._import_msg = f"Couldn't read Windows history: {e}"

    def tick(self):
        self._ticks += 1
        try:
            if os.path.exists(SHOW_FLAG):
                os.remove(SHOW_FLAG)
                self.show()
            visible = self.state() != "withdrawn"  # hidden: keep recording, skip drawing
            if visible and self.tabs.get() == "Live":
                self.draw_live()
            if self._import_msg:
                self.status.configure(text=self._import_msg)
                self.refresh_btn.configure(state="normal")
                self._import_msg = None
                if visible:
                    self.draw_history()
            elif visible and self.tabs.get() in self.history and self._ticks % self.history[self.tabs.get()].every == 0:
                self.draw_history()
        except Exception as e:  # e.g. db locked during an import; try again next second
            self.status.configure(text=f"Couldn't update the view: {e}")
        finally:
            self.after(1000, self.tick)


if __name__ == "__main__":
    if already_running():
        open(SHOW_FLAG, "w").close()
        sys.exit(0)
    ctk.set_appearance_mode("dark")
    App(ensure_admin(), hidden="--tray" in sys.argv).mainloop()

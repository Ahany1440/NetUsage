"""Data layer: SRUM import, live per-app sampling, and the SQLite history store."""
import ctypes
import glob
import os
import re
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import winreg
from bisect import bisect_right
from collections import defaultdict, deque
from contextlib import closing
from ctypes import wintypes as wt
from datetime import datetime, timedelta, timezone

import psutil
from dissect.esedb.tools.sru import SRU

def _data_dir():
    """Normally ProgramData. A portable.txt next to the .exe (e.g. on a USB stick) keeps each PC's data
    in a folder beside it instead, so nothing is left on PCs it visits."""
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "portable.txt")):
        return os.path.join(here, "NetUsage data", os.environ.get("COMPUTERNAME", "this PC"))
    return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "NetUsage")


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "usage.db")
SRUM_SRC = r"C:\Windows\System32\sru\SRUDB.dat"
SRUM_TOTAL = "windows (no app name)"  # SRUM's empty-id row: all traffic on the adapter that hour
OTHER = "Unattributed"  # traffic nobody could tie to an app (SRUM gaps, UDP, headers)
# what older versions called that leftover in saved live rows, after display_name()
LEFTOVER_NAMES = {OTHER, "Other (udp, system)"}


def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS usage(ts INTEGER, app TEXT, sent INTEGER, recv INTEGER,"
                " src TEXT, key TEXT UNIQUE)")
    con.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts)")
    return con


def _insert(rows):
    """Add rows, skipping any whose key is already stored. Returns rows added."""
    with closing(_db()) as con, con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?)", rows)
        return con.total_changes - before


def app_name(raw):
    """'\\Device\\...\\chrome.exe' -> 'chrome.exe'. Service and store-app ids pass through."""
    if not raw:
        return SRUM_TOTAL
    return raw.replace("/", "\\").rstrip("\\").split("\\")[-1].lower()


NAMES = {
    "steam.exe": "Steam", "steamwebhelper.exe": "Steam", "steamservice.exe": "Steam",
    "epiconlineservicesinstallhelper.exe": "Epic Games", "epicgameslauncher.exe": "Epic Games",
    "epiconlineserviceshost.exe": "Epic Games", "eabackgroundservice": "EA app", "eadesktop.exe": "EA app", "eaanticheat.installer.exe": "EA app", "upc.exe": "Ubisoft Connect",
    "dosvc": "Windows Update", "wuauserv": "Windows Update", "svchost.exe": "Windows services",
    "brave.exe": "Brave", "chrome.exe": "Chrome", "firefox.exe": "Firefox", "msedge.exe": "Microsoft Edge",
    "msedgewebview2.exe": "Edge WebView", "discord.exe": "Discord", "spotify.exe": "Spotify",
    "qbittorrent.exe": "qBittorrent", "jdownloader2.exe": "JDownloader", "idman.exe": "Internet Download Manager",
    "cloudflarewarp": "Cloudflare WARP", "onedrive.exe": "OneDrive", "amdinstallmanager.exe": "AMD Software",
    "claude.exe": "Claude", "windowsstore.exe": "Microsoft Store",
    "fortniteclient-win64-shipping.exe": "Fortnite", "fortniteclient-win64-shipping_eac_eos.exe": "Fortnite",
}
# Store app ids: name_version_arch_resource_publisher (resource is often empty)
STORE_APP = re.compile(r"^(?:[\w-]+\.)*([\w-]+?)_[\d.]+_(?:x64|x86|arm64|neutral)_\w*_\w+$")


def display_name(app):
    """'steam.exe' -> 'Steam', 'claude_3.6556.1.0_x64__pzs8sxrjxfjjc' -> 'Claude'."""
    for key in (app.lower(), app.lower() + ".exe"):  # saved live rows may already be display names
        if key in NAMES:
            return NAMES[key]
    m = STORE_APP.match(app)
    if m:
        app = NAMES.get(m[1].lower() + ".exe", m[1])
    elif app.endswith(".exe"):
        app = app[:-4]
    return app[:1].upper() + app[1:]


def fmt_bytes(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.1f} KB"


# ---------- SRUM (Windows' own 30-60 day per-app log) ----------

def import_srum():
    """Shadow-copy the locked SRUM database and store its network rows. Returns rows added."""
    os.makedirs(DATA_DIR, exist_ok=True)
    copy = os.path.join(DATA_DIR, "SRUDB.dat")
    if os.path.exists(copy):
        os.remove(copy)
    r = subprocess.run(["esentutl.exe", "/y", SRUM_SRC, "/vss", "/d", copy],
                       capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("Windows would not copy its usage database. "
                           "Run the app as administrator.\n" + r.stdout.strip()[-300:])

    rows = []
    try:
        with open(copy, "rb") as fh:
            for e in SRU(fh).get_table_entries(table_name="network_data"):
                stamp = e["TimeStamp"]
                if stamp is None:
                    continue
                try:
                    app = app_name(e["AppId"])
                except IndexError:  # app id missing from SRUM's id map
                    app = "unknown"
                if stamp.tzinfo is None:  # SRUM stores UTC
                    stamp = stamp.replace(tzinfo=timezone.utc)
                ts = int(stamp.timestamp()) - 3600  # a SRUM row stamps the end of the hour it covers
                rec = e.record
                rows.append((ts, app, e["BytesSent"] or 0, e["BytesRecvd"] or 0, "srum",
                             f"{ts}|{rec.get('AppId')}|{rec.get('UserId')}|"
                             f"{rec.get('InterfaceLuid')}|{rec.get('L2ProfileId')}"))
    finally:
        os.remove(copy)  # holds every user's app history; don't leave it lying around
    return _insert(rows)


# ---------- App logs (exact per-app bytes, better than SRUM's partial attribution) ----------

STEAM_STATS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] stats: \(Invalid, 0\) : (\d+) Bytes, (\d+) sec")


def import_steam_log():
    """Store the download bytes Steam logs every ~5 minutes. Returns rows added."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        return 0  # Steam not installed
    rows = []
    for name in ("content_log.previous.txt", "content_log.txt"):
        try:
            with open(os.path.join(steam, "logs", name), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = STEAM_STATS.match(line)
                    if m:  # the line is written at the end of its window; file it at the midpoint
                        end = int(datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").timestamp())
                        rows.append((end - int(m[3]) // 2, "steam.exe", 0, int(m[2]), "applog", f"steam|{end}"))
        except OSError:
            pass  # the .previous log only exists after Steam rotates it
    return _insert(rows)


EPIC_STAT = re.compile(r"^\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d):(\d+)\]\[\s*\d+\]LogBuildPatchServices: "
                       r"Build Stat: (TotalDownloadedData|ProcessActiveTime): ([\d.,:]+) ?(\w*)")
EPIC_UNITS = {"us": 1e-6, "ms": 1e-3, "sec": 1, "min": 60, "hr": 3600, "hrs": 3600}


def _epic_seconds(value, unit):
    """'398.725 ms' -> 0.4, '9.83 sec' -> 9.83, '1:27 min' -> 87 (longer times are written m:ss or h:mm:ss)."""
    if ":" in value:
        secs = 0
        for part in value.split(":"):
            secs = secs * 60 + float(part)
        return secs
    return float(value.replace(",", "")) * EPIC_UNITS.get(unit, 0)


def import_epic_log():
    """Epic's launcher and Fortnite each log one summary per finished download: total bytes and how long it
    ran. The bytes are spread back over that time in 10-minute pieces. Returns rows added."""
    local = os.environ.get("LOCALAPPDATA", "")
    sources = [(os.path.join(local, "EpicGamesLauncher", "Saved", "Logs", "EpicGamesLauncher*.log"),
                "epicgameslauncher.exe", "epic"),
               # Fortnite downloads extra game content itself, outside Epic's installer
               (os.path.join(local, "FortniteGame", "Saved", "Logs", "FortniteGame*.log"),
                "fortniteclient-win64-shipping.exe", "fortnite")]
    rows = []
    for pattern, app, tag in sources:
        for path in glob.glob(pattern):
            size = 0
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = EPIC_STAT.match(line)
                    if not m:
                        continue
                    if m[3] == "TotalDownloadedData":
                        size = int(m[4].replace(",", ""))
                    elif size:  # ProcessActiveTime closes the summary that TotalDownloadedData opened
                        # Epic's log times are UTC.
                        end = int(datetime.strptime(m[1], "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc).timestamp())
                        # shortcut: assumes no pause right before the summary; pauses shift bytes a little earlier
                        secs = max(int(_epic_seconds(m[4], m[5])), 1)
                        pieces = -(-secs // 600)  # ceil: one piece per started 10 minutes
                        for i in range(pieces):
                            rows.append((end - secs + int((i + 0.5) * secs / pieces), app, 0,
                                         size // pieces, "applog", f"{tag}|{m[1]}:{m[2]}|{i}"))
                        size = 0
    return _insert(rows) + _import_epic_installer_logs()


EOS_PROGRESS = re.compile(r'^\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d):\d+\].*"type":"progress".*?"downloadedBytes":(\d+)')


def _import_epic_installer_logs():
    """Game installs and updates run in Epic's install helper, which logs its running byte total
    every second. Stored as one row per minute."""
    dirs = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Epic Games", "Epic Online Services"),
            os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "EpicOnlineServices")]
    rows = []
    for path in [p for d in dirs for p in glob.glob(os.path.join(d, "InstallHelper", "Logs", "InstallHelper-*.log"))]:
        minutes, prev = defaultdict(int), 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = EOS_PROGRESS.match(line)
                if not m:
                    continue
                total = int(m[2])
                delta = total - prev if total >= prev else total  # running total drops when a new task starts
                prev = total
                ts = datetime.strptime(m[1], "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc).timestamp()
                minutes[int(ts // 60) * 60] += delta
        name = os.path.basename(path)
        rows += [(t + 30, "epiconlineservicesinstallhelper.exe", 0, b, "applog", f"eos|{name}|{t}")
                 for t, b in minutes.items() if b]
    return _insert(rows)


# ---------- History queries ----------

def _hourly_rows(con, since):
    """[(ts, app, recv, sent)] where each SRUM hour adds up to that hour's real adapter total."""
    hours = defaultdict(lambda: ([0, 0], defaultdict(lambda: [0, 0])))  # ts -> (total, apps)
    for ts, app, s, r in con.execute("SELECT ts, app, sent, recv FROM usage WHERE src = 'srum' AND ts >= ?",
                                     (since - 3600,)):
        total, apps = hours[ts]
        target = total if app == SRUM_TOTAL else apps[display_name(app)]  # e.g. steam + steamwebhelper -> Steam
        target[0] += r
        target[1] += s

    # SRUM only credits apps with part of their traffic. An app's own log and NetUsage's live recording
    # measure it directly, so per app and hour the biggest of the three wins (they overlap, never add).
    starts = sorted(hours)
    better = {src: defaultdict(lambda: [0, 0]) for src in ("applog", "live")}
    for src, ts, app, r, s in con.execute("SELECT src, ts, app, recv, sent FROM usage WHERE src IN ('applog', 'live') "
                                          "AND ts >= ?", (since - 3600,)):
        i = bisect_right(starts, ts) - 1
        name = display_name(app)
        if i >= 0 and ts < starts[i] + 3600 and name not in LEFTOVER_NAMES:
            better[src][starts[i], name][0] += r
            better[src][starts[i], name][1] += s
    for found in better.values():
        for (ts, app), (r, s) in found.items():
            rec = hours[ts][1][app]
            rec[0], rec[1] = max(rec[0], r), max(rec[1], s)

    rows = []
    for ts, (total, apps) in hours.items():
        rows += [(ts, app, r, s) for app, (r, s) in apps.items()]
        rest = (max(total[0] - sum(v[0] for v in apps.values()), 0),
                max(total[1] - sum(v[1] for v in apps.values()), 0))
        if any(rest):
            rows.append((ts, OTHER, *rest))

    # Live rows only fill the time after the newest SRUM hour, so nothing is counted twice.
    after = max(starts) + 3600 if starts else 0
    rows += con.execute("SELECT ts, app, recv, sent FROM usage WHERE src = 'live' AND ts >= ?",
                        (max(since, after),)).fetchall()
    return rows


def history(n, unit):
    """Usage for the last n hours/days, bucketed. unit is 'hour' or 'day'."""
    now = datetime.now()
    if unit == "hour":
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=n - 1)
        labels = [(start + timedelta(hours=i)).strftime("%H:00") for i in range(n)]
        spans = [f"{t}–{(int(t[:2]) + 1) % 24:02d}:00" for t in labels]
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=n - 1)
        labels = spans = [(start + timedelta(days=i)).strftime("%b %d") for i in range(n)]

    with closing(_db()) as con:
        rows = _hourly_rows(con, int(start.timestamp()))

    recv, sent = [0] * n, [0] * n
    buckets = [defaultdict(lambda: [0, 0]) for _ in range(n)]
    for ts, app, r, s in rows:
        when = datetime.fromtimestamp(ts)
        i = int((when - start).total_seconds() // 3600) if unit == "hour" else (when.date() - start.date()).days
        if 0 <= i < n:
            recv[i] += r
            sent[i] += s
            buckets[i][display_name(app)][0] += r
            buckets[i][display_name(app)][1] += s
    return {"labels": labels, "spans": spans, "recv": recv, "sent": sent, "apps": top_apps(buckets),
            "buckets": [top_apps([b]) for b in buckets]}


def top_apps(buckets):
    """Merge {app: [recv, sent]} dicts into [(app, recv, sent)], biggest first."""
    total = defaultdict(lambda: [0, 0])
    for b in buckets:
        for app, (r, s) in b.items():
            total[app][0] += r
            total[app][1] += s
    return sorted(((a, r, s) for a, (r, s) in total.items()), key=lambda x: x[1] + x[2], reverse=True)


def history_until():
    """When Windows' saved history ends (the app's own live log covers time after that), or None."""
    with closing(_db()) as con:
        newest = con.execute("SELECT MAX(ts) FROM usage WHERE src = 'srum'").fetchone()[0]
    return datetime.fromtimestamp(newest + 3600) if newest else None


# ---------- Live per-app TCP counters (Windows TCP EStats, needs admin) ----------

_ip = ctypes.WinDLL("iphlpapi")
AF_INET, AF_INET6, TCP_TABLE_OWNER_PID_ALL, ESTABLISHED, ESTATS_DATA = 2, 23, 5, 5, 1


class _Row4(ctypes.Structure):  # MIB_TCPROW_OWNER_PID; its first 5 fields are a MIB_TCPROW
    _fields_ = [("state", wt.DWORD), ("laddr", wt.DWORD), ("lport", wt.DWORD),
                ("raddr", wt.DWORD), ("rport", wt.DWORD), ("pid", wt.DWORD)]


class _Row6(ctypes.Structure):  # MIB_TCP6ROW_OWNER_PID
    _fields_ = [("laddr", ctypes.c_ubyte * 16), ("lscope", wt.DWORD), ("lport", wt.DWORD),
                ("raddr", ctypes.c_ubyte * 16), ("rscope", wt.DWORD), ("rport", wt.DWORD),
                ("state", wt.DWORD), ("pid", wt.DWORD)]


class _Row6Est(ctypes.Structure):  # MIB_TCP6ROW, the layout the IPv6 EStats calls want
    _fields_ = [("state", wt.DWORD), ("laddr", ctypes.c_ubyte * 16), ("lscope", wt.DWORD),
                ("lport", wt.DWORD), ("raddr", ctypes.c_ubyte * 16), ("rscope", wt.DWORD),
                ("rport", wt.DWORD)]


class _DataRod(ctypes.Structure):  # TCP_ESTATS_DATA_ROD_v0
    _fields_ = [("bytes_out", ctypes.c_uint64), ("segs_out", ctypes.c_uint64),
                ("bytes_in", ctypes.c_uint64), ("segs_in", ctypes.c_uint64),
                ("segs_out_all", ctypes.c_uint64), ("segs_in_all", ctypes.c_uint64),
                ("soft_errors", ctypes.c_ulong), ("soft_error_reason", ctypes.c_ulong),
                ("snd_una", ctypes.c_ulong), ("snd_nxt", ctypes.c_ulong), ("snd_max", ctypes.c_ulong),
                ("thru_bytes_acked", ctypes.c_uint64), ("rcv_nxt", ctypes.c_ulong),
                ("thru_bytes_received", ctypes.c_uint64)]


def _tcp_rows(af):
    row = _Row4 if af == AF_INET else _Row6
    size = wt.DWORD(0)
    for _ in range(3):  # table can grow between the size query and the read
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wt.DWORD(len(buf))
        if _ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            n = wt.DWORD.from_buffer(buf).value
            return list((row * n).from_buffer(buf, 4))
    return []


def tcp_counters(enabled):
    """{conn_key: (pid, bytes_out, bytes_in)} for established internet TCP connections.
    `enabled` is a set of keys we already switched counting on for."""
    out = {}
    rod = _DataRod()
    on = ctypes.c_ubyte(1)
    for af in (AF_INET, AF_INET6):
        for r in _tcp_rows(af):
            if r.state != ESTABLISHED:
                continue
            if af == AF_INET:
                if r.raddr & 0xFF == 127:
                    continue
                est, setf, getf = r, _ip.SetPerTcpConnectionEStats, _ip.GetPerTcpConnectionEStats
                key = (r.laddr, r.lport, r.raddr, r.rport)
            else:
                if bytes(r.raddr) == bytes(15) + b"\x01":  # ::1
                    continue
                est = _Row6Est(r.state, r.laddr, r.lscope, r.lport, r.raddr, r.rscope, r.rport)
                setf, getf = _ip.SetPerTcp6ConnectionEStats, _ip.GetPerTcp6ConnectionEStats
                key = (bytes(r.laddr), r.lport, bytes(r.raddr), r.rport)
            if key not in enabled:
                if setf(ctypes.byref(est), ESTATS_DATA, ctypes.byref(on), 0, 1, 0) != 0:
                    continue  # not admin, or the connection just closed
                enabled.add(key)
            if getf(ctypes.byref(est), ESTATS_DATA, None, 0, 0, None, 0, 0,
                    ctypes.byref(rod), 0, ctypes.sizeof(rod)) == 0:
                out[key] = (r.pid, rod.bytes_out, rod.bytes_in)
    return out


# ---------- Live per-app UDP bytes (Windows Kernel-Network events, needs admin) ----------

UDP_EVENTS = {42: 0, 43: 1, 58: 0, 59: 1}  # event id -> index: 0 sent, 1 received (IPv4, IPv4, IPv6, IPv6)
UDP_SESSION = "NetUsage-UDP"


class _EventIds(ctypes.Structure):  # EVENT_FILTER_EVENT_ID, sized for our 4 ids
    _fields_ = [("filter_in", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                ("count", ctypes.c_ushort), ("ids", ctypes.c_ushort * 4)]


def _is_loopback(addr):
    return addr[0] == 127 if len(addr) == 4 else (addr == bytes(15) + bytes([1])  # ::1
        or (addr[:12] == bytes(10) + bytes([255, 255]) and addr[12] == 127))  # ::ffff:127.x


class UdpCounter:
    """Per-process UDP bytes (calls, streams, games, QUIC) from Windows' network event feed.
    Windows only sends the 4 UDP event ids, so TCP downloads don't wake Python."""

    def __init__(self):
        import etw
        import etw.etw
        from etw import evntprov
        from etw.common import RETURN_RAW_UNFORMATTED_DATA
        self.lock = threading.Lock()
        self.bytes = defaultdict(lambda: [0, 0])  # pid -> [sent, recv]
        self._ids = _EventIds(1, 0, len(UDP_EVENTS), (ctypes.c_ushort * 4)(*UDP_EVENTS))
        flt = evntprov.EVENT_FILTER_DESCRIPTOR(ctypes.addressof(self._ids), ctypes.sizeof(self._ids),
                                               evntprov.EVENT_FILTER_TYPE_EVENT_ID)
        self._params = etw.etw.ProviderParameters(0, [flt])  # kept on self: Windows reads it at start()
        provider = etw.ProviderInfo("Microsoft-Windows-Kernel-Network",
                                    etw.GUID("{7DD42A49-5329-4832-8DFD-43D979153A88}"),
                                    any_keywords=0x30, params=self._params.get())  # 0x30: IPv4 + IPv6
        # a session left behind by a crash would block ours; stopping a missing one is harmless
        subprocess.run(["logman", "stop", UDP_SESSION, "-ets"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        # raw records: the library's own field decoding cost half a CPU core during a call
        # ignore_exists_error=False: never attach to a session another NetUsage owns (e.g. the tray copy)
        self._etw = etw.ETW(session_name=UDP_SESSION, providers=[provider], event_callback=self._on_record,
                            callback_data_flag=RETURN_RAW_UNFORMATTED_DATA, ignore_exists_error=False)
        self._etw.start()

    def _on_record(self, event):
        rec = event[1].contents
        self.add(rec.EventHeader.EventDescriptor.Id, ctypes.string_at(rec.UserData, rec.UserDataLength))

    def add(self, event_id, payload):
        """payload starts PID, size (uint32 each), then destination and source address (4 or 16 bytes)."""
        if event_id not in UDP_EVENTS or len(payload) < 16:
            return
        pid, size = struct.unpack_from("<II", payload)
        n = 4 if event_id in (42, 43) else 16
        if any(_is_loopback(payload[8 + i * n:8 + (i + 1) * n]) for i in (0, 1)):
            return  # traffic between programs on this PC never touches the network
        with self.lock:
            self.bytes[pid][UDP_EVENTS[event_id]] += size

    def take(self):
        """{pid: [sent, recv]} since the last call."""
        with self.lock:
            out, self.bytes = self.bytes, defaultdict(lambda: [0, 0])
        return out

    def stop(self):
        self._etw.stop()


VIRTUAL_NICS = ("loopback", "vethernet", "virtual", "hyper-v", "vpn", "tap", "tunnel", "wsl")


def _nic_totals():
    # shortcut: name-based filter for virtual adapters (their traffic also crosses the real NIC);
    # switch to GetIfTable2's HardwareInterface flag if an odd adapter name gets through.
    return {name: (c.bytes_sent, c.bytes_recv) for name, c in psutil.net_io_counters(pernic=True).items()
            if not any(v in name.lower() for v in VIRTUAL_NICS)}


def _nic_delta(old, new, seconds, speeds=None):
    """(sent, recv) between two _nic_totals() readings taken `seconds` apart. Each adapter is compared only
    with itself, and only if it was there both times. Around sleep, Wi-Fi can vanish or read 0 for a moment
    and then report its whole running count again; a jump faster than its link speed is that, not traffic."""
    speeds = speeds or {}
    sent = recv = 0
    for n in old.keys() & new.keys():
        ds, dr = max(new[n][0] - old[n][0], 0), max(new[n][1] - old[n][1], 0)
        limit = (speeds.get(n) or 10_000) * 125_000 * (seconds + 1)  # Mbit/s -> bytes, +1 s of slack
        if ds + dr <= limit:
            sent, recv = sent + ds, recv + dr
    return sent, recv


class LiveSampler(threading.Thread):
    """Samples once a second for the Live tab and writes per-minute rows to the history db."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.speeds = deque([(0.0, 0.0)] * 120, maxlen=120)  # (down, up) bytes/s, oldest first
        self.samples = deque(maxlen=320)  # (time, {app: (sent, recv)}) per second, for the 5-minute view
        self.top = []  # [(app, down_bps, up_bps)], busiest first
        self._apps = {}
        self._enabled = set()
        self._prev = {}
        self._minute = defaultdict(lambda: [0, 0])  # app -> [sent, recv]
        try:
            self._udp = UdpCounter()
        except Exception:
            self._udp = None  # not admin: UDP stays in Unattributed

    def stop(self):
        if self._udp:
            self._udp.stop()

    def _app(self, pid):  # not _name: Thread uses that attribute
        if pid not in self._apps:
            try:
                self._apps[pid] = display_name(psutil.Process(pid).name().lower())
            except psutil.Error:
                self._apps[pid] = f"pid {pid}"
        return self._apps[pid]

    def _flush(self, minute_start):
        rows = [(minute_start, a, s, r, "live", None) for a, (s, r) in self._minute.items() if s or r]
        self._minute.clear()
        self._apps.clear()  # pids get reused
        if rows:
            with closing(_db()) as con, con:
                con.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?)", rows)

    def run(self):
        self._last_nic, self._last_t = _nic_totals(), time.time()
        self._minute_no = int(self._last_t // 60)
        while True:
            time.sleep(1)
            try:
                self._sample()
            except Exception:  # one bad sample must not kill live updates for good
                traceback.print_exc()

    def _sample(self):
        now, nic = time.time(), _nic_totals()
        dt = max(now - self._last_t, 0.001)
        sent, recv = _nic_delta(self._last_nic, nic, now - self._last_t,
                                {n: st.speed for n, st in psutil.net_if_stats().items()})
        self._last_nic, self._last_t = nic, now

        cur = tcp_counters(self._enabled)
        self._enabled &= cur.keys()
        per_app = defaultdict(lambda: [0, 0])
        for key, (pid, b_out, b_in) in cur.items():
            if key in self._prev:  # first sight is just the baseline
                p_out, p_in = self._prev[key]
                per_app[self._app(pid)][0] += max(b_out - p_out, 0)
                per_app[self._app(pid)][1] += max(b_in - p_in, 0)
        self._prev = {k: (v[1], v[2]) for k, v in cur.items()}
        for pid, (s, r) in (self._udp.take() if self._udp else {}).items():
            per_app[self._app(pid)][0] += s
            per_app[self._app(pid)][1] += r
        # NIC counters include headers, UDP and QUIC; whatever TCP stats can't place goes here.
        per_app[OTHER][0] += max(sent - sum(v[0] for v in per_app.values()), 0)
        per_app[OTHER][1] += max(recv - sum(v[1] for v in per_app.values()), 0)

        for app, (s, r) in per_app.items():
            self._minute[app][0] += s
            self._minute[app][1] += r
        if int(now // 60) != self._minute_no:
            minute_start, self._minute_no = self._minute_no * 60, int(now // 60)
            self._flush(minute_start)  # if the db is locked this minute is dropped, not retried

        top = sorted(((a, r / dt, s / dt) for a, (s, r) in per_app.items() if s or r),
                     key=lambda x: x[1] + x[2], reverse=True)
        with self.lock:
            self.speeds.append((recv / dt, sent / dt))
            self.top = top
            self.samples.append((now, {a: (s, r) for a, (s, r) in per_app.items() if s or r}))

    def recent(self, seconds=300, step=10):
        """The last few minutes in 10-second bars, shaped like history(). Kept in memory only."""
        end = (time.time() // step + 1) * step
        n = seconds // step
        start = end - n * step
        recv, sent = [0] * n, [0] * n
        buckets = [defaultdict(lambda: [0, 0]) for _ in range(n)]
        with self.lock:
            samples = list(self.samples)
        for ts, per_app in samples:
            i = int((ts - start) // step)
            if 0 <= i < n:
                for app, (s, r) in per_app.items():
                    recv[i] += r
                    sent[i] += s
                    buckets[i][app][0] += r
                    buckets[i][app][1] += s
        at = [datetime.fromtimestamp(start + i * step) for i in range(n + 1)]
        return {"labels": [t.strftime("%H:%M:%S") for t in at[:-1]],
                "spans": [f"{a:%H:%M:%S}–{b:%H:%M:%S}" for a, b in zip(at, at[1:])],
                "recv": recv, "sent": sent, "apps": top_apps(buckets),
                "buckets": [top_apps([b]) for b in buckets]}

"""Self-check for the data layer. Run: python test_data.py"""
import os
import sqlite3
import tempfile
import time

import data

data.DATA_DIR = tempfile.mkdtemp()
data.DB_PATH = os.path.join(data.DATA_DIR, "test.db")

assert data.app_name(r"\Device\HarddiskVolume3\Program Files\Chrome\chrome.exe") == "chrome.exe"
assert data.app_name("DiagTrack") == "diagtrack"
assert data.app_name(None) == "windows (no app name)"
assert data.display_name("steam.exe") == "Steam"
assert data.display_name("claude_3.6556.1.0_x64__pzs8sxrjxfjjc") == "Claude"
assert data.display_name("microsoft.windowsstore_22608.1401.3.0_x64__8wekyb3d8bbwe") == "Microsoft Store"
assert data.display_name("someapp.exe") == "Someapp"
assert data.fmt_bytes(5 * 1024 ** 3) == "5.00 GB"
assert data.fmt_bytes(3 * 1024 ** 2) == "3.0 MB"

now = int(time.time())
hour = now - 7200
con = data._db()
with con:
    con.executemany("INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?)", [
        # One SRUM hour: the empty-id row is the adapter total, apps are parts of it.
        (hour, data.SRUM_TOTAL, 300, 5000, "srum", "t"),
        (hour, "chrome.exe", 100, 1000, "srum", "a"),
        (hour, "chrome.exe", 100, 1000, "srum", "a"),        # duplicate key, must be ignored
        (hour, "steam.exe", 50, 500, "srum", "s"),           # SRUM only credits part of it...
        (hour + 600, "steam.exe", 0, 3000, "applog", "l"),   # ...Steam's own log has the real bytes
        (hour + 1200, "chrome.exe", 5, 5, "live", None),     # inside that SRUM hour: skipped
        (now - 60, "discord.exe", 10, 700, "live", None),    # after the newest SRUM hour: counted
        (now - 40 * 86400, "old.exe", 1, 1, "srum", "b"),    # outside 30 days
    ])
try:
    con.execute("INSERT INTO usage VALUES (?,?,?,?,?,?)", (now, "x", 0, 0, "srum", "a"))
    raise AssertionError("duplicate SRUM key was accepted")
except sqlite3.IntegrityError:
    pass
con.close()

h = data.history(24, "hour")
assert len(h["labels"]) == 24
apps = {a: (r, s) for a, r, s in h["apps"]}
assert apps == {"Steam": (3000, 50), "Chrome": (1000, 100), data.OTHER: (1000, 150),
                "Discord": (700, 10)}, apps
assert sum(len(b) for b in h["buckets"]) == 4 and h["buckets"][-1][0][0] == "Discord", h["buckets"]
assert sum(h["recv"]) == 5000 + 700 and sum(h["sent"]) == 300 + 10, h  # SRUM total, not total + apps
assert "Old" not in {a for a, _, _ in data.history(30, "day")["apps"]}

# An app log bigger than the SRUM total raises the total instead of going negative.
con = data._db()
with con:
    con.execute("INSERT INTO usage VALUES (?,?,?,?,?,?)", (hour + 900, "steam.exe", 0, 4000, "applog", "l2"))
con.close()
h = data.history(24, "hour")
apps = {a: r for a, r, _ in h["apps"]}
assert apps["Steam"] == 7000 and data.OTHER not in apps or apps[data.OTHER] == 0, apps
assert sum(h["recv"]) == 7000 + 1000 + 700, h

# NetUsage's own live recording inside a Windows hour names apps Windows missed;
# its leftover rows (old names too, like a bogus wake-from-sleep spike) are ignored there.
con = data._db()
with con:
    con.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?)", [
        (hour + 1500, "Discord", 40, 600, "live", None),
        (hour + 1560, "discord.exe", 10, 200, "live", None),           # older build's raw name: same app
        (hour + 1600, "unattributed", 0, 9 * 10**9, "live", None),
        (hour + 1600, "other (udp, system)", 0, 9 * 10**9, "live", None),
    ])
con.close()
h = data.history(24, "hour")
apps = {a: (r, s) for a, r, s in h["apps"]}
assert apps["Discord"] == (800 + 700, 50 + 10), apps                # 800 in the Windows hour + the later live 700
assert sum(h["recv"]) == 7000 + 1000 + 800 + 700, h                 # Discord raised the hour; no 9 GB leftovers

# Epic: one summary per install, bytes spread over the time it ran (30 min -> 3 pieces).
logs = os.path.join(data.DATA_DIR, "EpicGamesLauncher", "Saved", "Logs")
os.makedirs(logs)
with open(os.path.join(logs, "EpicGamesLauncher.log"), "w") as fh:
    fh.write("[2026.10.22-19.08.31:153][803]LogBuildPatchServices: Build Stat: TotalDownloadedData: 3,000,000\n"
             "[2026.10.22-19.08.31:153][803]LogBuildPatchServices: Build Stat: ProcessActiveTime: 30 min\n"
             "[2026.10.22-19.08.31:852][845]LogBuildPatchServices: Build Stat: TotalDownloadedData: 0\n"
             "[2026.10.22-19.08.31:852][845]LogBuildPatchServices: Build Stat: ProcessActiveTime: 695.258 ms\n")
helper = os.path.join(data.DATA_DIR, "Epic Games", "Epic Online Services", "InstallHelper", "Logs")
os.makedirs(helper)
progress = '[2026.10.22-19.{}:000] Msg.cpp(70): Sent message: {{"type":"progress","body":{{"bpsOngoingStats":{{"downloadedBytes":{}}}}}}}\n'
with open(os.path.join(helper, "InstallHelper-test.log"), "w") as fh:
    for stamp, total in (("08.10", 0), ("08.50", 400), ("09.20", 1000), ("09.40", 50)):  # last: new task
        fh.write(progress.format(stamp, total))
os.environ["LOCALAPPDATA"] = os.environ["PROGRAMDATA"] = data.DATA_DIR  # only the fake logs
assert data.import_epic_log() == 3 + 2  # 3 launcher pieces + 2 installer minutes
assert data.import_epic_log() == 0  # rescanning adds nothing
con = data._db()
got = con.execute("SELECT MIN(ts), MAX(ts), SUM(recv) FROM usage WHERE key LIKE 'epic|%'").fetchone()
con.close()
end = 1792696111  # 2026-10-22 19:08:31 UTC
assert got == (end - 1800 + 300, end - 300, 3000000), got
con = data._db()
got = con.execute("SELECT ts, recv FROM usage WHERE key LIKE 'eos|%' ORDER BY ts").fetchall()
con.close()
minute = 1792696080  # 2026-10-22 19:08:00 UTC
assert got == [(minute + 30, 400), (minute + 90, 650)], got  # 600 more, then 50 from the new task

# Epic durations: long ones are written m:ss / h:mm:ss.
assert abs(data._epic_seconds("398.725", "ms") - 0.398725) < 1e-9
assert data._epic_seconds("9.83", "sec") == 9.83
assert data._epic_seconds("1:27", "min") == 87
assert data._epic_seconds("1:02:03", "hrs") == 3723
# Fortnite's own log, same summary format: 2:36 min -> 1 piece, credited to Fortnite.
fn = os.path.join(data.DATA_DIR, "FortniteGame", "Saved", "Logs")
os.makedirs(fn)
with open(os.path.join(fn, "FortniteGame.log"), "w") as fh:
    fh.write("[2026.10.23-14.48.58:659][440]LogBuildPatchServices: Build Stat: TotalDownloadedData: 376004543\n"
             "[2026.10.23-14.48.58:660][440]LogBuildPatchServices: Build Stat: ProcessActiveTime: 2:36 min\n")
assert data.import_epic_log() == 1
con = data._db()
got = con.execute("SELECT app, recv, ts FROM usage WHERE key LIKE 'fortnite|%'").fetchall()
con.close()
assert got == [("fortniteclient-win64-shipping.exe", 376004543, 1792766938 - 156 + 78)], got
assert data.display_name(got[0][0]) == "Fortnite"

# Live sampler: per-app deltas from fake TCP counters must reach the top list.
ticks = [0]
data.tcp_counters = lambda enabled: {("k",): (os.getpid(), ticks[0] * 1000, ticks[0] * 50000)}
s = data.LiveSampler()
s._last_nic, s._last_t, s._minute_no = data._nic_totals(), time.time() - 1, int(time.time() // 60)
for ticks[0] in (1, 2, 3):
    s._sample()
me = [t for t in s.top if t[0] != data.OTHER]
assert me and me[0][1] > 0, s.top

# Portable mode: only a portable.txt next to the .exe moves the data folder.
import sys
usb = tempfile.mkdtemp()
sys.frozen, real_exe = True, sys.executable
sys.executable = os.path.join(usb, "NetUsage.exe")
os.environ["PROGRAMDATA"], os.environ["COMPUTERNAME"] = r"C:\ProgramData", "TESTPC"
assert data._data_dir() == os.path.join(r"C:\ProgramData", "NetUsage")
open(os.path.join(usb, "portable.txt"), "w").close()
assert data._data_dir() == os.path.join(usb, "NetUsage data", "TESTPC")
del sys.frozen
sys.executable = real_exe

# Adapters: one that drops out and comes back (wake from sleep) must not count its whole history.
before = {"Wi-Fi": (100, 1000)}
assert data._nic_delta(before, {}) == (0, 0)                                    # asleep: Wi-Fi gone
assert data._nic_delta({}, {"Wi-Fi": (5 * 10**9, 8 * 10**9)}) == (0, 0)         # back with its running count
assert data._nic_delta(before, {"Wi-Fi": (150, 1600)}) == (50, 600)             # normal second
assert data._nic_delta(before, {"Wi-Fi": (10, 20)}) == (0, 0)                   # counter reset

# UDP: events add up per process; traffic between programs on this PC is ignored.
from collections import defaultdict
import threading
u = data.UdpCounter.__new__(data.UdpCounter)  # skip starting the real Windows feed (needs admin)
u.lock, u.bytes = threading.Lock(), defaultdict(lambda: [0, 0])
import socket, struct
v4 = lambda pid, size, dst, src: struct.pack("<II", pid, size) + socket.inet_aton(dst) + socket.inet_aton(src) + bytes(8)
v6 = lambda pid, size, dst, src: (struct.pack("<II", pid, size) + socket.inet_pton(socket.AF_INET6, dst)
                                  + socket.inet_pton(socket.AF_INET6, src) + bytes(8))
u.add(42, v4(32032, 1200, "162.159.130.234", "192.168.1.5"))
u.add(43, v4(32032, 900, "192.168.1.5", "162.159.130.234"))
u.add(59, v6(32032, 100, "fe80::1", "2606:4700::1"))
u.add(43, v4(500, 50, "127.0.0.1", "127.0.0.1"))       # between programs on this PC: ignored
u.add(59, v6(500, 50, "::1", "::1"))                   # same, IPv6
u.add(11, v4(500, 999, "8.8.8.8", "192.168.1.5"))      # a TCP event slipping through: ignored
assert dict(u.take()) == {32032: [1200, 1000]}, u.bytes
assert dict(u.take()) == {}  # take() empties it

# 5-minute view: samples land in 10-second bars; anything older than 5 minutes drops out.
s = data.LiveSampler()
now = time.time()
s.samples.extend([(now - 400, {"Old": (1, 1)}), (now - 15, {"Steam": (10, 500)}), (now, {"Steam": (5, 100), "Discord": (0, 7)})])
r = s.recent()
assert len(r["labels"]) == 30 and len(r["spans"]) == 30
assert sum(r["recv"]) == 607 and sum(r["sent"]) == 15, r
assert [a for a, _, _ in r["apps"]] == ["Steam", "Discord"], r["apps"]
assert r["buckets"][-1] == [("Steam", 100, 5), ("Discord", 7, 0)], r["buckets"][-1]
print("all data checks passed")

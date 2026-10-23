# NetUsage

See which apps use your internet on Windows, live and over the last 30+ days. Free, local, nothing leaves your PC.

## Download

Get `NetUsage.exe` from the [Releases](../../releases) page and double-click it. Windows asks for admin rights; click **Yes** (it needs them to read Windows' usage history). No install and no Python needed. Works on Windows 10 and 11.

Some antivirus programs warn about it because the .exe isn't signed.

## Tabs

- **Live**: your download and upload speed, a graph of the last 2 minutes, and the apps using the internet right now. It updates every second.
- **5 minutes**: the last 5 minutes in 10-second bars, from the app's own live recording, so it only covers time NetUsage has been running.
- **24 hours / 7 days / 30 days**: total downloaded and uploaded, a bar chart per hour or day, and the top apps with a bar showing each one's share. Hover a bar for its numbers; click it to see the apps behind it.

Every time you open the app it scans Windows' usage history and the download logs of Steam and Epic Games. Click **Scan again** to repeat that without restarting.

## Running in the background

- Closing the window only hides it in the tray (next to the clock); it keeps recording. Right-click the tray icon for **Open NetUsage** or **Quit**.
- Only one copy runs at a time. Opening it again just brings the window back.
- While hidden it uses about 106 MB of RAM and 0.1–0.2% CPU.

To start it with Windows, hidden in the tray and with no admin prompt, run this once in an administrator PowerShell (change the path to where you put the .exe):

```powershell
$exe = "C:\Tools\NetUsage.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user; $trigger.Delay = "PT20S"
Register-ScheduledTask -TaskName NetUsage -Force `
  -Action (New-ScheduledTaskAction -Execute $exe -Argument "--tray") -Trigger $trigger `
  -Principal (New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest) `
  -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0)
```

To undo it: `schtasks /Delete /TN NetUsage /F` (also as administrator).

## Portable use (USB stick)

Put `NetUsage.exe` on a USB stick with an empty file named `portable.txt` next to it. NetUsage then saves its data in a `NetUsage data\<PC name>` folder on the stick instead of on the PC, so it leaves nothing behind on PCs you check. Quit it from the tray before unplugging.

Windows' usage history shows what every user on that PC did online, so only use it on PCs you own or are allowed to check.

## Where the numbers come from

- **History** comes from Windows' own usage log (SRUM). Windows records it even when this app is closed and keeps about 30 to 60 days. Windows writes it about once an hour, so the latest hour fills in later.
- Each scan copies that log into `C:\ProgramData\NetUsage\usage.db`, so your history keeps growing after Windows deletes old entries.
- Windows keeps a hidden row with **no app name** that holds the **total** traffic on your adapter for each hour. NetUsage uses it as the real total, so nothing is counted twice.
- Windows only credits apps with part of their traffic (for example about a third of a Steam download). **Steam's download log** records exact bytes, so NetUsage uses that for Steam.
- **Epic Games**: game installs and updates are logged second by second by Epic's install helper, so they count exactly. Launcher self-updates are logged as one summary when they finish, and NetUsage spreads those bytes over the time they took.
- Whatever is left after the named apps shows as **Unattributed**: traffic nobody could tie to an app (gaps in Windows' history, network overhead).
- **Live** per-app numbers come from Windows' per-connection TCP counters plus its network event feed for UDP, so calls, streams, games and QUIC (e.g. YouTube) show by app name too. Packet headers and other overhead stay Unattributed. The total speed at the top is always exact.
- While open, NetUsage also saves its live numbers every minute. They only fill the time after the newest hour Windows has saved.

## Run from source

```
pip install -r requirements.txt
python app.py
python test_data.py
```

`test_data.py` is a self-check of the data layer.

## Build the .exe

```
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name NetUsage --icon netusage.ico --add-data "netusage.ico;." --collect-submodules dissect --collect-submodules pystray --collect-submodules etw app.py
```

The .exe ends up in `dist\NetUsage.exe`. The icon is drawn by `make_icon.py`.

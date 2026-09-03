# win-iso-updater

[العربية](README.md)

**Put in an old Windows USB. Get out a patched ISO. One command.**

One Python script (682 lines). It needs only `requests`. It finds the install files. It downloads the latest cumulative update from the Microsoft catalog. It merges the update into the image. Then it builds an ISO that boots on UEFI and BIOS.

I built it while re-imaging 1,800 machines over 3 months. There was no central management. I did one machine at a time.

---

## The problem

Adding updates into a Windows image is called *slipstreaming*. The idea is well known. But doing it by hand is slow work.

The steps are: read the build number, search the catalog, download, mount `install.wim`, inject, clean up components, build the ISO. Each step breaks in its own way.

This tool runs all of them in one command.

---

## How it works

| Step | What it does |
|---|---|
| 1. Locate the install files | Scans every drive for a `sources` folder with `install.wim` or `install.esd` |
| 2. Prepare the image | Converts `ESD` to an editable `WIM`, then reads the build number from it |
| 3. Search | Asks the Microsoft Update Catalog for the latest cumulative update for that build |
| 4. Download | Uses `aria2c` if it is installed. If not, a built-in parallel HTTP Range download over 8 connections. A single connection is the last resort |
| 5. Inject | `DISM` mounts the image. It injects the `.msu`/`.cab` into **every** edition inside. Then it cleans up components |
| 6. Build | `oscdimg` writes one ISO. It boots on UEFI and on BIOS |

The downloaded package stays in the updates folder as a cache. The next run skips it if its size matches the remote size.

---

## The key design decision

`DISM 552` is the error that stops this job most often. It means the DISM on the machine is older than the image it services. The Windows message never says this.

The tool checks compatibility **before it starts**. It compares the DISM build number with the image build number. If they do not match, it stops and prints a clear reason. It also prints the ways out. You can install the ADK, pass `--dism`, or override with `--force` at your own risk.

The run is long. It takes tens of minutes. A failure near the end means you start again. The check at the start costs one moment.

---

## Running it

```bash
pip install -r requirements.txt
```

```bash
# USB plugged in: auto-detect and update the media in place
python iso_updater.py

# From a ready ISO, building a new ISO
python iso_updater.py --no-detect --iso "D:\Win11.iso" --out "D:\Win11-Updated.iso"
```

| Flag | What it does |
|---|---|
| `--media` | Media root, set by hand (e.g. `E:\`). Default: scan every drive |
| `--no-detect` | Turn off auto-detection and use ISO mode |
| `--iso` | Path to a ready ISO |
| `--out` | Where the finished ISO goes. Without it, the tool updates the USB in place |
| `--updates` | Folder for the update packages. Defaults to `<usb>\updates` as a cache |
| `--connections` | Number of parallel download connections (default 8) |
| `--no-online` | Skip the catalog. Inject only the local `.msu`/`.cab` files |
| `--dism` | Path to a newer `dism.exe`, like the one that comes with the ADK |
| `--force` | Skip the DISM compatibility check (failure expected) |

The tool has 17 flags. The table shows the main ones. `python iso_updater.py --help` shows the rest.

**Needs:** Python 3.9+ · Windows ADK (`DISM` + `oscdimg`) · administrator rights

Typical output. The timestamp prefix on each line is removed here. The log messages are in Arabic:

```
💿 وُجد وسط تثبيت: D:\sources\install.esd  (نوع: .esd)
🚀 وضع الوسط المباشر — الجذر: D:\
📦 مجلد التحديثات (كاش): D:\updates
🔄 الصورة بصيغة ESD (للقراءة فقط) — تحويلها إلى WIM قابل للخدمة...
🔍 فحص نسخة الويندوز...
📊 أعلى Build في الـ WIM: 22621
🧰 DISM المستخدَم: dism.exe  (build 26100)
🌐 بحث في كتالوج مايكروسوفت: Cumulative Update for Windows 11 Version 22H2 x64
🎯 أحدث تحديث: 2024-02 Cumulative Update for Windows 11 ...
⚡ استخدام aria2c بـ 8 اتصال
✅ نُزِّل 1 ملف تحديث إلى D:\updates
🔧 خدمة الإصدار index:1 ...
✅ تم دمج التحديثات في كل الإصدارات
📀 بناء ISO جديد (UEFI + BIOS)...
✅ ISO جاهز: D:\Win11-Updated.iso
```

The tool also writes every line to a plain-text log file (`--log`).

---

## Why I built it

I work as an IT supervisor. We had 1,800 machines with the same problems again and again. There was no central management to fix them from far. The only fix was to re-image all of them.

It took 3 months. On every machine the slowest step was the same. Windows finishes the install, then hours of updates start.

So I built this tool. It puts the updates into the image one time. Now the machine is up to date on its first boot.

In the end they were all enrolled in central management, and the old problems were gone. After that I built zero-touch provisioning. So those three months never repeat.

---

## License

MIT — see [LICENSE](LICENSE).

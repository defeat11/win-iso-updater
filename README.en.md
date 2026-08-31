# win-iso-updater

[العربية](README.md)

**An outdated Windows USB goes in — a patched ISO comes out. One command.**

A single Python script (682 lines, no dependencies beyond `requests`): it locates the install files, pulls the latest cumulative update from the Microsoft catalog, injects it into the image, and builds an ISO that boots on both UEFI and BIOS.

Built after imaging more than 1,800 machines by hand — each one sat through hours of updates after setup.

---

## The problem

Injecting updates into a Windows image is called *slipstreaming*. The idea is well known. Doing it by hand is tedious.

The steps: read the build number, search the catalog, download, mount `install.wim`, inject, clean up components, build the ISO. Every step breaks in its own way.

This tool runs all of them in one command.

---

## How it works

| Step | What it does |
|---|---|
| 1. Locate the install files | Scans every drive for a `sources` folder holding `install.wim` or `install.esd` |
| 2. Prepare the image | Converts `ESD` to an editable `WIM` and reads the build number from inside it |
| 3. Search | Queries the Microsoft Update Catalog for the latest cumulative update for that build |
| 4. Download | `aria2c` first if installed, otherwise a built-in parallel HTTP Range download over 8 connections, then a single connection as the last resort |
| 5. Inject | `DISM` mounts the image, injects the `.msu`/`.cab` into **every** edition inside it, then cleans up components |
| 6. Build | `oscdimg` writes an ISO that boots on UEFI and BIOS alike |

The downloaded package stays cached in the updates folder; the next run skips it when its size matches the remote size.

---

## The key design decision

`DISM 552` is the error that stops this process most often: the DISM on the machine is older than the image it is servicing. The Windows message never says that.

The tool checks compatibility **before it starts** — it compares the DISM build number against the image build number and stops with a clear explanation and the ways out (install the ADK, pass `--dism`, or override with `--force` at your own risk).

The run is long — tens of minutes. Failing near the end means starting over. The check up front costs a moment.

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
| `--out` | Where the finished ISO goes. Without it, the USB is updated in place |
| `--updates` | Folder for the update packages. Defaults to `<usb>\updates` as a cache |
| `--connections` | Number of parallel download connections (default 8) |
| `--no-online` | Skip the catalog — inject only the local `.msu`/`.cab` files |
| `--dism` | Path to a newer `dism.exe` (such as the one shipped with the ADK) |
| `--force` | Skip the DISM compatibility check (failure expected) |

The tool has 17 flags; the table shows the main ones. `python iso_updater.py --help` lists the rest.

**Requires:** Python 3.9+ · Windows ADK (`DISM` + `oscdimg`) · administrator rights

Typical output (the timestamp prefix on every line is omitted here). Log messages are in Arabic:

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

Every line is also written to a plain-text log file (`--log`).

---

## Why I built it

I work as an IT supervisor. I have imaged more than 1,800 machines.

This was the slowest step in the process, so I automated it.

---

## License

MIT — see [LICENSE](LICENSE).

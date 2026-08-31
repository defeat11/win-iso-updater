#!/usr/bin/env python3
"""
أداة خدمة ISO ويندوز: تحميل/فك ISO، دمج تحديثات .cab/.msu في كل الإصدارات،
ثم بناء ISO جديد قابل للإقلاع UEFI + BIOS.

التشغيل (يتطلب صلاحية Administrator):
    python iso_updater.py --iso D:\\Win11.iso --updates C:\\updates --out C:\\Win11_updated.iso
"""

import argparse
import concurrent.futures
import ctypes
import json
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import threading
from pathlib import Path

import requests

# أحدث build معروف لويندوز 11 (يمرَّر أيضًا عبر --min-build لتجاوز الثبات في الكود)
DEFAULT_MIN_BUILD = 22631

# خريطة build → تسمية الإصدار (للبحث في كتالوج مايكروسوفت)
WIN11_BUILD_TO_VERSION = {
    22000: "21H2",
    22621: "22H2",
    22631: "23H2",
    26100: "24H2",
}

CATALOG_SEARCH = "https://www.catalog.update.microsoft.com/Search.aspx"
CATALOG_DOWNLOAD = "https://www.catalog.update.microsoft.com/DownloadDialog.aspx"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) iso_updater"}

# مسار DISM المستخدَم (يُحدَّد في main: ADK إن وُجد، وإلا المدمج في النظام)
ADK_DISM = Path(r"C:\Program Files (x86)\Windows Kits\10"
                r"\Assessment and Deployment Kit\Deployment Tools\amd64\DISM\dism.exe")
DISM = "dism"

log = logging.getLogger("iso_updater")


# ---------------------------------------------------------------------------
# مساعدات عامة
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def resolve_dism(explicit=None) -> str:
    """يختار أفضل DISM متاح: المحدّد يدويًا، ثم ADK، ثم المدمج في النظام."""
    for c in (explicit, ADK_DISM, Path(r"C:\Windows\System32\dism.exe")):
        if c and Path(c).exists():
            return str(c)
    return "dism"


def dism_build(dism_path: str) -> int:
    """رقم الـ build لأداة DISM (مثل 19041 أو 26100) من إصدار الملف نفسه."""
    exe = dism_path if Path(dism_path).is_file() else (
        shutil.which(dism_path) or r"C:\Windows\System32\dism.exe")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True).stdout.strip()
        m = re.search(r"\d+\.\d+\.(\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def check_dism_compat(image_build, force=False):
    """
    DISM يجب أن يكون مساوياً أو أحدث من الصورة المخدومة، وإلا تفشل التحديثات
    (خصوصًا تحديثات checkpoint في 24H2). يوقف بإنذار واضح ما لم يُمرَّر force.
    """
    dv = dism_build(DISM)
    log.info("🧰 DISM المستخدَم: %s  (build %s)", DISM, dv or "?")
    if image_build and dv and dv < image_build:
        msg = (
            f"\n❌ عدم توافق: DISM build {dv} أقدم من الصورة build {image_build}.\n"
            f"   لا يمكن حقن تحديثات {WIN11_BUILD_TO_VERSION.get(image_build, '')} "
            f"(خصوصًا checkpoint) بأداة أقدم — هذا سبب الخطأ 552.\n"
            f"   الحل: ثبّت «Windows ADK» لإصدار 24H2 ثم أعد التشغيل (سيُكتشف "
            f"DISM الخاص به تلقائيًا)، أو نفّذ على جهاز ويندوز 11 24H2،\n"
            f"   أو مرّر مسار DISM أحدث عبر --dism. (للتجاوز على مسؤوليتك: --force)"
        )
        if force:
            log.warning(msg + "\n⚠️ تم التجاوز بـ --force — متوقّع الفشل.")
        else:
            log.error(msg)
            sys.exit(2)


def run(cmd, **kw):
    """تغليف subprocess.run: يلتقط المخرجات ويسجّلها كاملة عند الفشل."""
    log.debug("RUN: %s", " ".join(map(str, cmd)))
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    try:
        return subprocess.run(cmd, check=True, **kw)
    except subprocess.CalledProcessError as e:
        # اطبع آخر مخرجات الأداة (DISM/غيرها) للوق ليظهر السبب الحقيقي بدل كود غامض
        if e.stdout:
            log.error("⤷ stdout:\n%s", e.stdout.strip()[-2000:])
        if e.stderr:
            log.error("⤷ stderr:\n%s", e.stderr.strip()[-2000:])
        raise


# ---------------------------------------------------------------------------
# اكتشاف وسط تثبيت ويندوز تلقائيًا على أي قرص (C/D/E/... فلاشة/ISO مركّب)
# ---------------------------------------------------------------------------
def find_install_media(explicit=None):
    """
    يبحث في كل الأقراص عن مجلد sources فيه install.wim أو install.esd.
    يرجّع (جذر_الوسط, ملف_الصورة) أو (None, None).
    """
    roots = []
    if explicit:
        roots.append(Path(explicit))
    else:
        # كل حروف الأقراص الموجودة فعليًا — يشمل الفلاشة مهما كان حرفها
        roots = [Path(f"{c}:\\") for c in string.ascii_uppercase
                 if Path(f"{c}:\\").exists()]

    for root in roots:
        for name in ("install.wim", "install.esd"):
            img = root / "sources" / name
            if img.exists():
                log.info("💿 وُجد وسط تثبيت: %s  (نوع: %s)", img, img.suffix)
                return root, img
    return None, None


def get_fs_type(path: Path) -> str:
    """نظام ملفات القرص الذي يقع فيه المسار (NTFS / FAT32 / exFAT...)."""
    drive = os.path.splitdrive(str(path.resolve()))[0]  # مثل 'D:'
    if not drive:
        return ""
    out = run(["powershell", "-NoProfile", "-Command",
               f"(Get-Volume -DriveLetter {drive[0]}).FileSystem"],
              capture_output=True, text=True).stdout.strip()
    return out


def list_indices_img(img_file: Path):
    out = run([DISM, "/English", "/Get-WimInfo", f"/WimFile:{img_file}"],
              capture_output=True, text=True).stdout
    return [int(m) for m in re.findall(r"Index\s*:\s*(\d+)", out)]


def ensure_wim(img_file: Path, work_dir: Path) -> Path:
    """
    يضمن وجود install.wim قابل للخدمة.
    - لو الصورة install.wim: تُرجَع كما هي (نخدمها في مكانها).
    - لو install.esd (للقراءة فقط): تُحوَّل إلى install.wim داخل work_dir
      عبر تصدير كل الإصدارات (DISM /Export-Image).
    """
    if img_file.suffix.lower() == ".wim":
        return img_file

    log.info("🔄 الصورة بصيغة ESD (للقراءة فقط) — تحويلها إلى WIM قابل للخدمة...")
    work_dir.mkdir(parents=True, exist_ok=True)
    wim_out = work_dir / "install.wim"
    if wim_out.exists():
        wim_out.unlink()

    for idx in list_indices_img(img_file):
        log.info("  📦 تصدير الإصدار index:%d ...", idx)
        run([DISM, "/Export-Image",
             f"/SourceImageFile:{img_file}", f"/SourceIndex:{idx}",
             f"/DestinationImageFile:{wim_out}", "/Compress:max", "/CheckIntegrity"])
    log.info("✅ تم إنشاء WIM قابل للخدمة: %s", wim_out)
    return wim_out


# ---------------------------------------------------------------------------
# تحميل ISO (مع تحقق فعلي بدل حفظ صفحة خطأ)
# ---------------------------------------------------------------------------
def download_iso(url: str, dest: Path):
    log.info("📥 تحميل ISO من: %s", url)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()

        # روابط prss تتطلب توكن مؤقت؛ بدونه يرجع HTML — نرفضه مبكرًا
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype:
            raise RuntimeError(
                "الرابط أرجع صفحة HTML وليس ISO — رابط مايكروسوفت يحتاج توكن مؤقت "
                "من صفحة التحميل. مرّر ملف ISO جاهز عبر --iso بدلًا من ذلك."
            )

        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):  # 1 MiB
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done/1e6:,.0f} / {total/1e6:,.0f} MB", end="")
        print()
    log.info("✅ تم تحميل ISO: %s", dest)


# ---------------------------------------------------------------------------
# جلب آخر تحديث تراكمي من Microsoft Update Catalog (online)
# ---------------------------------------------------------------------------
def _download_stream(url: str, dest: Path):
    """تنزيل باتصال واحد (احتياطي عند عدم دعم Range)."""
    with requests.get(url, stream=True, timeout=120, headers=HTTP_HEADERS) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done/1e6:,.0f} / {total/1e6:,.0f} MB", end="")
        print()


def _download_segment(url, start, end, dest, counter, lock, total):
    """ينزّل مقطع [start,end] ويكتبه في موضعه داخل الملف."""
    headers = dict(HTTP_HEADERS, Range=f"bytes={start}-{end}")
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "r+b") as f:
            f.seek(start)
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                with lock:
                    counter[0] += len(chunk)
                    if total:
                        print(f"\r  {counter[0]/1e6:,.0f} / {total/1e6:,.0f} MB "
                              f"({counter[0]*100//total}%)", end="")


def _download_parallel(url: str, dest: Path, connections: int) -> bool:
    """تنزيل متعدّد الاتصالات عبر HTTP Range. يرجّع False إن تعذّر (لا دعم Range)."""
    try:
        h = requests.head(url, timeout=30, headers=HTTP_HEADERS, allow_redirects=True)
        size = int(h.headers.get("Content-Length", 0))
        accept = h.headers.get("Accept-Ranges", "").lower() == "bytes"
    except requests.RequestException:
        return False
    if not size or not accept or size < (8 << 20):
        return False

    # احجز حجم الملف مسبقًا
    with open(dest, "wb") as f:
        f.truncate(size)

    seg = size // connections
    ranges = [(i * seg, (size - 1) if i == connections - 1 else (i + 1) * seg - 1)
              for i in range(connections)]
    counter, lock = [0], threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as ex:
        futs = [ex.submit(_download_segment, url, s, e, dest, counter, lock, size)
                for s, e in ranges]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()  # يرفع أي استثناء من المقاطع
    print()
    return True


def _download_file(url: str, dest: Path, connections: int = 8):
    log.info("📥 تنزيل: %s", dest.name)
    # 1) aria2c لو موجود (الأسرع)
    if shutil.which("aria2c"):
        log.info("⚡ استخدام aria2c بـ %d اتصال", connections)
        run(["aria2c", f"-x{connections}", f"-s{connections}", "-k1M",
             "--dir", str(dest.parent), "--out", dest.name,
             "--console-log-level=warn", "--summary-interval=10",
             "--allow-overwrite=true", url], capture_output=False)
        return
    # 2) تنزيل متوازٍ داخلي
    try:
        if _download_parallel(url, dest, connections):
            return
    except Exception as e:
        log.warning("تعذّر التنزيل المتوازي (%s) — رجوع لاتصال واحد", e)
    # 3) احتياطي: اتصال واحد
    _download_stream(url, dest)


def _catalog_search(query: str):
    """يبحث في الكتالوج ويرجّع قائمة (guid, title, date) مرتّبة من الأحدث."""
    log.info("🌐 بحث في كتالوج مايكروسوفت: %s", query)
    r = requests.get(CATALOG_SEARCH, params={"q": query},
                     timeout=60, headers=HTTP_HEADERS)
    r.raise_for_status()
    html = r.text

    if "We did not find any results" in html:
        return []

    results = []
    # كل صف نتيجة يحتوي رابطًا بمعرّف GUID_link وعنوان، وخلية تاريخ MM/DD/YYYY
    # ملاحظة: الكتالوج يخلط بين علامات الاقتباس المفردة والمزدوجة
    for row in re.findall(r"<tr[^>]*id=[\"'][^\"']*_R\d+[\"'].*?</tr>", html, re.DOTALL):
        gm = re.search(r"id=[\"']([0-9a-fA-F-]{36})_link[\"']", row)
        if not gm:
            continue
        guid = gm.group(1)
        tm = re.search(r"_link[\"'].*?>(.*?)</a>", row, re.DOTALL)
        title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
        dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", row)
        date = dm.group(1) if dm else "1/1/1970"
        results.append((guid, title, date))

    def _key(item):
        mo, da, yr = (int(x) for x in item[2].split("/"))
        return (yr, mo, da)

    results.sort(key=_key, reverse=True)
    return results


def _catalog_download_urls(guid: str):
    """يطلب روابط التنزيل الفعلية (.msu/.cab) لمعرّف تحديث."""
    body = {
        "updateIDs": json.dumps(
            [{"size": 0, "languages": "", "uidInfo": guid, "updateID": guid}]
        )
    }
    r = requests.post(CATALOG_DOWNLOAD, data=body, timeout=60, headers=HTTP_HEADERS)
    r.raise_for_status()
    return re.findall(r"'(https?://[^']+\.(?:msu|cab))'", r.text)


def fetch_latest_updates(build: int, updates_dir: Path, arch: str = "x64",
                         connections: int = 8) -> int:
    """
    يجلب آخر تحديث تراكمي مطابق للـ build إلى updates_dir.
    يرجّع عدد الملفات المنزَّلة.
    """
    version = WIN11_BUILD_TO_VERSION.get(build)
    if version:
        query = f"Cumulative Update for Windows 11 Version {version} {arch}"
    else:
        query = f"Cumulative Update Windows 11 {arch} {build}"
        log.warning("build %d غير معروف في الخريطة — بحث عام", build)

    try:
        results = _catalog_search(query)
    except requests.RequestException as e:
        log.error("تعذّر الوصول لكتالوج مايكروسوفت: %s", e)
        return 0

    # استبعد تحديثات .NET و Dynamic، خذ أحدث تحديث OS تراكمي
    cu = next((x for x in results
               if "cumulative update" in x[1].lower()
               and ".net" not in x[1].lower()
               and "dynamic" not in x[1].lower()), None)
    if not cu:
        log.warning("لم يُعثر على تحديث تراكمي مطابق في الكتالوج.")
        return 0

    guid, title, date = cu
    log.info("🎯 أحدث تحديث: %s  (تاريخ %s)", title, date)

    urls = _catalog_download_urls(guid)
    if not urls:
        log.warning("لم تُستخرج روابط التنزيل — قد يكون شكل الكتالوج تغيّر.")
        return 0

    updates_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for url in urls:
        dest = updates_dir / url.split("/")[-1]
        # كاش: لو الملف موجود وبنفس الحجم البعيد، تخطَّه (لا إعادة تحميل)
        if dest.exists():
            try:
                remote = int(requests.head(url, timeout=30, headers=HTTP_HEADERS,
                                           allow_redirects=True)
                             .headers.get("Content-Length", 0))
            except requests.RequestException:
                remote = 0
            if remote and dest.stat().st_size == remote:
                log.info("✔️ موجود مسبقًا (كاش): %s", dest.name)
                count += 1
                continue
            log.info("♻️ ملف ناقص/مختلف — إعادة تنزيل: %s", dest.name)
        _download_file(url, dest, connections)
        count += 1
    log.info("✅ نُزِّل %d ملف تحديث إلى %s", count, updates_dir)
    return count


# ---------------------------------------------------------------------------
# قراءة build من install.wim (parsing صحيح ومستقل عن اللغة)
# ---------------------------------------------------------------------------
def get_build_version(wim_file: Path):
    """يرجع أعلى build بين كل الإصدارات داخل الـ WIM، أو None."""
    log.info("🔍 فحص نسخة الويندوز...")
    out = run([DISM, "/English", "/Get-WimInfo", f"/WimFile:{wim_file}"],
              capture_output=True, text=True).stdout

    builds = []
    for m in re.finditer(r"Version\s*:\s*(\d+)\.(\d+)\.(\d+)\.(\d+)", out):
        build = int(m.group(3))   # الحقل الثالث هو الـ build الحقيقي (مو الأخير)
        builds.append(build)

    if not builds:
        log.warning("تعذّر استخراج رقم الـ build من مخرجات dism")
        return None
    top = max(builds)
    log.info("📊 أعلى Build في الـ WIM: %d", top)
    return top


# ---------------------------------------------------------------------------
# تركيب ISO واستخراجه فعليًا إلى مجلد عمل
# ---------------------------------------------------------------------------
def extract_iso(iso: Path, extract_dir: Path):
    log.info("📂 تركيب ISO واستخراجه...")
    ps_mount = (
        f"$img = Mount-DiskImage -ImagePath '{iso}' -PassThru; "
        f"($img | Get-Volume).DriveLetter"
    )
    drive = run(["powershell", "-NoProfile", "-Command", ps_mount],
                capture_output=True, text=True).stdout.strip()
    if not drive:
        raise RuntimeError("تعذّر تحديد حرف الدرايف بعد تركيب الـ ISO")

    src = f"{drive}:\\"
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        # robocopy: نسخ مرآة كاملة؛ رموز الخروج <8 تعتبر نجاحًا
        rc = subprocess.run(
            ["robocopy", src, str(extract_dir), "/E", "/NFL", "/NDL", "/NP", "/R:2", "/W:2"]
        ).returncode
        if rc >= 8:
            raise RuntimeError(f"فشل robocopy (رمز {rc})")
        log.info("✅ تم استخراج محتوى الـ ISO إلى %s", extract_dir)
    finally:
        run(["powershell", "-NoProfile", "-Command",
             f"Dismount-DiskImage -ImagePath '{iso}'"])


# ---------------------------------------------------------------------------
# دمج التحديثات في كل الإصدارات داخل install.wim
# ---------------------------------------------------------------------------
def cleanup_stale_mounts(mount_dir: Path):
    """يفك أي تركيب سابق عالق ويُنظّف موارد التركيبات التالفة (يتجاهل الأخطاء)."""
    info = subprocess.run([DISM, "/Get-MountedImageInfo"],
                          capture_output=True, text=True).stdout
    if "Mount Dir" in info:
        log.warning("🧹 يوجد تركيب سابق عالق — يتم فكّه وتنظيفه...")
    # فكّ تركيب المجلد المستهدف لو معلّق (يتجاهل بهدوء لو غير مركّب)
    subprocess.run([DISM, "/Unmount-Wim", f"/MountDir:{mount_dir}", "/Discard"],
                   capture_output=True, text=True)
    # نظّف موارد كل التركيبات التالفة/المهجورة
    subprocess.run([DISM, "/Cleanup-Wim"], capture_output=True, text=True)


def integrate_updates(wim_file: Path, mount_dir: Path, updates_dir: Path):
    if not wim_file.exists():
        raise FileNotFoundError(wim_file)
    if not updates_dir.exists():
        log.warning("مجلد التحديثات غير موجود: %s — لا شيء لدمجه", updates_dir)
        return False

    packages = [p for p in updates_dir.iterdir()
                if p.suffix.lower() in (".cab", ".msu")]
    if not packages:
        log.warning("لا توجد حزم .cab/.msu في %s — لا شيء لدمجه", updates_dir)
        return False

    # تحذير حجم FAT32: install.wim المحدّث قد يتجاوز 4GB ويفشل على الفلاشة
    fs = get_fs_type(wim_file)
    if fs.upper() == "FAT32":
        log.warning("⚠️ الوسط بنظام FAT32 (حد الملف 4GB). لو كبر install.wim "
                    "بعد الدمج قد يفشل الحفظ — يُفضّل وسط NTFS/exFAT أو تقسيم install.swm.")

    # اجعل الصورة قابلة للكتابة (تخرج للقراءة فقط من ISO/الفلاشة)
    try:
        os.chmod(wim_file, 0o666)
    except OSError:
        pass

    for idx in list_indices_img(wim_file):
        log.info("🔧 خدمة الإصدار index:%d ...", idx)
        mount_dir.mkdir(parents=True, exist_ok=True)
        cleanup_stale_mounts(mount_dir)  # ينظّف أي تركيب عالق من تشغيلة سابقة
        run([DISM, "/Mount-Wim", f"/WimFile:{wim_file}",
             f"/Index:{idx}", f"/MountDir:{mount_dir}"])
        try:
            for pkg in packages:
                log.info("  ➕ %s", pkg.name)
                run([DISM, f"/Image:{mount_dir}", "/Add-Package",
                     f"/PackagePath:{pkg}"])
            # تقليل الحجم بعد الدمج
            run([DISM, f"/Image:{mount_dir}", "/Cleanup-Image",
                 "/StartComponentCleanup", "/ResetBase"])
            run([DISM, "/Unmount-Wim", f"/MountDir:{mount_dir}", "/Commit"])
        except Exception:
            log.exception("فشل أثناء الخدمة — تراجع بدون حفظ")
            subprocess.run([DISM, "/Unmount-Wim",
                            f"/MountDir:{mount_dir}", "/Discard"])
            raise
    log.info("✅ تم دمج التحديثات في كل الإصدارات")
    return True


# ---------------------------------------------------------------------------
# بناء ISO جديد بإقلاع مزدوج UEFI + BIOS
# ---------------------------------------------------------------------------
def build_iso(extract_dir: Path, out_iso: Path):
    log.info("📀 بناء ISO جديد (UEFI + BIOS)...")
    bios = extract_dir / "boot" / "etfsboot.com"
    uefi = extract_dir / "efi" / "microsoft" / "boot" / "efisys.bin"
    if not bios.exists() or not uefi.exists():
        raise FileNotFoundError("ملفات الإقلاع etfsboot.com/efisys.bin غير موجودة")

    bootdata = f"2#p0,e,b{bios}#pEF,e,b{uefi}"
    run(["oscdimg", f"-bootdata:{bootdata}", "-u2", "-h", "-m", "-o",
         str(extract_dir), str(out_iso)])
    log.info("✅ ISO جاهز: %s", out_iso)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="أداة خدمة وتحديث ويندوز: تكتشف وسط التثبيت تلقائيًا على أي قرص "
                    "(فلاشة/ISO) وتدمج التحديثات في install.wim/.esd")
    ap.add_argument("--media", type=Path,
                    help="جذر وسط التثبيت يدويًا (مثل E:\\). الافتراضي: اكتشاف تلقائي لكل الأقراص")
    ap.add_argument("--no-detect", action="store_true",
                    help="تعطيل الاكتشاف التلقائي واستخدام وضع الـ ISO أدناه")
    ap.add_argument("--iso", type=Path, default=Path("windows.iso"),
                    help="مسار ISO جاهز (يُستخدم فقط إذا لم يُكتشف وسط أو مع --no-detect)")
    ap.add_argument("--url", help="رابط تحميل ISO (يحتاج توكن صالح)")
    ap.add_argument("--extract", type=Path, default=Path(r"C:\win_iso"))
    ap.add_argument("--work", type=Path, default=Path(r"C:\win_iso_work"),
                    help="مجلد عمل محلي (NTFS) لتحويل ESD→WIM عند الحاجة")
    ap.add_argument("--mount", type=Path, default=Path(r"C:\mount"))
    ap.add_argument("--updates", type=Path, default=None,
                    help="مجلد التحديثات. الافتراضي: <الفلاشة>\\updates (كاش يبقى مع الفلاشة "
                         "فلا يعيد التحميل)، أو C:\\updates في وضع الـ ISO")
    ap.add_argument("--out", type=Path, default=None,
                    help="بناء ISO جديد بعد الدمج (اختياري؛ الفلاشة تتحدّث في مكانها بدونه)")
    ap.add_argument("--min-build", type=int, default=DEFAULT_MIN_BUILD)
    ap.add_argument("--no-online", action="store_true",
                    help="عدم جلب التحديثات من كتالوج مايكروسوفت (دمج المحلي فقط)")
    ap.add_argument("--arch", default="x64", help="معمارية التحديث (x64/arm64)")
    ap.add_argument("--connections", type=int, default=8,
                    help="عدد اتصالات التنزيل المتوازي (افتراضي 8؛ جرّب 16 لسرعة أعلى)")
    ap.add_argument("--dism", type=Path,
                    help="مسار dism.exe أحدث (مثل تبع ADK)؛ الافتراضي اكتشاف تلقائي")
    ap.add_argument("--force", action="store_true",
                    help="تجاوز فحص توافق DISM (متوقّع الفشل مع الصور الأحدث)")
    ap.add_argument("--log", type=Path, default=Path("iso_updater.log.txt"),
                    help="مسار ملف اللوق النصّي")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # لوق على الشاشة + ملف txt (utf-8 لدعم العربية والرموز)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(args.log, encoding="utf-8"),
        ],
    )
    log.info("📝 يُكتب اللوق في: %s", args.log.resolve())

    if not is_admin():
        log.error("❌ شغّل السكربت كـ Administrator (DISM يتطلب صلاحيات مرتفعة).")
        sys.exit(1)

    global DISM
    DISM = resolve_dism(args.dism)

    # ===== وضع 1: اكتشاف وسط تثبيت محروق على أي قرص (فلاشة C/D/E/...) =====
    media_root, img = (None, None)
    if not args.no_detect:
        media_root, img = find_install_media(args.media)

    if img is not None:
        log.info("🚀 وضع الوسط المباشر — الجذر: %s", media_root)
        # افتراضيًا احفظ التحديثات على الفلاشة نفسها (كاش يبقى معها)
        updates_dir = args.updates or (media_root / "updates")
        log.info("📦 مجلد التحديثات (كاش): %s", updates_dir)
        # حوّل ESD→WIM عند الحاجة (الناتج محلي قابل للخدمة)
        wim = ensure_wim(img, args.work)

        build = get_build_version(wim)
        if build is not None and build >= args.min_build:
            log.info("✅ النسخة حديثة (build %d ≥ %d)", build, args.min_build)
        else:
            log.warning("⚠️ النسخة أقدم من المطلوب — سيتم دمج التحديثات لرفعها")

        # تأكد أن DISM يقدر يخدم هذه الصورة قبل أي تنزيل/دمج
        check_dism_compat(build, args.force)

        # جلب آخر تحديث تراكمي من الموقع تلقائيًا
        if not args.no_online:
            fetch_latest_updates(build or args.min_build, updates_dir,
                                 args.arch, args.connections)

        merged = integrate_updates(wim, args.mount, updates_dir)

        if not merged:
            log.warning("🟡 لم يتغيّر أي ملف — لا توجد تحديثات للدمج. "
                        "حط ملفات .msu/.cab في %s ثم أعد التشغيل.", updates_dir)
            return

        # لو كانت ESD محوّلة محليًا: انسخ WIM المحدّث للفلاشة واحذف ESD القديم
        if wim != img:
            target = media_root / "sources" / "install.wim"
            log.info("📤 نسخ install.wim المحدّث إلى الفلاشة: %s", target)
            shutil.copy2(wim, target)
            try:
                img.unlink()  # احذف install.esd القديم ليعتمد الإعداد على الـ wim
                log.info("🗑️ حُذف install.esd القديم")
            except OSError:
                log.warning("تعذّر حذف install.esd القديم — احذفه يدويًا إن لزم")

        if args.out:  # بناء ISO اختياري؛ الفلاشة أصلًا قابلة للإقلاع
            build_iso(media_root, args.out)
        log.info("🔥 تم بنجاح ✅ — اندمجت التحديثات والوسط محدّث في مكانه: %s",
                 media_root)
        return

    # ===== وضع 2: ملف ISO (تحميل/فك ثم بناء) =====
    log.info("ℹ️ لم يُكتشف وسط محروق — التحويل إلى وضع الـ ISO")
    if not args.iso.exists():
        if not args.url:
            log.error("لا يوجد وسط مكتشف ولا ISO ولا --url للتحميل.")
            sys.exit(1)
        download_iso(args.url, args.iso)

    install_wim = args.extract / "sources" / "install.wim"
    if not install_wim.exists():
        extract_iso(args.iso, args.extract)

    build = get_build_version(install_wim)
    if build is not None and build >= args.min_build:
        log.info("✅ النسخة حديثة (build %d ≥ %d)", build, args.min_build)
    else:
        log.warning("⚠️ النسخة أقدم من المطلوب — سيتم دمج التحديثات لرفعها")

    check_dism_compat(build, args.force)

    updates_dir = args.updates or Path(r"C:\updates")
    if not args.no_online:
        fetch_latest_updates(build or args.min_build, updates_dir,
                             args.arch, args.connections)

    merged = integrate_updates(install_wim, args.mount, updates_dir)
    if not merged:
        log.warning("🟡 لم تُدمج أي تحديثات — حط ملفات .msu/.cab في %s ثم أعد التشغيل.",
                    updates_dir)
    out = args.out or Path(r"C:\updated_windows.iso")
    build_iso(args.extract, out)
    log.info("🔥 انتهى ✅  →  %s", out)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # يسجّل الـ traceback الكامل في ملف اللوق + الشاشة، ثم يخرج برمز خطأ
        logging.getLogger("iso_updater").exception("💥 فشل التنفيذ — التفاصيل أدناه")
        sys.exit(1)

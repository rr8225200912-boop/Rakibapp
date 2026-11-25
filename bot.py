"""
APK Builder Bot  (Telegram -> Gradle -> APK)

কাজের ধারা:
  1) তুমি Android প্রজেক্টের .zip পাঠাও
  2) বট অ্যাপের নাম চায়
  3) বট লোগো (PNG) চায়
  4) বট বিল্ড করে APK ফেরত পাঠায়

Railway Variables:
  BOT_TOKEN          BotFather থেকে পাওয়া টোকেন            (আবশ্যক)
  OWNER_ID           তোমার Telegram numeric ID                (আবশ্যক)
  GRADLE_XMX         ঐচ্ছিক, যেমন 2g (না দিলে RAM দেখে নিজে ঠিক করে)
  BUILD_TIMEOUT_MIN  ঐচ্ছিক, ডিফল্ট 40
  GRADLE_TASK        ঐচ্ছিক, ডিফল্ট assembleDebug
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import zipfile
from collections import deque
from queue import Queue

import requests

try:
    from PIL import Image, ImageDraw
except Exception:  # Pillow না থাকলে লোগো ফিচার বন্ধ থাকবে, বাকি সব চলবে
    Image = None
    ImageDraw = None

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNERS = {
    int(x)
    for x in re.split(r"[,\s]+", os.environ.get("OWNER_ID", ""))
    if x.strip().isdigit()
}
API = "https://api.telegram.org/bot%s" % TOKEN
FILE_API = "https://api.telegram.org/file/bot%s" % TOKEN

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024   # Telegram বট-ডাউনলোড সীমা
MAX_APK_BYTES = 49 * 1024 * 1024        # Telegram বট-আপলোড সীমা
MAX_UNZIPPED_BYTES = 600 * 1024 * 1024
BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT_MIN", "40")) * 60
GRADLE_TASK = os.environ.get("GRADLE_TASK", "assembleDebug")
ANDROID_HOME = os.environ.get("ANDROID_HOME", "/opt/android-sdk")
WORK = os.environ.get("WORK_DIR", "/tmp/builds")
SESS_DIR = os.path.join(WORK, "_sessions")

jobs = Queue()
sessions = {}   # chat_id -> {"stage": "name"|"logo", ...}
state = {"proc": None, "cancel": False, "busy": False, "current": None}


# ============================================================ Telegram helpers

def tg(method, **params):
    for _ in range(3):
        try:
            r = requests.post("%s/%s" % (API, method), data=params, timeout=60)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if data.get("error_code") == 429:
                time.sleep(data.get("parameters", {}).get("retry_after", 3) + 1)
                continue
            if "not modified" not in str(data.get("description", "")):
                print("TG error", method, data, flush=True)
            return None
        except Exception as e:  # noqa
            print("TG exception", method, e, flush=True)
            time.sleep(2)
    return None


def send(chat_id, text):
    last = None
    text = text or " "
    for i in range(0, len(text), 3900):
        last = tg("sendMessage", chat_id=chat_id, text=text[i:i + 3900])
    return last


def edit(chat_id, message_id, text):
    if message_id:
        tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:3900])


def send_document(chat_id, path, filename, caption=""):
    for _ in range(3):
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    "%s/sendDocument" % API,
                    data={"chat_id": chat_id, "caption": caption[:1000]},
                    files={"document": (filename, f)},
                    timeout=900,
                )
            if r.json().get("ok"):
                return True
            print("sendDocument failed", r.text[:300], flush=True)
        except Exception as e:  # noqa
            print("sendDocument exception", e, flush=True)
        time.sleep(3)
    return False


def send_log(chat_id, log_path, caption):
    """খুব বড় লগ হলে শুধু শেষের অংশ পাঠায়।"""
    try:
        size = os.path.getsize(log_path)
        path = log_path
        if size > 40 * 1024 * 1024:
            path = log_path + ".tail.txt"
            with open(log_path, "rb") as src, open(path, "wb") as dst:
                src.seek(max(0, size - 800 * 1024))
                dst.write(src.read())
        send_document(chat_id, path, "build-log.txt", caption)
    except Exception as e:  # noqa
        print("send_log failed", e, flush=True)


def download(file_id, dest):
    info = tg("getFile", file_id=file_id)
    if not info or "file_path" not in info:
        raise RuntimeError("ফাইলটি নামানো যায়নি (২০ MB-র বেশি হলে এমন হয়)।")
    with requests.get("%s/%s" % (FILE_API, info["file_path"]), stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)


# ============================================================ Memory / Gradle settings

def mem_limit_mb():
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v.isdigit() and int(v) < (1 << 50):
                return int(v) // 1048576
        except Exception:
            pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 4096


def gradle_xmx():
    env = os.environ.get("GRADLE_XMX", "").strip().lower()
    if env:
        return env
    mb = mem_limit_mb()
    return "%dm" % max(768, min(3072, int(mb * 0.55)))


# ============================================================ Project helpers

def safe_extract(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            parts = m.filename.replace("\\", "/").split("/")
            if parts[0] == "__MACOSX" or ".." in parts:
                continue
            total += m.file_size
            if total > MAX_UNZIPPED_BYTES:
                raise RuntimeError("zip খুলে অনেক বড় হয়ে যাচ্ছে।")
            target = os.path.realpath(os.path.join(dest, *parts))
            if not (target == dest_real or target.startswith(dest_real + os.sep)):
                continue
            if m.is_dir() or m.filename.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(m) as srcf, open(target, "wb") as out:
                    shutil.copyfileobj(srcf, out)


def find_root(src):
    best = None
    for dirpath, dirnames, filenames in os.walk(src):
        depth = dirpath[len(src):].count(os.sep)
        if depth > 3:
            dirnames[:] = []
            continue
        if "settings.gradle" in filenames or "settings.gradle.kts" in filenames:
            if best is None or depth < best[0]:
                best = (depth, dirpath)
    return best[1] if best else None


def prepare(root):
    """local.properties বানায়, কোন Gradle কমান্ড চলবে সেটা ফেরত দেয়।"""
    with open(os.path.join(root, "local.properties"), "w") as f:
        f.write("sdk.dir=%s\n" % ANDROID_HOME)
    gradlew = os.path.join(root, "gradlew")
    jar = os.path.join(root, "gradle", "wrapper", "gradle-wrapper.jar")
    if os.path.exists(gradlew) and os.path.exists(jar):
        try:
            with open(gradlew, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
            with open(gradlew, "wb") as f:
                f.write(data)
        except Exception:
            pass
        os.chmod(gradlew, 0o755)
        return "./gradlew"
    return "gradle"


def find_apks(root):
    found = []
    pattern = os.path.join(root, "**", "build", "outputs", "apk", "**", "*.apk")
    for p in glob.glob(pattern, recursive=True):
        if "androidTest" in p:
            continue
        found.append(p)
    found.sort(key=lambda p: (("debug" not in p), -os.path.getmtime(p)))
    return found[:3]


def error_summary(lines):
    lines = list(lines)
    for i, ln in enumerate(lines):
        if "What went wrong" in ln:
            return "\n".join(lines[i:i + 14])
    errs = [l for l in lines if l.startswith("e: ") or "error:" in l.lower() or "FAILED" in l]
    return "\n".join(errs[:15]) if errs else "\n".join(lines[-20:])


# ============================================================ Branding (নাম + লোগো)

def res_escape(s):
    s = s.replace("\\", "\\\\").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("'", "\\'").replace('"', '\\"')
    if s[:1] in ("@", "?"):
        s = "\\" + s
    return s


def find_app_module(root):
    """যে মডিউলে <application> আছে এবং Android application প্লাগিন ব্যবহার করে সেটা খোঁজে।"""
    best = None
    skip = {"build", ".gradle", ".git", "node_modules", ".idea"}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in skip]
        if os.path.basename(dp) != "main" or "AndroidManifest.xml" not in fn:
            continue
        if os.path.basename(os.path.dirname(dp)) != "src":
            continue
        manifest = os.path.join(dp, "AndroidManifest.xml")
        try:
            mtxt = open(manifest, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if "<application" not in mtxt:
            continue
        module = os.path.dirname(os.path.dirname(dp))
        score = 1
        for bf in ("build.gradle.kts", "build.gradle"):
            p = os.path.join(module, bf)
            if os.path.exists(p):
                try:
                    if "android.application" in open(p, encoding="utf-8", errors="replace").read():
                        score = 2
                except Exception:
                    pass
        depth = module[len(root):].count(os.sep)
        key = (-score, depth)
        if best is None or key < best[0]:
            best = (key, manifest, os.path.join(dp, "res"))
    return (best[1], best[2]) if best else (None, None)


def set_attr(tag, attr, value):
    pat = re.compile(r'\bandroid:%s\s*=\s*"[^"]*"' % re.escape(attr))
    if pat.search(tag):
        return pat.sub('android:%s="%s"' % (attr, value), tag, count=1)
    return tag.replace("<application", '<application android:%s="%s"' % (attr, value), 1)


def edit_manifest(path, set_label, set_icon):
    txt = open(path, encoding="utf-8").read()
    m = re.search(r"<application\b[^>]*>", txt)
    if not m:
        raise RuntimeError("AndroidManifest-এ <application> পাইনি")
    tag = m.group(0)
    if set_label:
        tag = set_attr(tag, "label", "@string/app_name")
    if set_icon:
        tag = set_attr(tag, "icon", "@mipmap/ic_launcher")
        tag = set_attr(tag, "roundIcon", "@mipmap/ic_launcher_round")
    txt = txt[:m.start()] + tag + txt[m.end():]
    if set_label:
        def fix_activity(mm):
            t = mm.group(0)
            if re.search(r'\bandroid:label\s*=', t):
                t = re.sub(r'\bandroid:label\s*=\s*"[^"]*"', 'android:label="@string/app_name"', t, count=1)
            return t
        txt = re.sub(r"<activity(?:-alias)?\b[^>]*>", fix_activity, txt)
    open(path, "w", encoding="utf-8").write(txt)


def set_app_name(res_dir, name):
    esc = res_escape(name)
    pat = re.compile(r'(<string\b[^>]*\bname\s*=\s*"app_name"[^>]*>)(.*?)(</string>)', re.S)
    found = False
    for p in glob.glob(os.path.join(res_dir, "values*", "*.xml")):
        try:
            t = open(p, encoding="utf-8").read()
        except Exception:
            continue
        if pat.search(t):
            t = pat.sub(lambda m: m.group(1) + esc + m.group(3), t)
            open(p, "w", encoding="utf-8").write(t)
            found = True
    if not found:
        d = os.path.join(res_dir, "values")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "pb_app_name.xml"), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                    '    <string name="app_name">%s</string>\n</resources>\n' % esc)


def load_logo_square(path):
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    s = max(w, h)
    sq = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sq.paste(img, ((s - w) // 2, (s - h) // 2))
    if s > 1024:
        sq = sq.resize((1024, 1024), Image.LANCZOS)
    return sq


def pick_bg(img):
    small = img.resize((64, 64), Image.LANCZOS)
    px = small.load()
    corners = [px[1, 1], px[62, 1], px[1, 62], px[62, 62]]
    solid = [c for c in corners if c[3] > 200]
    if len(solid) >= 3:
        r = sum(c[0] for c in solid) // len(solid)
        g = sum(c[1] for c in solid) // len(solid)
        b = sum(c[2] for c in solid) // len(solid)
        return "#%02X%02X%02X" % (r, g, b)
    total, n = 0.0, 0
    for x in range(64):
        for y in range(64):
            r, g, b, a = px[x, y]
            if a > 128:
                total += 0.299 * r + 0.587 * g + 0.114 * b
                n += 1
    lum = total / n if n else 0
    return "#1F1F1F" if lum > 140 else "#FFFFFF"


def make_icon(logo, bg_hex, size, shape, scale):
    bg = tuple(int(bg_hex[i:i + 2], 16) for i in (1, 3, 5))
    base = Image.new("RGBA", (size, size), bg + (255,))
    side = max(1, int(size * scale))
    lg = logo.resize((side, side), Image.LANCZOS)
    off = (size - side) // 2
    base.alpha_composite(lg, (off, off))
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    if shape == "round":
        d.ellipse((0, 0, size - 1, size - 1), fill=255)
    else:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.18), fill=255)
    base.putalpha(mask)
    return base


def apply_logo(res_dir, logo_path):
    if Image is None:
        raise RuntimeError("Pillow ইনস্টল নেই")
    logo = load_logo_square(logo_path)
    bg = pick_bg(logo)

    # পুরনো আইকন মুছি
    for d in glob.glob(os.path.join(res_dir, "mipmap*")):
        for f in glob.glob(os.path.join(d, "ic_launcher*")):
            try:
                os.remove(f)
            except Exception:
                pass

    legacy = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    foreground = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
    for dens, size in legacy.items():
        d = os.path.join(res_dir, "mipmap-" + dens)
        os.makedirs(d, exist_ok=True)
        make_icon(logo, bg, size, "square", 0.82).save(os.path.join(d, "ic_launcher.png"), optimize=True)
        make_icon(logo, bg, size, "round", 0.72).save(os.path.join(d, "ic_launcher_round.png"), optimize=True)
    for dens, size in foreground.items():
        d = os.path.join(res_dir, "mipmap-" + dens)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        side = int(size * 0.62)
        lg = logo.resize((side, side), Image.LANCZOS)
        canvas.alpha_composite(lg, ((size - side) // 2, (size - side) // 2))
        canvas.save(os.path.join(d, "pb_icon_fg.png"), optimize=True)

    v = os.path.join(res_dir, "values")
    os.makedirs(v, exist_ok=True)
    with open(os.path.join(v, "pb_icon_bg.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                '    <color name="pb_icon_bg">%s</color>\n</resources>\n' % bg)
    ad = os.path.join(res_dir, "mipmap-anydpi-v26")
    os.makedirs(ad, exist_ok=True)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
           '    <background android:drawable="@color/pb_icon_bg"/>\n'
           '    <foreground android:drawable="@mipmap/pb_icon_fg"/>\n'
           '</adaptive-icon>\n')
    for n in ("ic_launcher.xml", "ic_launcher_round.xml"):
        with open(os.path.join(ad, n), "w", encoding="utf-8") as f:
            f.write(xml)


def apply_branding(root, app_name, logo_path):
    """নাম ও লোগো বসায়। কী কী হলো তার তালিকা ফেরত দেয়।"""
    done = []
    manifest, res_dir = find_app_module(root)
    if not manifest:
        raise RuntimeError("অ্যাপ মডিউল (AndroidManifest.xml) খুঁজে পাইনি")
    os.makedirs(res_dir, exist_ok=True)
    if app_name:
        set_app_name(res_dir, app_name)
        done.append("নাম")
    if logo_path:
        apply_logo(res_dir, logo_path)
        done.append("লোগো")
    edit_manifest(manifest, bool(app_name), bool(logo_path))
    return done


# ============================================================ Build

def run_build(chat_id, root, cmd_name, status_id, workdir):
    log_path = os.path.join(workdir, "build-log.txt")
    tail = deque(maxlen=400)
    env = dict(os.environ)
    env["ANDROID_HOME"] = ANDROID_HOME
    env["ANDROID_SDK_ROOT"] = ANDROID_HOME
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        env["GRADLE_USER_HOME"] = "/data/gradle"
        os.makedirs("/data/gradle", exist_ok=True)

    jvm = "-Xmx%s -XX:MaxMetaspaceSize=768m -XX:+UseSerialGC -Dfile.encoding=UTF-8" % gradle_xmx()
    cmd = [
        cmd_name, GRADLE_TASK,
        "--no-daemon", "--no-parallel", "--max-workers=2", "--no-watch-fs",
        "--console=plain",
        "-Dorg.gradle.jvmargs=" + jvm,
        "-Pkotlin.compiler.execution.strategy=in-process",
    ]
    proc = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1, start_new_session=True,
    )
    state["proc"] = proc

    def reader():
        with open(log_path, "w", encoding="utf-8") as lf:
            for line in proc.stdout:
                lf.write(line)
                tail.append(line.rstrip("\n"))

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    started = time.time()
    last_edit = 0
    timed_out = False
    while proc.poll() is None:
        time.sleep(3)
        elapsed = time.time() - started
        if state["cancel"]:
            break
        if elapsed > BUILD_TIMEOUT:
            timed_out = True
            break
        if time.time() - last_edit > 45:
            last_edit = time.time()
            edit(chat_id, status_id,
                 "🔨 বিল্ড চলছে... %d মিনিট %d সেকেন্ড\n(প্রথমবার ১০-২০ মিনিট লাগতে পারে)"
                 % (elapsed // 60, elapsed % 60))
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except Exception:
            pass
    t.join(timeout=10)
    state["proc"] = None
    return proc.returncode, timed_out, log_path, tail


def run_job(job):
    chat_id = job["chat_id"]
    name = job["zip_name"]
    app_name = job.get("app_name")
    logo = job.get("logo_path")
    os.makedirs(WORK, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="b_", dir=WORK)
    state.update(busy=True, cancel=False, current=app_name or name)
    try:
        status = send(chat_id, "⬇️ \"%s\" নামাচ্ছি..." % name)
        status_id = status["message_id"] if status else None
        zip_path = os.path.join(workdir, "src.zip")
        download(job["file_id"], zip_path)

        src = os.path.join(workdir, "src")
        safe_extract(zip_path, src)
        root = find_root(src)
        if not root:
            edit(chat_id, status_id, "❌ zip-এর ভেতরে settings.gradle বা settings.gradle.kts পাইনি। "
                 "Android Studio প্রজেক্টের পুরো ফোল্ডারটা zip করে পাঠাও।")
            return

        brand_note = ""
        if app_name or logo:
            try:
                done = apply_branding(root, app_name, logo)
                brand_note = "🎨 %s বসানো হয়েছে।\n" % " ও ".join(done)
            except Exception as e:  # noqa
                traceback.print_exc()
                brand_note = "⚠️ নাম/লোগো বসাতে পারিনি (%s), তবু বিল্ড করছি।\n" % e

        cmd_name = prepare(root)
        edit(chat_id, status_id, "%s🔨 বিল্ড শুরু হয়েছে (RAM %d MB, Gradle %s)..."
             % (brand_note, mem_limit_mb(), gradle_xmx()))

        rc, timed_out, log_path, tail = run_build(chat_id, root, cmd_name, status_id, workdir)

        if state["cancel"]:
            edit(chat_id, status_id, "🛑 বিল্ড বাতিল করা হয়েছে।")
            return
        if timed_out:
            edit(chat_id, status_id, "⏰ সময় শেষ (%d মিনিট)। বিল্ড থামিয়ে দিয়েছি।" % (BUILD_TIMEOUT // 60))
            send_log(chat_id, log_path, "বিল্ড লগ")
            return
        if rc != 0:
            edit(chat_id, status_id, "❌ বিল্ড ফেল করেছে।")
            text = "\n".join(tail)
            hint = ""
            if rc in (-9, 137) or "OutOfMemoryError" in text or "GC overhead" in text \
                    or "out of JVM memory" in text:
                hint = ("\n\n💡 মেমোরি শেষ হয়ে গেছে। Railway-তে service-এর RAM বাড়াও "
                        "(কমপক্ষে ৪ GB), তারপর আবার পাঠাও।")
            send(chat_id, "কারণ (সংক্ষেপে):\n\n" + error_summary(tail)[:3300] + hint)
            send_log(chat_id, log_path, "পুরো লগ — এটা Claude-কে পাঠালে ঠিক করে দেবে")
            return

        apks = find_apks(root)
        if not apks:
            edit(chat_id, status_id, "⚠️ বিল্ড সফল, কিন্তু কোনো APK পাইনি।")
            send_log(chat_id, log_path, "বিল্ড লগ")
            return

        edit(chat_id, status_id, "✅ বিল্ড সফল! APK পাঠাচ্ছি...")
        base = app_name or re.sub(r"\.zip$", "", name, flags=re.I) or "app"
        base = re.sub(r"[^\w\-]+", "_", base, flags=re.U).strip("_") or "app"
        for i, apk in enumerate(apks):
            size = os.path.getsize(apk)
            fname = base + (".apk" if i == 0 else "_%d.apk" % (i + 1))
            if size > MAX_APK_BYTES:
                send(chat_id, "APK %.1f MB — Telegram-এ পাঠানো যাচ্ছে না (সীমা ৫০ MB)।" % (size / 1048576))
                continue
            ok = send_document(chat_id, apk, fname,
                               "%s (%.1f MB)\nফোনে খুলে ইনস্টল করো।" % (fname, size / 1048576))
            if not ok:
                send(chat_id, "APK পাঠাতে পারিনি, একটু পরে আবার zip পাঠাও।")
    except Exception as e:  # noqa
        traceback.print_exc()
        send(chat_id, "❌ ত্রুটি: %s" % e)
    finally:
        state.update(busy=False, current=None, proc=None)
        shutil.rmtree(workdir, ignore_errors=True)
        if logo:
            try:
                os.remove(logo)
            except Exception:
                pass


def worker():
    while True:
        job = jobs.get()
        try:
            run_job(job)
        except Exception:
            traceback.print_exc()


# ============================================================ Chat flow

HELP = (
    "👋 আমি APK Builder Bot।\n\n"
    "কীভাবে ব্যবহার করবে:\n"
    "1) Android Studio প্রজেক্টের পুরো ফোল্ডার .zip করে পাঠাও (২০ MB-র কম)\n"
    "2) আমি অ্যাপের নাম জিজ্ঞেস করব\n"
    "3) তারপর লোগো (PNG) চাইব\n"
    "4) বিল্ড শেষে APK পাঠিয়ে দেব\n\n"
    "কমান্ড:\n"
    "/skip — নাম বা লোগো না দিয়ে এগিয়ে যাও\n"
    "/status — এখন কী চলছে\n"
    "/cancel — চলমান কাজ বাতিল"
)

ASK_LOGO = (
    "এবার অ্যাপের লোগো পাঠাও (PNG)।\n\n"
    "💡 সেরা কোয়ালিটির জন্য ছবিটা \"File/Document\" হিসেবে পাঠাও, 512×512 বা তার বেশি হলে ভালো।\n"
    "লোগো না দিতে চাইলে /skip লেখো।"
)


def is_image_doc(doc):
    mime = (doc.get("mime_type") or "").lower()
    fname = (doc.get("file_name") or "").lower()
    return mime.startswith("image/") or fname.endswith((".png", ".jpg", ".jpeg", ".webp"))


def clean_name(text):
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text


def enqueue(chat_id):
    s = sessions.pop(chat_id, None)
    if not s:
        return
    pos = jobs.qsize() + (1 if state["busy"] else 0)
    jobs.put({
        "chat_id": chat_id, "file_id": s["file_id"], "zip_name": s["zip_name"],
        "app_name": s.get("app_name"), "logo_path": s.get("logo_path"),
    })
    summary = "নাম: %s | লোগো: %s" % (s.get("app_name") or "আগেরটাই", "আছে" if s.get("logo_path") else "আগেরটাই")
    send(chat_id, "📥 সব পেয়েছি! (%s)\n%s" % (
        summary, "লাইনে %d নম্বরে আছে।" % (pos + 1) if pos else "এখনই বিল্ড শুরু করছি।"))


def receive_logo(chat_id, file_id, size):
    s = sessions.get(chat_id)
    if not s:
        return
    if Image is None:
        send(chat_id, "এই সার্ভারে ছবি প্রসেসিং চালু নেই। /skip দিয়ে এগোও।")
        return
    if size and size > MAX_DOWNLOAD_BYTES:
        send(chat_id, "ছবি অনেক বড় (২০ MB-র বেশি)। ছোট করে আবার পাঠাও।")
        return
    os.makedirs(SESS_DIR, exist_ok=True)
    raw = os.path.join(SESS_DIR, "%s_%d.raw" % (chat_id, int(time.time())))
    out = raw[:-4] + ".png"
    try:
        download(file_id, raw)
        sq = load_logo_square(raw)
        sq.save(out, optimize=True)
    except Exception as e:  # noqa
        print("logo error", e, flush=True)
        send(chat_id, "ছবিটা পড়তে পারিনি। PNG আবার পাঠাও, অথবা /skip লেখো।")
        return
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    s["logo_path"] = out
    w = sq.size[0]
    if w < 192:
        send(chat_id, "⚠️ ছবিটা ছোট (%dpx), আইকন একটু ঝাপসা হতে পারে। তবু ব্যবহার করছি।" % w)
    enqueue(chat_id)


def handle(update):
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if not OWNERS:
        send(chat_id, "🔒 বট এখনো লক করা হয়নি।\nতোমার Telegram ID: %s\n\n"
             "Railway → Variables-এ OWNER_ID = %s দিয়ে সেভ করো, বট নিজে রিস্টার্ট হবে।" % (user_id, user_id))
        return
    if user_id not in OWNERS:
        send(chat_id, "⛔ এই বট শুধু মালিকের জন্য।")
        return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
        return
    if cmd == "/status":
        s = sessions.get(chat_id)
        if state["busy"]:
            send(chat_id, "🔨 বিল্ড চলছে: %s\nঅপেক্ষায়: %d টি" % (state["current"], jobs.qsize()))
        elif s:
            send(chat_id, "⏳ তোমার উত্তরের অপেক্ষায় আছি (%s)।" % ("নাম" if s["stage"] == "name" else "লোগো"))
        else:
            send(chat_id, "✅ বসে আছি, zip পাঠাও।")
        return
    if cmd == "/cancel":
        if chat_id in sessions:
            s = sessions.pop(chat_id)
            if s.get("logo_path"):
                try:
                    os.remove(s["logo_path"])
                except Exception:
                    pass
            send(chat_id, "🛑 বাতিল করেছি। নতুন করে zip পাঠাও।")
        elif state["busy"]:
            state["cancel"] = True
            send(chat_id, "🛑 বিল্ড বাতিল করছি...")
        else:
            send(chat_id, "বাতিল করার মতো কিছু চলছে না।")
        return
    if cmd == "/skip":
        s = sessions.get(chat_id)
        if not s:
            send(chat_id, "আগে একটা zip পাঠাও।")
        elif s["stage"] == "name":
            s["app_name"] = None
            s["stage"] = "logo"
            send(chat_id, "ঠিক আছে, আগের নামই থাকবে।\n\n" + ASK_LOGO)
        else:
            enqueue(chat_id)
        return

    doc = msg.get("document")
    photo = msg.get("photo")

    # নতুন zip
    if doc and (doc.get("file_name") or "").lower().endswith(".zip"):
        if doc.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
            send(chat_id, "zip ২০ MB-র বেশি। build/ ও .gradle ফোল্ডার বাদ দিয়ে আবার zip করো।")
            return
        old = sessions.pop(chat_id, None)
        if old and old.get("logo_path"):
            try:
                os.remove(old["logo_path"])
            except Exception:
                pass
        sessions[chat_id] = {"stage": "name", "file_id": doc["file_id"],
                             "zip_name": doc.get("file_name", "app.zip")}
        send(chat_id, "✅ zip পেয়েছি।\n\nঅ্যাপের নাম কী হবে? (যেমন: PocketBite)\n"
                      "আগের নাম রাখতে চাইলে /skip লেখো।")
        return

    s = sessions.get(chat_id)

    # লোগো
    if s and s["stage"] == "logo":
        if photo:
            receive_logo(chat_id, photo[-1]["file_id"], photo[-1].get("file_size", 0))
            return
        if doc and is_image_doc(doc):
            receive_logo(chat_id, doc["file_id"], doc.get("file_size", 0))
            return
        send(chat_id, "লোগো ছবি (PNG) পাঠাও, অথবা /skip লেখো।")
        return

    # নাম
    if s and s["stage"] == "name":
        if text and not text.startswith("/"):
            name = clean_name(text)
            if not name or len(name) > 40:
                send(chat_id, "নাম ১ থেকে ৪০ অক্ষরের মধ্যে হতে হবে। আবার লেখো।")
                return
            s["app_name"] = name
            s["stage"] = "logo"
            send(chat_id, "নাম: %s ✅\n\n%s" % (name, ASK_LOGO))
        else:
            send(chat_id, "অ্যাপের নামটা টেক্সটে লেখো, অথবা /skip লেখো।")
        return

    if doc:
        send(chat_id, "প্রজেক্ট পাঠাতে .zip ফাইল লাগবে।")
        return
    send(chat_id, "zip ফাইল পাঠাও, অথবা /help লেখো।")


# ============================================================ Main

def on_term(signum, frame):
    p = state.get("proc")
    if p is not None and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    os._exit(0)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN সেট করা নেই।")
    signal.signal(signal.SIGTERM, on_term)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(SESS_DIR, exist_ok=True)

    tg("deleteWebhook")
    threading.Thread(target=worker, daemon=True).start()
    print("Bot started. Owners: %s | RAM: %d MB | Gradle Xmx: %s | Pillow: %s"
          % (OWNERS or "NONE (lock mode)", mem_limit_mb(), gradle_xmx(), Image is not None), flush=True)
    for uid in OWNERS:
        send(uid, "🟢 বট চালু হয়েছে। zip পাঠাও। (RAM: %d MB)" % mem_limit_mb())

    offset = None
    while True:
        try:
            r = requests.get(
                "%s/getUpdates" % API,
                params={"timeout": 50, "offset": offset, "allowed_updates": json.dumps(["message"])},
                timeout=70,
            )
            data = r.json()
            if not data.get("ok"):
                print("getUpdates not ok:", str(data)[:200], flush=True)
                time.sleep(5)
                continue
            for u in data["result"]:
                offset = u["update_id"] + 1
                try:
                    handle(u)
                except Exception:
                    traceback.print_exc()
        except Exception as e:  # noqa
            print("poll error", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()

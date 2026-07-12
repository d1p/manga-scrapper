import os
import zipfile
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image

# ================= CONFIG =================
INPUT_DIR = "input_manga"
OUTPUT_DIR = "output_cbz"
CACHE_DIR = "cache_images"

MAX_VOLUME_SIZE_MB = 300
MAX_WORKERS = 4

SUPPORTED_FORMATS = (".jpg", ".jpeg", ".png", ".webp")

# =========================================

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

lock = threading.Lock()


# ================= STATE =================
class ChapterState:
    def __init__(self, chapter_num, pages_total):
        self.chapter_num = chapter_num
        self.pages_total = pages_total
        self.pages_done = 0
        self.status = "pending"  # pending / processing / done / cached


class VolumeState:
    def __init__(self, volume_num):
        self.volume_num = volume_num
        self.chapters = []
        self.size_estimate = 0
        self.status = "pending"  # pending / building / done


class GlobalState:
    def __init__(self):
        self.chapters = {}
        self.volumes = {}
        self.total_pages = 0
        self.done_pages = 0


STATE = GlobalState()


# ================= UTIL =================
def get_chapter_dirs():
    chapters = []
    for folder in sorted(os.listdir(INPUT_DIR)):
        path = os.path.join(INPUT_DIR, folder)
        if os.path.isdir(path):
            try:
                num = int(folder.lower().replace("chapter", "").strip())
            except:
                continue
            chapters.append((num, path))
    return sorted(chapters, key=lambda x: x[0])


def get_images(folder):
    return sorted([
        f for f in os.listdir(folder)
        if f.lower().endswith(SUPPORTED_FORMATS)
    ])


def estimate_chapter_size(folder):
    total = 0
    for root, _, files in os.walk(folder):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


# ================= CACHE =================
def cached_image_path(chapter_num, image_name):
    return os.path.join(CACHE_DIR, f"ch{chapter_num}_{image_name}")


def process_image(src, dst):
    if os.path.exists(dst):
        return "cached"

    try:
        img = Image.open(src)
        img.save(dst, "JPEG", quality=90)
        return "processed"
    except:
        shutil.copy(src, dst)
        return "processed"


# ================= UI =================
def render_ui():
    os.system("cls" if os.name == "nt" else "clear")

    progress = (
        STATE.done_pages / STATE.total_pages * 100
        if STATE.total_pages else 0
    )

    print("=== Manga Builder ===\n")

    print(f"Progress: {progress:.2f}% ({STATE.done_pages}/{STATE.total_pages} pages)\n")

    print("Volumes:")
    for v in STATE.volumes.values():
        ch_range = f"{min(v.chapters)}–{max(v.chapters)}" if v.chapters else "-"
        print(f"[{v.volume_num}] Ch {ch_range} | {v.status.upper()} | {v.size_estimate // (1024*1024)} MB")
    print()

    print("Chapters:")
    row = ""
    for ch in sorted(STATE.chapters.values(), key=lambda x: x.chapter_num):
        if ch.status == "done":
            icon = "✅"
        elif ch.status == "processing":
            percent = int((ch.pages_done / ch.pages_total) * 100)
            icon = f"🔄{percent}%"
        elif ch.status == "cached":
            icon = "📦"
        else:
            icon = "⏳"

        row += f"{ch.chapter_num:02d}{icon}  "
        if len(row) > 80:
            print(row)
            row = ""

    if row:
        print(row)


# ================= PROCESS =================
def process_chapter(chapter_num, folder):
    images = get_images(folder)
    state = STATE.chapters[chapter_num]

    all_cached = True

    for img_name in images:
        src = os.path.join(folder, img_name)
        dst = cached_image_path(chapter_num, img_name)

        result = process_image(src, dst)

        with lock:
            state.pages_done += 1
            STATE.done_pages += 1
            state.status = "processing"

        if result != "cached":
            all_cached = False

        render_ui()

    with lock:
        if all_cached:
            state.status = "cached"
        else:
            state.status = "done"

        render_ui()


# ================= VOLUME PLANNER =================
def plan_volumes(chapters):
    current_vol = VolumeState(1)
    vol_num = 1

    for ch_num, folder in chapters:
        size = estimate_chapter_size(folder)

        if current_vol.size_estimate + size > MAX_VOLUME_SIZE_MB * 1024 * 1024:
            STATE.volumes[vol_num] = current_vol
            vol_num += 1
            current_vol = VolumeState(vol_num)

        current_vol.chapters.append(ch_num)
        current_vol.size_estimate += size

    STATE.volumes[vol_num] = current_vol


# ================= CBZ BUILDER =================
def build_cbz(volume: VolumeState):
    volume.status = "building"
    render_ui()

    vol_name = f"Vol_{volume.volume_num}_Ch_{min(volume.chapters)}-{max(volume.chapters)}.cbz"
    vol_path = os.path.join(OUTPUT_DIR, vol_name)

    with zipfile.ZipFile(vol_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for ch in volume.chapters:
            for file in sorted(os.listdir(CACHE_DIR)):
                if file.startswith(f"ch{ch}_"):
                    zf.write(
                        os.path.join(CACHE_DIR, file),
                        arcname=f"Ch_{ch}/{file}"
                    )

    volume.status = "done"
    render_ui()


# ================= MAIN =================
def main():
    chapters = get_chapter_dirs()

    # Init state
    for ch_num, folder in chapters:
        images = get_images(folder)
        STATE.chapters[ch_num] = ChapterState(ch_num, len(images))
        STATE.total_pages += len(images)

    # Plan volumes BEFORE processing
    plan_volumes(chapters)

    render_ui()

    # Process chapters in parallel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for ch_num, folder in chapters:
            executor.submit(process_chapter, ch_num, folder)

    # Build CBZs
    for vol in STATE.volumes.values():
        build_cbz(vol)

    print("\nDone.")


if __name__ == "__main__":
    main()
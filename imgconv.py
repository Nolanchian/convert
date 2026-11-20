#!/usr/bin/env python3
import argparse
import io
import os
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageOps

RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
HEIC_EXTS = {".heic", ".heif"}
# Pillow 讀不了這些格式，要交給 macOS 的 sips 解碼
SIPS_DECODE_EXTS = RAW_EXTS | HEIC_EXTS
INPUT_EXTS = SIPS_DECODE_EXTS | {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
DEFAULT_QUALITY = {"jpg": 90, "webp": 82}
PIL_FORMAT = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}
# sips 解 RAW 本身已用滿多核心，同時開多個 sips 反而變慢；改成一個 sips 一次吃一批，批量只影響進度回報頻率
SIPS_BATCH_SIZE = 8


def iter_boxes(data: bytes, start: int, end: int):
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        yield kind, position + header, position + size
        position += size


def find_box(data: bytes, path: list[bytes], start: int, end: int):
    for kind, body_start, body_end in iter_boxes(data, start, end):
        if kind == path[0]:
            return (body_start, body_end) if len(path) == 1 else find_box(data, path[1:], body_start, body_end)
    return None


def read_moov(file) -> bytes:
    while True:
        position = file.tell()
        header = file.read(16)
        if len(header) < 8:
            raise ValueError("不是有效的 CR3（找不到 moov）")
        size, kind = struct.unpack_from(">I4s", header)
        if size == 1:
            size = struct.unpack_from(">Q", header, 8)[0]
        file.seek(position)
        if kind == b"moov":
            return file.read(size)
        file.seek(position + size)


def extract_cr3_preview(src: Path) -> bytes:
    # CR3 是 ISO BMFF 容器，第一條 track 存的是相機產生的全尺寸 JPEG；只讀 moov 與那段資料，不解 RAW
    with src.open("rb") as file:
        moov = read_moov(file)
        stbl = find_box(moov, [b"moov", b"trak", b"mdia", b"minf", b"stbl"], 0, len(moov))
        stsz = find_box(moov, [b"stsz"], *stbl) if stbl else None
        if not stsz:
            raise ValueError(f"{src.name} 找不到內嵌 JPEG")
        size = struct.unpack_from(">I", moov, stsz[0] + 4)[0] or struct.unpack_from(">I", moov, stsz[0] + 12)[0]
        co64 = find_box(moov, [b"co64"], *stbl)
        if co64:
            offset = struct.unpack_from(">Q", moov, co64[0] + 8)[0]
        else:
            stco = find_box(moov, [b"stco"], *stbl)
            offset = struct.unpack_from(">I", moov, stco[0] + 8)[0]
        file.seek(offset)
        data = file.read(size)
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{src.name} 的內嵌資料不是 JPEG")
    return strip_jpeg_metadata(data, cr3_orientation(moov))


def cr3_orientation(moov: bytes) -> int:
    # 直拍的方向記在 CMT1（一段 TIFF IFD0）裡，內嵌 JPEG 本身沒有方向資訊
    index = moov.find(b"CMT1")
    if index < 4:
        return 1
    size = struct.unpack_from(">I", moov, index - 4)[0]
    return tiff_orientation(moov[index + 4:index - 4 + size])


def tiff_orientation(tiff: bytes) -> int:
    try:
        byte_order = "<" if tiff[:2] == b"II" else ">"
        ifd_offset = struct.unpack_from(f"{byte_order}I", tiff, 4)[0]
        entry_count = struct.unpack_from(f"{byte_order}H", tiff, ifd_offset)[0]
        for index in range(entry_count):
            tag, _, _, value = struct.unpack_from(f"{byte_order}HHIH", tiff, ifd_offset + 2 + 12 * index)
            if tag == 0x0112:
                return value if 1 <= value <= 8 else 1
    except struct.error:
        pass
    return 1


def orientation_segment(orientation: int) -> bytes:
    # 最小的 EXIF：只有 Orientation 一個欄位，檢視器能轉正，又不帶 GPS 等拍攝資訊
    tiff = (
        b"MM\x00\x2a" + struct.pack(">IH", 8, 1)
        + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
    )
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def strip_jpeg_metadata(data: bytes, orientation: int | None = None) -> bytes:
    # 不重新壓縮，直接拿掉 APP1（EXIF/XMP，含 GPS）；APP2 的 ICC 色彩描述檔保留。
    # 但方向要留下：sips 輸出的 JPG 畫面沒有轉正，只靠 EXIF 的 Orientation 標記
    kept = [data[:2]]
    insert_at = 1
    found_orientation = 1
    position = 2
    while position + 4 <= len(data) and data[position] == 0xFF:
        marker = data[position + 1]
        if marker == 0xDA:
            break
        length = struct.unpack_from(">H", data, position + 2)[0]
        segment = data[position:position + 2 + length]
        if marker == 0xE1:
            if segment[4:10] == b"Exif\x00\x00":
                found_orientation = tiff_orientation(segment[10:])
        else:
            kept.append(segment)
            # JFIF 的 APP0 要維持緊接在 SOI 後面
            if marker == 0xE0 and len(kept) == 2:
                insert_at = 2
        position += 2 + length
    kept.append(data[position:])
    final_orientation = orientation or found_orientation
    if final_orientation != 1:
        kept.insert(insert_at, orientation_segment(final_orientation))
    return b"".join(kept)


def sips_to_jpeg(pairs: list[tuple[Path, Path]], quality: int, max_side: int | None) -> list[tuple[Path, Path, str | None]]:
    out_dir = pairs[0][1].parent
    out_dir.mkdir(parents=True, exist_ok=True)
    command = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    if max_side:
        command += ["-Z", str(max_side)]
    command += [str(src) for src, _ in pairs] + ["--out", str(out_dir)]
    started = time.time()
    result = subprocess.run(command, capture_output=True, text=True)
    outcomes = []
    for src, dst in pairs:
        # --overwrite 時舊檔本來就存在，要用修改時間判斷這次是否真的寫出
        if dst.exists() and dst.stat().st_mtime >= started - 1:
            dst.write_bytes(strip_jpeg_metadata(dst.read_bytes()))
            outcomes.append((src, dst, None))
        else:
            outcomes.append((src, dst, f"sips 轉檔失敗：{result.stderr.strip()}"))
    return outcomes


def decode_raw(src: Path, tmp_dir: str) -> Path:
    # Pillow 讀不了 RAW，交給 macOS ImageIO（sips）解碼成無損 TIFF 再處理
    out = Path(tmp_dir) / f"{src.stem}.tiff"
    result = subprocess.run(
        ["sips", "-s", "format", "tiff", str(src), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    # sips 失敗時不一定回傳非 0，所以也要檢查檔案是否真的產生
    if result.returncode != 0 or not out.exists():
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"sips 無法解碼 {src.name}：{message}")
    return out


def has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def prepare_for_format(img: Image.Image, fmt: str) -> Image.Image:
    if fmt == "jpg":
        if has_alpha(img):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return img if img.mode == "RGB" else img.convert("RGB")
    if img.mode not in ("RGB", "RGBA"):
        return img.convert("RGBA" if has_alpha(img) else "RGB")
    return img


def convert_one(src: Path, dst: Path, fmt: str, quality: int | None, max_side: int | None, fast: bool) -> Path:
    with tempfile.TemporaryDirectory() as tmp_dir:
        transpose = True
        if fast and src.suffix.lower() == ".cr3":
            preview = extract_cr3_preview(src)
            if fmt == "jpg" and not max_side:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(preview)
                return dst
            source = io.BytesIO(preview)
        elif src.suffix.lower() in SIPS_DECODE_EXTS:
            source = decode_raw(src, tmp_dir)
            # sips 輸出 TIFF 時已經把畫面轉正，卻還留著原本的方向標記；再轉一次會多轉 90°
            transpose = False
        else:
            source = src
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened) if transpose else opened.copy()
        # 保留色彩描述檔，否則 Display P3 等廣色域圖會偏色；EXIF 則刻意不帶
        icc_profile = img.info.get("icc_profile")
        if max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        img = prepare_for_format(img, fmt)

        save_options = {"icc_profile": icc_profile} if icc_profile else {}
        if fmt == "jpg":
            save_options.update(quality=quality, optimize=True, progressive=True)
        elif fmt == "webp":
            save_options.update(quality=quality, method=4)

        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, PIL_FORMAT[fmt], **save_options)
    return dst


def iter_sources(path: Path, recursive: bool):
    if path.is_file():
        yield path, path.parent
        return
    candidates = path.rglob("*") if recursive else path.glob("*")
    for candidate in sorted(candidates):
        # 略過 macOS 在外接碟產生的 ._ 檔等隱藏檔
        if candidate.is_file() and not candidate.name.startswith(".") and candidate.suffix.lower() in INPUT_EXTS:
            yield candidate, path


def path_key(path: Path) -> str:
    # APFS 預設不分大小寫：IMG_1.jpg 與 IMG_1.JPG 是同一個檔
    return str(path.resolve()).lower()


def collect_jobs(args) -> tuple[list[tuple[Path, Path]], list[str]]:
    jobs, skipped, seen_destinations = [], [], set()
    sources = []
    for input_path in args.inputs:
        if not input_path.exists():
            skipped.append(f"找不到：{input_path}")
            continue
        sources.extend(iter_sources(input_path, args.recursive))
    source_keys = {path_key(src) for src, _ in sources}

    for src, base in sources:
        relative = src.relative_to(base)
        dst = ((args.output / relative) if args.output else src).with_suffix(f".{args.to}")
        dst_key = path_key(dst)
        if dst_key == path_key(src):
            skipped.append(f"來源與輸出相同，略過：{src}")
        elif dst_key in seen_destinations:
            skipped.append(f"輸出檔名重複，略過：{src} → {dst}")
        elif dst_key in source_keys:
            # RAW+JPEG 同名時，絕不讓 IMG_1.CR3 的輸出蓋掉相機的 IMG_1.JPG，即使有 --overwrite
            skipped.append(f"輸出會蓋掉來源檔，略過（請用 -o 指定其他資料夾）：{src} → {dst}")
        elif dst.exists() and not args.overwrite:
            skipped.append(f"已存在（用 --overwrite 覆蓋）：{dst}")
        else:
            seen_destinations.add(dst_key)
            jobs.append((src, dst))
    return jobs, skipped


def run_jobs(jobs, fmt: str, quality: int | None, max_side: int | None, fast: bool, workers: int | None, on_result):
    def uses_sips_directly(src: Path) -> bool:
        suffix = src.suffix.lower()
        return fmt == "jpg" and suffix in SIPS_DECODE_EXTS and not (fast and suffix == ".cr3")

    sips_groups: dict[Path, list[tuple[Path, Path]]] = {}
    pillow_jobs = []
    for src, dst in jobs:
        if uses_sips_directly(src):
            sips_groups.setdefault(dst.parent, []).append((src, dst))
        else:
            pillow_jobs.append((src, dst))

    for group in sips_groups.values():
        for start in range(0, len(group), SIPS_BATCH_SIZE):
            for outcome in sips_to_jpeg(group[start:start + SIPS_BATCH_SIZE], quality, max_side):
                on_result(*outcome)

    if pillow_jobs:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(convert_one, src, dst, fmt, quality, max_side, fast): (src, dst)
                for src, dst in pillow_jobs
            }
            for future in as_completed(futures):
                src, dst = futures[future]
                try:
                    future.result()
                    on_result(src, dst, None)
                except Exception as error:
                    on_result(src, dst, str(error))


def parse_args():
    parser = argparse.ArgumentParser(description="快速圖片轉檔：RAW/HEIC/JPG/PNG → JPG/PNG/WebP")
    parser.add_argument("inputs", nargs="+", type=Path, help="檔案或資料夾")
    parser.add_argument("--to", required=True, choices=PIL_FORMAT.keys(), help="輸出格式")
    parser.add_argument("-q", "--quality", type=int, help="品質 1-100（預設 JPG 90、WebP 82）")
    parser.add_argument("--max", dest="max_side", type=int, help="長邊上限（px），只縮不放")
    parser.add_argument("-o", "--output", type=Path, help="輸出資料夾（預設放在原檔旁）")
    parser.add_argument("-r", "--recursive", action="store_true", help="包含子資料夾")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count(), help="平行數（預設 CPU 核心數）")
    parser.add_argument("--overwrite", action="store_true", help="覆蓋已存在的輸出檔")
    parser.add_argument("--fast", action="store_true", help="CR3 直接取相機內嵌 JPEG（極快；轉 JPG 且無 --max 時不重壓，-q 無效）")
    return parser.parse_args()


def main():
    args = parse_args()
    quality = args.quality or DEFAULT_QUALITY.get(args.to)
    jobs, skipped = collect_jobs(args)
    for message in skipped:
        print(f"- {message}")
    if not jobs:
        print("沒有需要轉檔的檔案")
        return

    done_count = failures = 0

    def report(src: Path, dst: Path, error: str | None):
        nonlocal done_count, failures
        done_count += 1
        if error:
            failures += 1
            print(f"[{done_count}/{len(jobs)}] ✗ {src}：{error}", file=sys.stderr)
        else:
            print(f"[{done_count}/{len(jobs)}] ✓ {src} → {dst}")

    run_jobs(jobs, args.to, quality, args.max_side, args.fast, args.jobs, report)

    print(f"完成 {len(jobs) - failures} 個，失敗 {failures} 個，略過 {len(skipped)} 個")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

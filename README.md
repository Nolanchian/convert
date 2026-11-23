# Nolan｜快速轉檔器

批次把 CR3、HEIC、JPG、PNG 轉成 JPG、PNG、WebP；ODT 轉 Word（.docx）。

**線上版：<https://nolanchian.github.io/convert/>**

## 兩種用法

| | 線上版（GitHub Pages） | 本機完整版（`server.py`） |
|---|---|---|
| 轉檔在哪裡跑 | 你自己的瀏覽器，檔案不會上傳 | 你自己的 Mac |
| CR3 → JPG | 取相機內嵌的 JPEG（極快） | macOS 完整解 RAW，或選快速模式 |
| HEIC（iPhone 照片） | ✓（瀏覽器不能原生解時，自動載入約 2MB 的 libheif 解碼器） | ✓（macOS 原生解碼） |
| JPG / PNG → WebP / JPG / PNG | ✓ | ✓ |
| ODT → Word（.docx） | 不支援 | ✓（需安裝 LibreOffice） |
| 輸入方式 | 拖曳檔案或資料夾，下載 zip | 拖曳檔案，或直接指定 Mac 上的資料夾 |
| 需要安裝 | 無 | Python 3.11+、Pillow，僅支援 macOS（用到 `sips`） |

## 本機完整版

```bash
pip3 install Pillow
brew install --cask libreoffice   # 只有 ODT 轉 Word 需要
python3 server.py
```

會自動開啟 `http://127.0.0.1:8765/`。伺服器只接受這台電腦的連線，其他人無法用你的電腦轉檔。

## 命令列

```bash
python3 imgconv.py ~/Pictures/trip --to jpg -o ~/Desktop/out
python3 imgconv.py ~/Pictures/trip -r --to webp --max 2048 -q 80 -o ~/Desktop/web
python3 imgconv.py ~/Pictures/trip --to jpg --fast -o ~/Desktop/out
```

| 參數 | 說明 |
|---|---|
| `--to` | 圖片的輸出格式：`jpg`、`png`、`webp`（ODT 一律轉成 `.docx`） |
| `-q` | 品質 1–100（預設 JPG 90、WebP 82） |
| `--max` | 長邊上限（px），只縮不放 |
| `-o` | 輸出資料夾（預設放在原檔旁） |
| `-r` | 包含子資料夾 |
| `--fast` | CR3 直接取相機內嵌的 JPEG |
| `--overwrite` | 覆蓋已存在的輸出檔 |

## 注意

- 圖片輸出一律移除 EXIF（含 GPS），只保留照片方向，以及色彩描述檔。
- ODT 轉出的 Word 檔會保留文件內的作者等資訊。
- RAW+JPEG 同名時，不會讓 CR3 的輸出蓋掉相機的 JPG。

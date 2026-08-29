"""文件解析：统一输出 [{index, text, page}] 分块列表。

文本型格式直读；图片与扫描版 PDF 走 OCR（tesseract，chi_sim+eng）。
"""
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def _chunk(text: str, page: int, index_ref: list, out: list) -> None:
    size, overlap = settings.chunk_size, settings.chunk_overlap
    text = text.strip()
    if not text:
        return
    start = 0
    while start < len(text):
        piece = text[start:start + size]
        out.append({"index": index_ref[0], "text": piece, "page": page})
        index_ref[0] += 1
        if start + size >= len(text):
            break
        start += size - overlap


def _ocr_image_bytes(img_bytes: bytes, dpi: int | None = None, retry: bool = True) -> str:
    """对图片字节做 OCR；预处理（灰度/放大）+ 可配 PSM，低置信自动升 DPI 重试。

    tesseract 未安装时抛出带指引的错误。
    """
    try:
        import pytesseract
        from PIL import Image
        import io

        lang = settings.ocr_lang
        psm = settings.ocr_psm
        text = ""
        for attempt, cur_dpi in enumerate([dpi or settings.ocr_dpi, settings.ocr_retry_dpi]):
            img = Image.open(io.BytesIO(img_bytes))
            if img.mode != "L":
                img = img.convert("L")
            # 小图放大 2 倍，提升小字识别率
            w, h = img.size
            if min(w, h) < 600:
                img = img.resize((w * 2, h * 2), Image.LANCZOS)
            text = pytesseract.image_to_string(
                img, lang=lang, config=f"--psm {psm}")
            if not retry or len(text.strip()) >= settings.ocr_min_chars:
                break
        return text
    except pytesseract.TesseractNotFoundError:
        raise ValueError(
            "OCR 环境未安装：容器内请使用本项目镜像（已内置 tesseract + 中文包）；"
            "本地调试请安装 tesseract-ocr 与 chi_sim 语言包")
    except ImportError:
        raise ValueError("OCR 依赖缺失：请安装 pytesseract 与 pillow")


def _parse_pdf(path: Path, max_pages: int | None = None) -> list[dict]:
    """PDF：文本页直读；无文本页（扫描件）自动 OCR 兜底（低置信升 DPI 重试）。

    max_pages：只解析前 N 页（智能匹配预判用，避免整本扫描件全量 OCR）。
    """
    import pymupdf

    chunks, ref = [], [0]
    doc = pymupdf.open(str(path))
    if max_pages is not None:
        doc = doc[:max_pages]  # pymupdf 支持切片，不加载后续页
    for page_no, page in enumerate(doc, start=1):
        text = page.get_text("text") or ""
        if not text.strip():
            logger.info("第 %s 页无文本层，走 OCR", page_no)
            pix = page.get_pixmap(dpi=settings.ocr_dpi)
            text = _ocr_image_bytes(pix.tobytes("png"))
            if len(text.strip()) < settings.ocr_min_chars:
                logger.info("第 %s 页 OCR 结果过短，升 DPI 重试", page_no)
                pix = page.get_pixmap(dpi=settings.ocr_retry_dpi)
                text = _ocr_image_bytes(pix.tobytes("png"), retry=False)
        _chunk(text, page_no, ref, chunks)
    return chunks


def _parse_image(path: Path) -> list[dict]:
    """图片文件整体 OCR。"""
    text = _ocr_image_bytes(path.read_bytes())
    chunks, ref = [], [0]
    _chunk(text, 1, ref, chunks)
    return chunks


def _parse_docx(path: Path) -> list[dict]:
    import docx

    doc = docx.Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    chunks, ref = [], [0]
    _chunk(text, 1, ref, chunks)
    return chunks


def _parse_xlsx(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    chunks, ref = [], [0]
    for sheet in wb.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                rows.append(" | ".join(cells))
        _chunk(f"[工作表: {sheet.title}]\n" + "\n".join(rows), 1, ref, chunks)
    return chunks


def _parse_text(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    chunks, ref = [], [0]
    _chunk(text, 1, ref, chunks)
    return chunks


PARSERS = {
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
    ".xlsx": _parse_xlsx,
    ".txt": _parse_text,
    ".csv": _parse_text,
    ".md": _parse_text,
    **{ext: _parse_image for ext in IMAGE_EXTS},
}


def parse_file(path: str, max_pages: int | None = None) -> list[dict]:
    """按扩展名选择解析器，返回分块列表。不支持的格式抛 ValueError。

    max_pages 仅对 PDF 生效：只解析前 N 页（分类预判场景，避免整本 OCR）。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    parser = PARSERS.get(suffix)
    if parser is None:
        raise ValueError(f"暂不支持的文件格式: {suffix}")
    chunks = parser(p, max_pages) if suffix == ".pdf" and max_pages else parser(p)
    if not chunks:
        raise ValueError("文件解析后为空")
    return chunks

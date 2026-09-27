"""Structural inspection only. Never extract text or rasterize."""
import logging
import warnings
from pathlib import Path
from PIL import Image
from pypdf import PdfReader

MAX_BYTES = 200 * 1024 * 1024
MAX_PAGES = 300
EXTENSIONS = {".pdf": "PDF", ".tif": "TIFF", ".tiff": "TIFF",
              ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
MIMES = {"PDF": "application/pdf", "TIFF": "image/tiff",
         "PNG": "image/png", "JPEG": "image/jpeg"}
# Parser diagnostics can include source strings. Keep document data out of logs.
logging.getLogger("pypdf").disabled = True
logging.getLogger("pypdf").addHandler(logging.NullHandler())
logging.getLogger("pypdf").propagate = False

class InvalidFile(ValueError):
    pass

def inspect(path, extension, max_bytes=MAX_BYTES, max_pages=MAX_PAGES):
    if Path(path).stat().st_size > max_bytes:
        raise InvalidFile("size_limit")
    expected = EXTENSIONS.get(extension.lower())
    if expected is None:
        raise InvalidFile("unsupported_format")
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        pages = []
        if expected == "PDF":
            if not magic.startswith(b"%PDF-"):
                raise InvalidFile("format_mismatch")
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise InvalidFile("encrypted_pdf")
            count = len(reader.pages)
            if not 0 < count <= max_pages:
                raise InvalidFile("page_limit")
            for page in reader.pages:
                pages.append((float(page.mediabox.width), float(page.mediabox.height)))
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format != expected:
                        raise InvalidFile("format_mismatch")
                    count = getattr(image, "n_frames", 1)
                    if expected != "TIFF" and count != 1:
                        raise InvalidFile("animated_image_unsupported")
                    if not 0 < count <= max_pages:
                        raise InvalidFile("page_limit")
                    for number in range(count):
                        image.seek(number)
                        pages.append(image.size)
                    if expected == "PNG":
                        image.verify()
        return MIMES[expected], pages
    except InvalidFile:
        raise
    except Exception:
        raise InvalidFile("invalid_document") from None

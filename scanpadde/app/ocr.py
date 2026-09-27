"""Deterministic page rendering and local native-Tesseract OCR."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from .storage import sha, sync_directory

DPI = 180
ENGINE = "tesseract"
PREPROCESSING_VERSION = "none-v1"

def engine_version():
    output = subprocess.run(["tesseract", "--version"], check=True, capture_output=True,
                            text=True, timeout=10).stdout.splitlines()[0]
    return output.strip()

def embedded_text(source, page_number, extension):
    if extension.lower() != ".pdf":
        return None
    text = (PdfReader(source, strict=True).pages[page_number - 1].extract_text() or "").strip()
    return text or None

def render(paths, source, source_sha, page_number, extension):
    directory = paths.guard("pages/" + source_sha)
    directory.mkdir(exist_ok=True)
    target = paths.guard(directory / f"{page_number:04d}.png")
    if target.exists():
        return target.relative_to(paths.root).as_posix(), sha(target)
    fd, temporary = tempfile.mkstemp(prefix=".render-", suffix=".png", dir=directory)
    os.close(fd)
    Path(temporary).unlink()  # renderer/Pillow create this pathname themselves
    try:
        if extension.lower() == ".pdf":
            prefix = str(Path(temporary).with_suffix(""))
            subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-r", str(DPI),
                            "-png", "-singlefile", str(source), prefix], check=True, capture_output=True,
                           timeout=90)
            produced = Path(prefix + ".png")
        else:
            with Image.open(source) as image:
                image.seek(page_number - 1)
                image.convert("RGB").save(temporary, "PNG", optimize=False)
            produced = Path(temporary)
        if not produced.exists() or produced.stat().st_size == 0:
            raise RuntimeError("render_empty")
        produced.replace(target)
        sync_directory(directory)
        return target.relative_to(paths.root).as_posix(), sha(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
        Path(str(Path(temporary).with_suffix("")) + ".png").unlink(missing_ok=True)

def _tsv(image, language):
    run = subprocess.run(["tesseract", str(image), "stdout", "-l", language, "tsv"], check=True,
                         capture_output=True, text=True, timeout=120)
    lines = run.stdout.splitlines()
    if not lines: raise RuntimeError("ocr_empty_tsv")
    headers = lines[0].split("\t")
    words = []
    for line in lines[1:]:
        cells = line.split("\t", len(headers)-1)
        if len(cells) != len(headers): continue
        row = dict(zip(headers, cells))
        text = row["text"].strip()
        if not text or row["conf"] == "-1": continue
        words.append({"text": text, "bbox": {"x": int(row["left"]), "y": int(row["top"]),
                      "width": int(row["width"]), "height": int(row["height"])}, "confidence": float(row["conf"])})
    confidence = sum(w["confidence"] for w in words) / len(words) if words else None
    return " ".join(w["text"] for w in words), words, confidence

def recognize(image, language="deu+eng"):
    text, words, confidence = _tsv(image, language)
    rotation = 0
    # Expensive alternatives are only tested when the normal OCR is genuinely weak.
    if confidence is not None and confidence < 45:
        with Image.open(image) as base:
            for angle in (90, 180, 270):
                candidate = Path(tempfile.mkstemp(prefix=".rotate-", suffix=".png")[1])
                try:
                    base.rotate(angle, expand=True).save(candidate, "PNG")
                    candidate_text, candidate_words, candidate_confidence = _tsv(candidate, language)
                    if (candidate_confidence or -1) > (confidence or -1):
                        text, words, confidence, rotation = candidate_text, candidate_words, candidate_confidence, angle
                finally:
                    candidate.unlink(missing_ok=True)
    return text, json.dumps(words, ensure_ascii=False, separators=(",", ":")), confidence, rotation

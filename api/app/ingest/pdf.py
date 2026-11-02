"""PDF -> list of (page_number, text). Page numbers are the 1-based PDF page index."""

import logging
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

# pypdf logs noisy font warnings on government PDFs; they don't affect extracted text.
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class ParsedPdf:
    title: str | None
    pages: list[tuple[int, str]]


def clean_text(text: str) -> str:
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # re-join words hyphenated across lines
    return re.sub(r"\s+", " ", text).strip()


def parse_pdf(data: bytes) -> ParsedPdf:
    reader = PdfReader(BytesIO(data))
    pages = [(i, clean_text(p.extract_text() or "")) for i, p in enumerate(reader.pages, start=1)]
    title = (reader.metadata.title or "").strip() if reader.metadata else ""
    return ParsedPdf(title=title or None, pages=pages)

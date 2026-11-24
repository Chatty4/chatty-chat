"""Plain text of a document, for search."""

from collections.abc import Callable, Iterator
from io import BytesIO
from typing import BinaryIO

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pptx.shapes.autoshape import Shape
from pypdf import PdfReader

# shortcut: indexes only the start of big documents (tsvector max is 1 MB), raise if matches deep
# in long files are missed
MAX_CHARS = 200_000

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def document_text(data: bytes, mime: str) -> str | None:
    """Text of a supported document, cut at MAX_CHARS. None if the type isn't supported.
    Raises the library's error if the bytes don't match the type."""
    reader = READERS.get(mime) or (_text if mime.startswith("text/") else None)
    if reader is None:
        return None
    parts, size = [], 0
    for part in reader(BytesIO(data)):  # stops reading pages once there's enough text
        parts.append(part)
        size += len(part) + 1
        if size >= MAX_CHARS:
            break
    return "\n".join(parts)[:MAX_CHARS].replace("\x00", "")  # Postgres text can't hold \x00


def _pdf(file: BinaryIO) -> Iterator[str]:
    for page in PdfReader(file).pages:
        yield page.extract_text()


def _docx(file: BinaryIO) -> Iterator[str]:
    document = Document(file)
    for paragraph in document.paragraphs:
        yield paragraph.text
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield cell.text


def _xlsx(file: BinaryIO) -> Iterator[str]:
    workbook = load_workbook(file, read_only=True, data_only=True)  # values, not formulas
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                yield " ".join(str(value) for value in row if value is not None)
    finally:
        workbook.close()


def _pptx(file: BinaryIO) -> Iterator[str]:
    for slide in Presentation(file).slides:
        for shape in slide.shapes:
            if isinstance(shape, Shape):  # text boxes, titles, placeholders; skips pictures, tables
                yield shape.text_frame.text
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None:  # a notes slide without a text placeholder
                yield notes.text


def _text(file: BinaryIO) -> Iterator[str]:
    yield file.read(MAX_CHARS * 4).decode("utf-8", errors="replace")  # 4 = max bytes per char


READERS: dict[str, Callable[[BinaryIO], Iterator[str]]] = {
    "application/pdf": _pdf,
    DOCX: _docx,
    XLSX: _xlsx,
    PPTX: _pptx,
}
from io import BytesIO

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pypdf.errors import PdfReadError

from app.labeling.documents import DOCX, MAX_CHARS, PPTX, XLSX, document_text

# a one-page PDF with the text "Hello Bucharest"; pypdf rebuilds the missing xref table
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 46>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (Hello Bucharest) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
)


def saved(document) -> bytes:
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_pdf():
    assert document_text(PDF, "application/pdf") == "Hello Bucharest"


def test_docx_paragraphs_and_tables():
    document = Document()
    document.add_paragraph("Quarterly report")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Revenue in Bucharest"
    assert document_text(saved(document), DOCX) == "Quarterly report\nRevenue in Bucharest"


def test_xlsx_skips_empty_cells():
    workbook = Workbook()
    workbook.active.append(["Invoice", 450, None, "RON"])
    assert document_text(saved(workbook), XLSX) == "Invoice 450 RON"


def test_pptx_slides_and_notes():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Roadmap 2027"
    slide.notes_slide.notes_text_frame.text = "speaker notes here"
    assert document_text(saved(presentation), PPTX) == "Roadmap 2027\nspeaker notes here"


def test_text_drops_null_bytes():
    assert document_text("ăîș hello\x00".encode(), "text/plain") == "ăîș hello"


def test_unsupported_type_is_none():
    assert document_text(b"PK", "application/zip") is None


def test_long_text_is_capped():
    assert len(document_text(b"word " * 100_000, "text/plain")) == MAX_CHARS


def test_broken_pdf_raises():
    with pytest.raises(PdfReadError):  # pypdf's error, the worker counts it as a failed attempt
        document_text(b"not a pdf", "application/pdf")

from io import BytesIO

from reportlab.pdfgen import canvas

from starter_agent.cv_workbench.jd_ingestion import JobDocumentError, parse_resume_document


def _text_pdf(text: str) -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(72, 720, text)
    document.save()
    return stream.getvalue()


def test_resume_pdf_uses_local_text_extraction_without_mineru() -> None:
    parsed = parse_resume_document(filename="resume.pdf", content=_text_pdf("Junxiao He"))

    assert parsed.extraction_method == "local_pdf"
    assert "Junxiao He" in parsed.markdown


def test_resume_pdf_falls_back_to_local_text_when_mineru_fails() -> None:
    class FailingOcr:
        def parse(self, *, filename: str, content: bytes) -> str:
            raise JobDocumentError("jd_ocr_status_failed")

    parsed = parse_resume_document(
        filename="resume.pdf", content=_text_pdf("Local fallback"), ocr=FailingOcr()
    )

    assert parsed.extraction_method == "local_pdf"
    assert "Local fallback" in parsed.markdown

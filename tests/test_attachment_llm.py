def test_build_attachment_prompt():
    from src.extract.attachment_prompt import build_attachment_prompt

    prompt = build_attachment_prompt(
        extracted_text="Q3 card transactions reached 1.2M, up 15% YoY.",
        filename="q3_report.pdf",
        mime_type="application/pdf",
        email_subject="Q3 Card Report",
        email_date="2026-01-15",
    )
    assert "Q3 card transactions" in prompt
    assert "q3_report.pdf" in prompt
    assert "Q3 Card Report" in prompt
    assert "JSON" in prompt


def test_prompt_truncation():
    from src.extract.attachment_prompt import build_attachment_prompt

    long_text = "A" * 100_000
    prompt = build_attachment_prompt(
        extracted_text=long_text,
        filename="big.pdf",
        mime_type="application/pdf",
    )
    assert "truncated" in prompt.lower()
    assert len(prompt) < 60_000

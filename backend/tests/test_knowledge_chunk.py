from aep.knowledge.chunk import chunk
from aep.knowledge.extract import Paragraph


def test_headings_and_pdf_pages_are_boundaries():
    paragraphs = [
        Paragraph("甲" * 50, "第一章 > 期限", 1),
        Paragraph("乙" * 50, "第一章 > 期限", 2),
        Paragraph("丙" * 50, "第一章 > 方式", 2),
    ]
    result = chunk(paragraphs, size=200, overlap=20)
    assert [(c.heading, c.page, c.text) for c in result] == [
        (p.heading, p.page, p.text) for p in paragraphs
    ]


def test_exact_overlap_and_hard_cuts_without_punctuation():
    text = "".join(chr(0x4E00 + i) for i in range(530))
    result = chunk([Paragraph(text)], size=200, overlap=30)
    assert [len(c.text) for c in result] == [200, 200, 190]
    assert result[0].text[-30:] == result[1].text[:30]
    assert result[1].text[-30:] == result[2].text[:30]
    assert result[0].text + "".join(c.text[30:] for c in result[1:]) == text


def test_prefers_blank_lines_then_sentences_then_newlines():
    paragraphs = [Paragraph("甲" * 120), Paragraph("乙" * 120 + "。" + "丙" * 130)]
    result = chunk(paragraphs, size=200, overlap=20)
    assert result[0].text == "甲" * 120 + "\n\n"
    assert result[1].text.endswith("。")
    lines = chunk([Paragraph("甲" * 120 + "\n" + "乙" * 120)], size=200, overlap=0)
    assert lines[0].text.endswith("\n")


def test_short_fragments_count_only_non_whitespace_characters():
    assert chunk([Paragraph("字 \n" * 19)], size=200, overlap=0) == []
    result = chunk([Paragraph("甲" * 200 + "乙" * 19)], size=200, overlap=0)
    assert len(result) == 1
    assert result[0].text == "甲" * 200
    assert len(chunk([Paragraph("字 " * 20)])) == 1


def test_short_paragraphs_can_pack_under_the_same_heading():
    result = chunk([Paragraph("甲" * 15, "A"), Paragraph("乙" * 15, "A")])
    assert [c.text for c in result] == ["甲" * 15 + "\n\n" + "乙" * 15]


def test_overlap_never_prevents_progress_with_many_small_sentences():
    result = chunk([Paragraph("短句。" * 200)], size=200, overlap=100)
    assert result
    assert all(20 <= len(c.text) <= 200 for c in result)
    assert all(a.text[-100:] == b.text[:100] for a, b in zip(result, result[1:], strict=False))

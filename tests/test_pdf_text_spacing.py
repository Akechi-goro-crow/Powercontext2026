# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keep words intact when a PDF positions each glyph in its own text object."""

import re
from collections import Counter

import pytest
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document
from tests.pdf_helpers import pdf_bytes, pdf_writer


@pytest.mark.parametrize("line", ["Revenue reached 1234 units in 2025", "Net income (loss) was 12,345.67 in 2024."])
def test_separately_positioned_glyphs_keep_words_and_other_page_content(line):
    writer = pdf_writer("Placeholder", "")
    page = writer.pages[0]
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    fonts = resources["/Font"]
    assert isinstance(fonts, DictionaryObject)
    font = fonts["/F1"]
    assert isinstance(font, DictionaryObject)
    font[NameObject("/BaseFont")] = NameObject("/Courier")
    operators = []
    for index, character in enumerate(line):
        escaped = character.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        # The final TJ adjustment moves no displayed glyph before ET ends the object.
        # Plain extraction incorrectly converts it to a space after every character.
        operators.append(f"BT /F1 10 Tf {50 + index * 6} 750 Td [({escaped}) -50000] TJ ET")
    operators.extend([
        "BT /F1 10 Tf 50 720 Td (Normal line 678.90) Tj ET",
        "BT /F1 10 Tf 0 1 -1 0 550 200 Tm (Vertical label 456) Tj ET",
    ])
    # Widely separated single-character cells are separate tokens, not a word.
    for index, letter in enumerate("ABCDEFGH"):
        operators.append(f"BT /F1 10 Tf {50 + index * 30} 690 Td ({letter}) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(operators).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)

    pages = parse_pdf_document(PDFDocumentInput(document_id="positioned-glyphs", content=pdf_bytes(writer)))

    expected = line + " Normal line 678.90 Vertical label 456 A B C D E F G H"
    assert Counter(re.findall(r"[a-z]+|\d+", pages[0].text.lower())) == Counter(
        re.findall(r"[a-z]+|\d+", expected.lower())
    )
    assert [page.page_number for page in pages] == [1, 2]
    assert pages[1].text == ""

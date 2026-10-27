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

"""Capture one CSV row as a stable Source that can be cited back to the original record."""

from __future__ import annotations

import csv
import io
import json
from typing import NoReturn
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document addressed by a caller-stable document id."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One validated data row, with header order and original field text preserved."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured row whose identity is the document id plus the primary-key value."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate a whole CSV document before returning any row."""

    _require_document_id(value.document_id)
    if value.primary_key == "":
        _invalid("primary_key must be non-empty")
    records = _read_records(value.content)
    if not records or not records[0]:
        _invalid("CSV header is missing")
    columns = tuple(records[0])
    _require_header(columns, value.primary_key)
    key_index = columns.index(value.primary_key)

    rows: list[CSVRowInput] = []
    seen: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(columns):
            _invalid("CSV row width must match the header")
        primary_key_value = record[key_index]
        if primary_key_value.strip() == "":
            _invalid("primary key value must not be blank")
        if primary_key_value in seen:
            _invalid("primary key values must be unique")
        seen.add(primary_key_value)
        csv_row_source_id(value.document_id, primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable row identity, percent-encoding both components."""

    _require_document_id(document_id)
    if primary_key_value.strip() == "":
        _invalid("primary key value must not be blank")
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        _invalid("source id must not exceed 256 characters")
    return source_id


class CSVRowSourceAdapter:
    """Resolve one validated row into a captured Source and read that row back."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        _require_row(value)
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one row as ordered JSON text without changing field types."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


def _read_records(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        _invalid("CSV document must be strict UTF-8")
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        _invalid("malformed CSV")


def _require_document_id(document_id: str) -> None:
    if document_id.strip() == "" or document_id != document_id.strip():
        _invalid("document_id must be non-empty and must not have surrounding whitespace")


def _require_header(columns: tuple[str, ...], primary_key: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.strip() == "":
            _invalid("CSV header field must not be blank")
        if column in seen:
            _invalid("CSV header fields must be unique")
        seen.add(column)
    if primary_key not in seen:
        _invalid("primary_key must match a header field exactly")


def _require_row(value: CSVRowInput) -> None:
    if value.primary_key == "":
        _invalid("primary_key must be non-empty")
    if len(value.columns) != len(value.values):
        _invalid("CSV row width must match the header")
    _require_header(value.columns, value.primary_key)
    key_index = value.columns.index(value.primary_key)
    if value.values[key_index] != value.primary_key_value:
        _invalid("primary key value must match the keyed column")


def _invalid(detail: str) -> NoReturn:
    raise ValueError(detail)


CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSVRowSourceAdapter(),
    projections=(CSVRowTextEvidenceProjection(),),
)

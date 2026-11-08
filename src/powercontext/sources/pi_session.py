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

"""Capture the visible events on one selected Pi session branch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
_SUMMARY_TYPES = frozenset({"compaction", "branch_summary"})


class PiSessionInput(BaseModel):
    """One offline Pi v3 session archive and the branch leaf to read."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event, including the original record that produced its text."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """Captured event whose identity is the session id plus the event id."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the visible events on the selected branch, ordered from root to leaf."""

    session_id, ordered = _read_archive(value.content)
    selected = _select_branch(ordered, value.leaf_id)
    events: list[PiEventInput] = []
    for record in selected:
        event = _visible_event(session_id, record)
        if event is None:
            continue
        _event_source_id(event.session_id, event.event_id)
        events.append(event)
    return tuple(events)


def _event_source_id(session_id: str, event_id: str) -> str:
    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        _invalid("source id must not exceed 256 characters")
    return source_id


class PiEventSourceAdapter:
    """Resolve one visible Pi event and read that same event back."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = _event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose the full event text without applying the Memory entry size limit."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import new visible events on one branch and persist progress only after success."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = path
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, ordered = _read_archive(_read_path(self._path))
        known = _checkpoint_events(session.checkpoint)
        fingerprints = {_event_source_id(session_id, record["id"]): _fingerprint(record) for record in ordered}
        for identity, previous in known.items():
            current = fingerprints.get(identity)
            if current is not None and current != previous:
                _invalid("imported Pi event was rewritten")

        imported = dict(known)
        for record in _select_branch(ordered, self._leaf_id):
            event = _visible_event(session_id, record)
            if event is None:
                continue
            identity = _event_source_id(event.session_id, event.event_id)
            fingerprint = fingerprints[identity]
            if identity in imported:
                continue
            await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            imported[identity] = fingerprint
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"events": imported},
        )


def _read_path(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def _read_archive(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    records = _jsonl_objects(_decode_session(content))
    if not records:
        _invalid("Pi session header is missing")
    header, nodes = records[0], records[1:]
    if header.get("type") != "session" or header.get("version") != 3:
        _invalid("Pi session header must be version 3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or session_id == "":
        _invalid("Pi session id must be a non-empty string")
    return session_id, _ordered_events(nodes)


def _decode_session(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        _invalid("Pi session must be strict UTF-8")


def _jsonl_objects(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.strip() == "":
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            _invalid("Pi session line must be one JSON object")
        if not isinstance(value, dict):
            _invalid("Pi session line must be one JSON object")
        records.append(value)
    return records


def _ordered_events(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in nodes:
        event_id = record.get("id")
        if not isinstance(event_id, str) or event_id == "" or event_id in seen:
            _invalid("Pi event id must be unique")
        if "parentId" not in record:
            _invalid("Pi event parent is missing")
        parent_id = record["parentId"]
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in seen):
            _invalid("Pi event parent must already exist")
        seen.add(event_id)
        ordered.append(record)
    return ordered


def _select_branch(ordered: list[dict[str, Any]], leaf_id: str | None) -> list[dict[str, Any]]:
    if not ordered:
        _invalid("Pi session has no events")
    by_id = {record["id"]: record for record in ordered}
    selected = ordered[-1]["id"] if leaf_id is None else leaf_id
    if selected not in by_id:
        _invalid("Pi session leaf does not exist")
    chain: list[dict[str, Any]] = []
    cursor: str | None = selected
    while cursor is not None:
        record = by_id[cursor]
        chain.append(record)
        cursor = record["parentId"]
    chain.reverse()
    return chain


def _visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str) or event_type == "":
        _invalid("Pi event type is missing")
    parent_id = record["parentId"]
    event_id = record["id"]
    if event_type == "message":
        rendered = _message_body(record.get("message"))
    elif event_type in _SUMMARY_TYPES:
        rendered = _summary_body(event_type, record.get("summary"))
    elif event_type == "custom_message":
        rendered = _custom_body(record.get("content"))
    else:
        return None
    if rendered is None:
        return None
    role, body = rendered
    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(message: object) -> tuple[str, str] | None:
    if not isinstance(message, dict):
        _invalid("Pi message event must contain a message object")
    role = message.get("role")
    if not isinstance(role, str) or role == "":
        _invalid("Pi message role is missing")
    if role == "bashExecution":
        command = message.get("command", "")
        output = message.get("output", "")
        if not isinstance(command, str) or not isinstance(output, str):
            _invalid("bash execution must contain text command and output")
        return role, f"$ {command}\n{output}"
    if role == "toolResult":
        tool_name = message.get("toolName", "")
        tool_call_id = message.get("toolCallId", "")
        if not isinstance(tool_name, str) or not isinstance(tool_call_id, str):
            _invalid("tool result must identify its tool call")
        return role, f"[tool_result {tool_name} {tool_call_id}] {_text_body(message.get('content'))}"
    body = _content_body(message.get("content"))
    if body is None:
        return None
    return role, body


def _summary_body(event_type: str, summary: object) -> tuple[str, str] | None:
    if summary is None:
        return None
    if not isinstance(summary, str):
        _invalid("Pi summary must be text")
    if summary == "":
        return None
    return event_type, summary


def _custom_body(content: object) -> tuple[str, str] | None:
    pieces = _text_pieces(content)
    if not pieces:
        return None
    return "custom", "\n".join(pieces)


def _content_body(content: object) -> str | None:
    if isinstance(content, str):
        return content if content else None
    if content is None:
        return None
    if not isinstance(content, list):
        _invalid("Pi message content must be text or blocks")
    pieces: list[str] = []
    for block in content:
        piece = _message_piece(block)
        if piece:
            pieces.append(piece)
    if not pieces:
        return None
    return "\n".join(pieces)


def _message_piece(block: object) -> str | None:
    if isinstance(block, str):
        return block or None
    if not isinstance(block, dict):
        _invalid("Pi message block must be an object")
    block_type = block.get("type")
    if block_type in {None, "thinking", "image"}:
        return None
    if block_type == "text":
        text = block.get("text", "")
        if not isinstance(text, str):
            _invalid("Pi text block must contain text")
        return text or None
    if block_type == "toolCall":
        name = block.get("name", "")
        call_id = block.get("id", "")
        if not isinstance(name, str) or not isinstance(call_id, str):
            _invalid("tool call must contain a name and id")
        return f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments', {}))}"
    return None


def _text_body(content: object) -> str:
    return "\n".join(_text_pieces(content))


def _text_pieces(content: object) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        _invalid("Pi text content must be a string or blocks")
    pieces: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                pieces.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if not isinstance(text, str):
            _invalid("Pi text block must contain text")
        if text:
            pieces.append(text)
    return pieces


def _compact_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        _invalid("tool arguments must be JSON")


def _fingerprint(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _checkpoint_events(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        _invalid("Pi checkpoint must be an object")
    events = checkpoint.get("events", {})
    if not isinstance(events, dict):
        _invalid("Pi checkpoint events must be an object")
    known: dict[str, str] = {}
    for identity, fingerprint in events.items():
        if not isinstance(identity, str) or not isinstance(fingerprint, str):
            _invalid("Pi checkpoint event must map an identity to a fingerprint")
        known[identity] = fingerprint
    return known


def _invalid(detail: str) -> NoReturn:
    raise ValueError(detail)


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PiEventSourceAdapter(),
    projections=(PiEventTextEvidenceProjection(),),
)

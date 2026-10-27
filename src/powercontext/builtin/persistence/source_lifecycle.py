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

"""Scope-local lifecycle registrations and searchable Topic availability."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE as NODES


class SourceLifecycleRepository:
    """Persist graph nodes and accepted events in caller-owned transactions."""

    async def load(self, connection: AsyncConnection, scope_id: str) -> dict[str, bytes]:
        rows = await connection.execute(
            select(NODES.c.node_id, NODES.c.payload).where(NODES.c.scope_id == scope_id).order_by(NODES.c.node_id)
        )
        return {str(row.node_id): bytes(row.payload) for row in rows}

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        *,
        exists: bool,
        kind: str,
        available: bool,
        artifact_id: str | None,
        reference_revision: int | None,
        payload: bytes,
    ) -> None:
        values = {
            "kind": kind,
            "available": available,
            "artifact_id": artifact_id,
            "reference_revision": reference_revision,
            "payload": payload,
        }
        if exists:
            statement = update(NODES).where(NODES.c.scope_id == scope_id, NODES.c.node_id == node_id).values(**values)
        else:
            statement = insert(NODES).values(scope_id=scope_id, node_id=node_id, **values)
        await connection.execute(statement)


def topic_is_available(
    scope_id: str, artifact_id: ColumnElement[str], revision: ColumnElement[int]
) -> ColumnElement[bool]:
    """Unregistered topics and topics with at least one supporting binding remain eligible."""
    blocked = (
        select(NODES.c.artifact_id)
        .where(
            NODES.c.scope_id == scope_id,
            NODES.c.kind == "topic",
            NODES.c.artifact_id == artifact_id,
            NODES.c.reference_revision == revision,
        )
        .group_by(NODES.c.artifact_id, NODES.c.reference_revision)
        .having(func.max(NODES.c.available) == 0)
        .exists()
    )
    return ~blocked


async def unavailable_topics(connection: AsyncConnection, scope_id: str) -> set[tuple[str, int]]:
    rows = await connection.execute(
        select(NODES.c.artifact_id, NODES.c.reference_revision)
        .where(NODES.c.scope_id == scope_id, NODES.c.kind == "topic")
        .group_by(NODES.c.artifact_id, NODES.c.reference_revision)
        .having(func.max(NODES.c.available) == 0)
    )
    return {(str(row.artifact_id), int(row.reference_revision)) for row in rows}

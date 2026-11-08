"""Research notes assistant using seekdb search and PowerContext memory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DB_PATH = Path(os.getenv("SEEKDB_PATH", str(ROOT / "data" / "notes.db")))
SEEKDB_HOST = os.getenv("SEEKDB_HOST", "")
SEEKDB_PORT = int(os.getenv("SEEKDB_PORT", "2881"))
SEEKDB_TENANT = os.getenv("SEEKDB_TENANT", "sys")
SEEKDB_USER = os.getenv("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")
POWERCONTEXT_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_SCOPE_ID = os.getenv("POWERCONTEXT_SCOPE_ID", "")
POWERCONTEXT_TOKEN = os.getenv("POWERCONTEXT_TOKEN", "")


class NoteInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class MemoryInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4_000)


def get_collection():
    try:
        import pyseekdb
    except ImportError as error:
        raise RuntimeError("install pyseekdb before starting the Qoder assistant") from error
    if SEEKDB_HOST:
        client = pyseekdb.RemoteServerClient(
            host=SEEKDB_HOST,
            port=SEEKDB_PORT,
            tenant=SEEKDB_TENANT,
            database="qoder_notes",
            user=SEEKDB_USER,
            password=SEEKDB_PASSWORD,
        )
    else:
        client = pyseekdb.Client(path=str(DB_PATH), database="qoder_notes")
    collection = client.get_or_create_collection("research_notes")
    return collection


def powercontext_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if POWERCONTEXT_TOKEN:
        headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    return headers


def require_scope() -> str:
    if not POWERCONTEXT_SCOPE_ID:
        raise HTTPException(status_code=503, detail="POWERCONTEXT_SCOPE_ID is not configured")
    return POWERCONTEXT_SCOPE_ID


app = FastAPI(title="Qoder Research Notes Assistant")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "seekdb_mode": "remote" if SEEKDB_HOST else "embedded",
        "powercontext_configured": bool(POWERCONTEXT_SCOPE_ID),
    }


@app.post("/api/ingest")
def ingest(note: NoteInput) -> dict[str, Any]:
    note_id = f"note-{abs(hash((note.title, note.source))) % 10**12}"
    document = f"标题：{note.title}\n标签：{', '.join(note.tags)}\n来源：{note.source}\n\n{note.content}"
    try:
        collection = get_collection()
        collection.upsert(
            ids=[note_id],
            documents=[document],
            metadatas=[{"title": note.title, "tags": note.tags, "source": note.source}],
        )
        collection.refresh_index()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"seekdb ingest failed: {error}") from error
    return {"id": note_id, "title": note.title, "message": "资料已加入研究资料库"}


@app.post("/api/search")
def search(request: SearchInput) -> dict[str, Any]:
    try:
        result = get_collection().query(query_texts=[request.query], n_results=request.limit)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"seekdb search failed: {error}") from error
    ids = result.get("ids", [[]])[0]
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    return {
        "query": request.query,
        "results": [
            {"id": item_id, "content": content, "metadata": metadata}
            for item_id, content, metadata in zip(ids, documents, metadatas, strict=False)
        ],
    }


@app.post("/api/memory/save")
def save_memory(request: MemoryInput) -> dict[str, Any]:
    scope_id = require_scope()
    response = requests.post(
        f"{POWERCONTEXT_URL}/v1/memory/remember",
        headers=powercontext_headers(),
        json={
            "scope_id": scope_id,
            "kind": "preference",
            "text": f"user_id={request.user_id}\n{request.text}",
            "reason": "Qoder research notes assistant user context",
        },
        timeout=20,
    )
    response.raise_for_status()
    return {"saved": True, "result": response.json()}


@app.post("/api/memory/load")
def load_memory(request: MemoryInput) -> dict[str, Any]:
    scope_id = require_scope()
    response = requests.post(
        f"{POWERCONTEXT_URL}/v1/memory/search",
        headers=powercontext_headers(),
        json={"scope_id": scope_id, "query": request.user_id, "limit": 5},
        timeout=20,
    )
    response.raise_for_status()
    return {"user_id": request.user_id, "memory": response.json()}

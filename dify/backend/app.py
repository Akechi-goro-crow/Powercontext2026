"""HTTP bridge used by the Dify Chatflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("SEEKDB_PATH", str(ROOT / "data" / "fun.db")))
POWERCONTEXT_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_SCOPE_ID = os.getenv("POWERCONTEXT_SCOPE_ID", "")


class QueryRequest(BaseModel):
    query: str


class MemoryRequest(BaseModel):
    session_id: str
    text: str = ""


def get_collection():
    try:
        import pyseekdb
    except ImportError as error:
        raise RuntimeError("pyseekdb is required for the Dify bridge") from error
    client = pyseekdb.Client(path=str(DB_PATH), database="dify_fun")
    collection = client.get_or_create_collection("debate_lore")
    collection.refresh_index()
    return collection


app = FastAPI(title="Dify Fun Bridge")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "seekdb_path": str(DB_PATH), "powercontext_scope_configured": bool(POWERCONTEXT_SCOPE_ID)}


@app.post("/retrieve")
def retrieve(request: QueryRequest) -> dict[str, Any]:
    try:
        result = get_collection().query(query_texts=[request.query], n_results=5)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"seekdb retrieval failed: {error}") from error
    ids = result.get("ids", [[]])[0]
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    items = [
        {"id": item_id, "text": text, "metadata": metadata}
        for item_id, text, metadata in zip(ids, documents, metadatas, strict=False)
    ]
    return {"items": items, "context": "\n".join(f"[{item['id']}] {item['text']}" for item in items)}


@app.post("/memory/load")
def load_memory(request: MemoryRequest) -> dict[str, Any]:
    if not POWERCONTEXT_SCOPE_ID:
        return {"state": "No saved player state is configured yet."}
    response = requests.post(
        f"{POWERCONTEXT_URL}/v1/memory/search",
        json={"scope_id": POWERCONTEXT_SCOPE_ID, "query": request.session_id, "limit": 3},
        timeout=20,
    )
    response.raise_for_status()
    return {"state": response.json()}


@app.post("/memory/save")
def save_memory(request: MemoryRequest) -> dict[str, Any]:
    if not POWERCONTEXT_SCOPE_ID:
        return {"saved": False, "reason": "POWERCONTEXT_SCOPE_ID is not configured"}
    response = requests.post(
        f"{POWERCONTEXT_URL}/v1/memory/remember",
        json={
            "scope_id": POWERCONTEXT_SCOPE_ID,
            "kind": "preference",
            "text": f"player_session={request.session_id}\n{request.text}",
            "reason": "Dify fun debate player state",
        },
        timeout=20,
    )
    response.raise_for_status()
    return {"saved": True, "result": response.json()}

"""SDD context module backed by seekdb and PowerContext."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "context.db"
DEFAULT_MANIFEST = ROOT / "data" / "manifest.json"


def _seekdb_collection(db_path: Path):
    try:
        import pyseekdb
    except ImportError as error:
        raise RuntimeError("install pyseekdb before running the seekdb-backed context module") from error
    client = pyseekdb.Client(path=str(db_path), database="engineering_context")
    collection = client.get_or_create_collection("artifacts")
    return client, collection


def ingest_seekdb(db_path: Path) -> dict[str, Any]:
    """Index the active specification, implementation, and tests in seekdb."""

    files = [
        ROOT / "specs" / "shipping-spec.md",
        ROOT / "sample_project" / "shipping.py",
        ROOT / "sample_project" / "test_shipping.py",
    ]
    client, collection = _seekdb_collection(db_path)
    ids = [path.relative_to(ROOT).as_posix() for path in files]
    documents = [path.read_text(encoding="utf-8") for path in files]
    metadatas = [{"path": item} for item in ids]
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    collection.refresh_index()
    manifest = {"db_path": str(db_path), "documents": ids}
    DEFAULT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def query_seekdb(db_path: Path, query: str, limit: int = 5) -> list[dict[str, Any]]:
    _, collection = _seekdb_collection(db_path)
    result = collection.query(query_texts=[query], n_results=limit)
    ids = result.get("ids", [[]])[0]
    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]
    return [
        {"path": path, "text": text, "metadata": metadata}
        for path, text, metadata in zip(ids, documents, metadatas, strict=False)
    ]


class PowerContextState:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.scope_id = os.getenv("POWERCONTEXT_SCOPE_ID")

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.request(method, self.base_url + path, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def ensure_scope(self) -> str:
        if self.scope_id:
            return self.scope_id
        payload = self._request(
            "POST",
            "/v1/scopes",
            {
                "title": "MiniMax SDD shipping task",
                "summary": "Reusable state for the shipping eligibility modification",
                "idempotency_key": "minimax-sdd-shipping-task",
            },
        )
        self.scope_id = payload["scope_id"]
        return self.scope_id

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        scope_id = self.ensure_scope()
        return self._request(
            "POST",
            "/v1/memory/remember",
            {
                "scope_id": scope_id,
                "kind": "decision",
                "text": json.dumps(state, ensure_ascii=False, sort_keys=True),
                "reason": "MiniMax SDD validation state",
            },
        )

    def search_state(self, query: str) -> dict[str, Any]:
        scope_id = self.ensure_scope()
        return self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": scope_id, "query": query, "limit": 5},
        )


def prepare_context(db_path: Path, task: str) -> dict[str, Any]:
    results = query_seekdb(db_path, task)
    return {"task": task, "sources": results, "source_paths": [item["path"] for item in results]}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--db", type=Path, default=DEFAULT_DB)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("task")
    prepare.add_argument("--db", type=Path, default=DEFAULT_DB)
    save = sub.add_parser("save-state")
    save.add_argument("state_json", type=Path)
    resume = sub.add_parser("resume")
    resume.add_argument("query", nargs="?", default="MiniMax SDD shipping task")
    args = parser.parse_args()
    if args.command == "ingest":
        print(json.dumps(ingest_seekdb(args.db), ensure_ascii=False, indent=2))
    elif args.command == "prepare":
        print(json.dumps(prepare_context(args.db, args.task), ensure_ascii=False, indent=2))
    elif args.command == "save-state":
        state = json.loads(args.state_json.read_text(encoding="utf-8"))
        print(json.dumps(PowerContextState().save_state(state), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(PowerContextState().search_state(args.query), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

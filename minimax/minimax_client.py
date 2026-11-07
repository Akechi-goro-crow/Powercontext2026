"""Minimal MiniMax ChatCompletion v2 client used by the sample workflow."""

from __future__ import annotations

import json
import os
import argparse
from pathlib import Path
from typing import Any

import requests


MINIMAX_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1/text/chatcompletion_v2")


def generate_patch(context: str, task: str, output_path: Path) -> dict[str, Any]:
    """Ask MiniMax for a proposed patch and persist the complete response."""

    api_key = os.environ["MINIMAX_API_KEY"]
    model = os.getenv("MINIMAX_MODEL", "MiniMax-Text-01")
    prompt = (
        "You are modifying a small Python checkout module. Return a unified diff only.\n"
        "Preserve the public function signature and existing compatible behavior.\n\n"
        f"TASK:\n{task}\n\nCONTEXT:\n{context}\n"
    )
    response = requests.post(
        MINIMAX_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a careful software engineer."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-file", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", type=Path, default=Path("minimax/artifacts/minimax/response.json"))
    args = parser.parse_args()
    context = args.context_file.read_text(encoding="utf-8")
    payload = generate_patch(context, args.task, args.output)
    print(json.dumps({"model": payload.get("model"), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

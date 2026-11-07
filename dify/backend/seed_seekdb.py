"""Seed the playful debate lore collection used by the Dify bridge."""

from __future__ import annotations

import os
from pathlib import Path

import pyseekdb


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("SEEKDB_PATH", str(ROOT / "data" / "fun.db")))
SEEKDB_HOST = os.getenv("SEEKDB_HOST", "")
SEEKDB_PORT = int(os.getenv("SEEKDB_PORT", "2881"))
SEEKDB_TENANT = os.getenv("SEEKDB_TENANT", "sys")
SEEKDB_USER = os.getenv("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")


LORE = [
    ("rule-001", "离谱辩论规则：主持人必须先复述双方立场，再给出一个意外但自洽的裁判理由。", "rule"),
    ("rule-002", "离谱辩论规则：不得把现实中的个人隐私、医疗、金融或违法建议当作玩笑素材。", "safety"),
    ("lore-001", "月球快递员宣称月尘是最环保的包装材料，因为它本来就在那里。", "space"),
    ("lore-002", "会唱歌的火锅认为沸腾是一种公开演讲，鸳鸯锅是它的双语能力。", "food"),
    ("lore-003", "图书馆的自动门坚持自己是知识守门人，每次开门都要收取一个冷知识。", "city"),
]


def main() -> None:
    if SEEKDB_HOST:
        client = pyseekdb.RemoteServerClient(
            host=SEEKDB_HOST,
            port=SEEKDB_PORT,
            tenant=SEEKDB_TENANT,
            database="dify_fun",
            user=SEEKDB_USER,
            password=SEEKDB_PASSWORD,
        )
    else:
        client = pyseekdb.Client(path=str(DB_PATH), database="dify_fun")
    collection = client.get_or_create_collection("debate_lore")
    collection.add(
        ids=[item[0] for item in LORE],
        documents=[item[1] for item in LORE],
        metadatas=[{"category": item[2]} for item in LORE],
    )
    collection.refresh_index()
    print(f"seeded {len(LORE)} lore records into {DB_PATH}")


if __name__ == "__main__":
    main()

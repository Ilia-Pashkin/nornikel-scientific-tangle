"""Эмбеддинги чанков через baai/bge-m3 на routerai.ru → embeddings.jsonl."""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING)

from tqdm import tqdm

from sciknot.config import settings
from sciknot.llm import embed

BATCH = 32
MAX_CHARS = 6000  # bge-m3 держит 8k токенов, обрезаем с запасом


def main(limit: int | None = None):
    chunks_path = settings.processed_dir / "chunks.jsonl"
    out_path = settings.processed_dir / "embeddings.jsonl"

    chunks = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
    done: set[str] = set()
    if out_path.exists():
        for line in open(out_path, encoding="utf-8"):
            try:
                done.add(json.loads(line)["chunk_id"])
            except Exception:
                pass
    todo = [c for c in chunks if c["chunk_id"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"чанков всего {len(chunks)}, готово {len(done)}, в работу {len(todo)}")

    with open(out_path, "a", encoding="utf-8") as f:
        for i in tqdm(range(0, len(todo), BATCH), desc="embed", unit="batch"):
            batch = todo[i : i + BATCH]
            vecs = embed([c["text"][:MAX_CHARS] for c in batch])
            for c, v in zip(batch, vecs):
                f.write(json.dumps({"chunk_id": c["chunk_id"], "vector": v}) + "\n")
            f.flush()
    print(f"OK -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    main(limit=args.limit)

"""Дозагрузка отдельных файлов в chunks.jsonl (инкрементально, без пересборки)."""

import argparse
import json
import sys
from dataclasses import asdict

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from sciknot.config import settings
from sciknot.ingest.pipeline import ingest_file


def main(rel_paths: list[str]):
    chunks_path = settings.processed_dir / "chunks.jsonl"
    known_docs = set()
    if chunks_path.exists():
        for line in open(chunks_path, encoding="utf-8"):
            known_docs.add(json.loads(line)["doc_id"])

    added = 0
    with open(chunks_path, "a", encoding="utf-8") as f:
        for rel in rel_paths:
            path = settings.data_dir / rel
            if not path.exists():
                print(f"нет файла: {rel}")
                continue
            chunks = ingest_file(path)
            if not chunks:
                print(f"пусто: {rel}")
                continue
            if chunks[0].doc_id in known_docs:
                print(f"уже в базе: {rel}")
                continue
            for c in chunks:
                f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
            added += len(chunks)
            print(f"+{len(chunks)} чанков: {rel}")
    print(f"OK: добавлено {added} чанков")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-scan", type=int, default=None, help="взять топ-N из keyword_scan.json")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()
    paths = list(args.paths)
    if args.from_scan:
        hits = json.load(open(settings.processed_dir / "keyword_scan.json", encoding="utf-8"))
        paths.extend(h["path"] for h in hits[: args.from_scan])
    main(paths)

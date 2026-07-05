"""Быстрый скан непроиндексированных PDF на ключевые слова — чтобы доиндексировать точечно."""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import fitz
from tqdm import tqdm

from sciknot.config import settings


def scan(categories: list[str], keywords: list[str], out: Path):
    files = []
    for cat in categories:
        files.extend(p for p in (settings.data_dir / cat).rglob("*.pdf"))
    print(f"файлов: {len(files)}")
    hits = []
    for path in tqdm(files, unit="file"):
        try:
            with fitz.open(path) as doc:
                score = 0
                for page in doc:
                    text = page.get_text("text").lower()
                    score += sum(text.count(kw) for kw in keywords)
                if score > 0:
                    hits.append({"path": str(path.relative_to(settings.data_dir)), "hits": score})
        except Exception:
            continue
    hits.sort(key=lambda x: -x["hits"])
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")
    for h in hits[:15]:
        print(f"{h['hits']:4d}  {h['path']}")
    print(f"итого документов с упоминаниями: {len(hits)} -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="Журналы,Материалы конференций")
    ap.add_argument("--keywords", default="закачк")
    args = ap.parse_args()
    scan(
        [c.strip() for c in args.categories.split(",")],
        [k.strip().lower() for k in args.keywords.split(",")],
        settings.processed_dir / "keyword_scan.json",
    )

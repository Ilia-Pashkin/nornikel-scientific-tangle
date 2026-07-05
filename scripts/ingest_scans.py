"""Vision-ингест сканов: страницы PDF без текстового слоя → рендер → VLM (mmproj) →
текстовое описание → chunks.jsonl (дальше стандартные run_embed/run_load/run_extract)."""

import argparse
import hashlib
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import fitz

from sciknot import llm
from sciknot.config import settings

VISION_PROMPT = (
    "Это страница технического документа по горно-металлургической тематике. "
    "1) Перепиши весь читаемый текст со страницы. "
    "2) Опиши все рисунки, графики и таблицы: что изображено, оси, кривые, числовые значения. "
    "Отвечай на русском, без вступлений."
)


def find_scan_pages() -> list[tuple[str, int]]:
    docs = sorted({json.loads(l)["source_path"]
                   for l in open(settings.processed_dir / "chunks.jsonl", encoding="utf-8")})
    scans = []
    for rel in docs:
        p = settings.data_dir / rel
        if p.suffix.lower() != ".pdf":
            continue
        try:
            with fitz.open(p) as d:
                for i, page in enumerate(d):
                    infos = page.get_image_info()
                    big = [x for x in infos if x["width"] >= 300 and x["height"] >= 300]
                    if big and not page.get_text("text").strip():
                        scans.append((rel, i))
        except Exception:
            continue
    return scans


def main(vision_base: str, vision_model: str):
    llm.configure(llm_base=vision_base, llm_key="local",
                  answer_model=vision_model, extract_model=vision_model)

    chunks_path = settings.processed_dir / "chunks.jsonl"
    existing = {json.loads(l)["chunk_id"] for l in open(chunks_path, encoding="utf-8")}

    scans = find_scan_pages()
    print(f"страниц-сканов: {len(scans)}")

    added = 0
    with open(chunks_path, "a", encoding="utf-8") as out:
        for rel, page_no in scans:
            doc_id = hashlib.md5(rel.encode("utf-8")).hexdigest()[:12]
            chunk_id = f"{doc_id}:scan{page_no}"
            if chunk_id in existing:
                print(f"уже есть: {rel} стр.{page_no + 1}")
                continue
            with fitz.open(settings.data_dir / rel) as d:
                pix = d[page_no].get_pixmap(dpi=140)
                png = pix.tobytes("png")
            try:
                text = llm.chat_vision(
                    "Ты — OCR и аналитик технической документации.",
                    VISION_PROMPT, png, mime="image/png", max_tokens=1800,
                )
            except Exception as e:
                print(f"ОШИБКА {rel} стр.{page_no + 1}: {e}")
                continue
            parts = rel.split("\\")
            category = parts[0]
            journal = parts[1] if category == "Журналы" and len(parts) > 2 else None
            import re
            m = re.search(r"(20[0-2]\d)", rel)
            out.write(json.dumps({
                "doc_id": doc_id, "chunk_id": chunk_id,
                "text": f"[Скан, распознан VLM] {text}",
                "source_path": rel, "category": category, "journal": journal,
                "year": int(m.group(1)) if m else None,
                "location": f"стр. {page_no + 1} (скан)",
            }, ensure_ascii=False) + "\n")
            added += 1
            print(f"+ {rel} стр.{page_no + 1}: {len(text)} симв.")
    print(f"OK: добавлено {added} чанков со сканов")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vision-base", default="http://127.0.0.1:8089/v1")
    ap.add_argument("--vision-model", default="qwen3.6-35b-a3b-vision")
    args = ap.parse_args()
    main(args.vision_base, args.vision_model)

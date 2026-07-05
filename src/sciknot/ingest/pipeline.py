"""Обход корпуса → парсинг → чанкинг → chunks.jsonl."""

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from tqdm import tqdm

from sciknot.config import settings
from sciknot.ingest.parsers import Unit, parse_file

log = logging.getLogger(__name__)

CHUNK_CHARS = 4000  # ~1000-1400 токенов для русского текста
CHUNK_OVERLAP = 400
MIN_CHUNK_CHARS = 200


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    source_path: str  # относительный путь для цитирования
    category: str  # Обзоры / Статьи / Доклады / Журналы / Материалы конференций
    journal: str | None
    year: int | None
    location: str  # "стр. 3 – стр. 5"


def _doc_meta(path: Path) -> tuple[str, str | None, int | None]:
    rel = path.relative_to(settings.data_dir)
    parts = rel.parts
    category = parts[0]
    journal = parts[1] if category == "Журналы" and len(parts) > 2 else None
    year = None
    m = re.search(r"(20[0-2]\d)", str(rel))
    if m:
        year = int(m.group(1))
    return category, journal, year


def _split_long(text: str) -> list[str]:
    """Режет длинный текст на куски CHUNK_CHARS с оверлапом, стараясь по границе абзаца."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    pieces = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            # ищем ближайший перенос строки/точку назад, чтобы не рвать предложение
            cut = max(text.rfind("\n", start + CHUNK_CHARS // 2, end),
                      text.rfind(". ", start + CHUNK_CHARS // 2, end))
            if cut > start:
                end = cut + 1
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return pieces


def chunk_units(units: list[Unit]) -> list[tuple[str, str]]:
    """Пакует юниты (страницы/слайды) в чанки ~CHUNK_CHARS. Возвращает (location, text)."""
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    buf_labels: list[str] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_labels, buf_len
        if buf and buf_len >= MIN_CHUNK_CHARS:
            loc = buf_labels[0] if buf_labels[0] == buf_labels[-1] else f"{buf_labels[0]} – {buf_labels[-1]}"
            chunks.append((loc, "\n\n".join(buf)))
        buf, buf_labels, buf_len = [], [], 0

    for label, text in units:
        for piece in _split_long(text):
            if buf_len + len(piece) > CHUNK_CHARS and buf:
                flush()
            buf.append(piece)
            buf_labels.append(label)
            buf_len += len(piece)
    flush()
    return chunks


def ingest_file(path: Path) -> list[Chunk]:
    units = parse_file(path)
    if not units:
        return []
    category, journal, year = _doc_meta(path)
    rel = str(path.relative_to(settings.data_dir))
    doc_id = hashlib.md5(rel.encode("utf-8")).hexdigest()[:12]
    result = []
    for i, (loc, text) in enumerate(chunk_units(units)):
        result.append(Chunk(
            doc_id=doc_id,
            chunk_id=f"{doc_id}:{i}",
            text=text,
            source_path=rel,
            category=category,
            journal=journal,
            year=year,
            location=loc,
        ))
    return result


def run_ingest(categories: list[str], out_path: Path | None = None, limit: int | None = None) -> Path:
    out_path = out_path or settings.processed_dir / "chunks.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for cat in categories:
        cat_dir = settings.data_dir / cat
        if not cat_dir.exists():
            log.warning("Нет папки категории: %s", cat_dir)
            continue
        files.extend(p for p in sorted(cat_dir.rglob("*")) if p.is_file())
    if limit:
        files = files[:limit]

    n_chunks = 0
    n_docs = 0
    n_skipped = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for path in tqdm(files, desc="ingest", unit="file"):
            chunks = ingest_file(path)
            if not chunks:
                n_skipped += 1
                continue
            n_docs += 1
            for c in chunks:
                f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
                n_chunks += 1

    log.info("Готово: %d документов, %d чанков, %d пропущено → %s", n_docs, n_chunks, n_skipped, out_path)
    print(f"OK: {n_docs} документов, {n_chunks} чанков, {n_skipped} пропущено -> {out_path}")
    return out_path

"""Загрузка новых документов через UI: сохранение → инкрементальный пайплайн
(чанкинг → экстракция → эмбеддинги → граф) в фоновом потоке со статусом."""

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from sciknot.config import settings
from sciknot.extract.extractor import extract_chunk
from sciknot.graph.loader import run_load
from sciknot.ingest.pipeline import Chunk, ingest_file
from sciknot.llm import chat_vision, embed

VISION_PROMPT = (
    "Это страница технического документа по горно-металлургической тематике. "
    "1) Перепиши весь читаемый текст со страницы. "
    "2) Опиши все рисунки, графики и таблицы: что изображено, оси, кривые, числовые значения. "
    "Отвечай на русском, без вступлений."
)

IMG_PROMPT = (
    "Это иллюстрация из технического документа по горно-металлургической тематике. "
    "Опиши предметно: что изображено; для графиков — оси, кривые, числовые значения; "
    "для таблиц — их содержимое; для схем — элементы и связи. Перепиши подписи и текст. "
    "Отвечай на русском, без вступлений."
)

UPLOAD_CATEGORY = "Загруженные"
ALLOWED_EXT = {".pdf", ".docx", ".docm", ".pptx", ".xlsx", ".xls"}
EMBED_BATCH = 32
HASHES_PATH = settings.processed_dir / "content_hashes.json"

STATUS: dict = {"running": False, "stage": None, "detail": "", "error": None,
                "files": [], "added_chunks": 0}
_lock = threading.Lock()


def _load_hashes() -> dict:
    """hash содержимого -> имя файла. При первом обращении хэшируем уже загруженное."""
    if HASHES_PATH.exists():
        return json.loads(HASHES_PATH.read_text(encoding="utf-8"))
    hashes = {}
    updir = settings.data_dir / UPLOAD_CATEGORY
    if updir.exists():
        for p in updir.iterdir():
            if p.is_file():
                hashes[hashlib.md5(p.read_bytes()).hexdigest()] = p.name
    _save_hashes(hashes)
    return hashes


def _save_hashes(hashes: dict) -> None:
    HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    HASHES_PATH.write_text(json.dumps(hashes, ensure_ascii=False, indent=1), encoding="utf-8")


def _set(**kw):
    STATUS.update(kw)


def save_uploads(files: list[tuple[str, bytes]]) -> list[Path]:
    """Сохраняет файлы в корпус (data/<...>/Загруженные), возвращает пути."""
    updir = settings.data_dir / UPLOAD_CATEGORY
    updir.mkdir(parents=True, exist_ok=True)
    saved = []
    for name, blob in files:
        name = re.sub(r"[^\w\s.,()\-№]", "_", Path(name).name)
        p = updir / name
        i = 1
        while p.exists():
            p = updir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        p.write_bytes(blob)
        saved.append(p)
    return saved


def _pipeline(paths: list[Path], index_images: bool = False):
    try:
        chunks_path = settings.processed_dir / "chunks.jsonl"
        emb_path = settings.processed_dir / "embeddings.jsonl"
        chunks_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. чанкинг
        _set(stage="Парсинг и чанкинг", detail="")
        known_docs = set()
        if chunks_path.exists():
            for line in open(chunks_path, encoding="utf-8"):
                known_docs.add(json.loads(line)["doc_id"])
        new_chunks = []
        with open(chunks_path, "a", encoding="utf-8") as f:
            for p in paths:
                cs = ingest_file(p)
                if not cs:
                    _set(detail=f"{p.name}: пустой или неподдерживаемый — пропущен")
                    continue
                if cs[0].doc_id in known_docs:
                    _set(detail=f"{p.name}: уже в базе — пропущен")
                    continue
                for c in cs:
                    f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
                new_chunks.extend(cs)
                _set(detail=f"{p.name}: +{len(cs)} чанков")
        # 1б. vision-распознавание страниц-сканов (без текстового слоя):
        # локальная модель с mmproj или облачная VLM (Gemini) — что активно
        try:
            scan_chunks = _vision_scan_chunks(paths, {c.chunk_id for c in new_chunks})
            if scan_chunks:
                with open(chunks_path, "a", encoding="utf-8") as f:
                    for c in scan_chunks:
                        f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
                new_chunks.extend(scan_chunks)
        except Exception as e:
            _set(detail=f"vision-распознавание сканов пропущено: {str(e)[:120]}")

        # 1в. опционально: vision-анализ ВСЕХ встроенных изображений (галочка в UI)
        if index_images:
            try:
                img_chunks = _vision_image_chunks(paths, {c.chunk_id for c in new_chunks})
                if img_chunks:
                    with open(chunks_path, "a", encoding="utf-8") as f:
                        for c in img_chunks:
                            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
                    new_chunks.extend(img_chunks)
            except Exception as e:
                _set(detail=f"vision-анализ изображений прерван: {str(e)[:120]}")

        _set(added_chunks=len(new_chunks))
        if not new_chunks:
            _set(running=False, stage="Готово", detail="новых документов нет")
            return

        # 2. экстракция сущностей
        ext_path = settings.processed_dir / "extractions.jsonl"
        done = 0
        with open(ext_path, "a", encoding="utf-8") as f:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(extract_chunk, asdict(c)) for c in new_chunks]
                for fut in as_completed(futures):
                    result = fut.result()
                    if result is not None:
                        with _lock:
                            f.write(json.dumps(result, ensure_ascii=False) + "\n")
                            f.flush()
                    done += 1
                    _set(stage="Экстракция сущностей (LLM)", detail=f"{done} / {len(new_chunks)}")

        # 3. эмбеддинги
        done = 0
        with open(emb_path, "a", encoding="utf-8") as f:
            for i in range(0, len(new_chunks), EMBED_BATCH):
                batch = new_chunks[i:i + EMBED_BATCH]
                vecs = embed([c.text[:6000] for c in batch])
                for c, v in zip(batch, vecs):
                    f.write(json.dumps({"chunk_id": c.chunk_id, "vector": v}) + "\n")
                done += len(batch)
                _set(stage="Эмбеддинги", detail=f"{done} / {len(new_chunks)}")

        # 4. граф
        _set(stage="Загрузка в граф знаний", detail="Neo4j: чанки, векторы, сущности, связи")
        run_load({"chunks", "embeddings", "extractions"})

        _set(running=False, stage="Готово",
             detail=f"проиндексировано документов: {len({c.doc_id for c in new_chunks})}, "
                    f"чанков: {len(new_chunks)}")
    except Exception as e:
        _set(running=False, stage="Ошибка", error=str(e)[:400])


def _vision_scan_chunks(paths: list[Path], existing_ids: set[str]) -> list[Chunk]:
    """Страницы PDF без текстового слоя → рендер → VLM-описание → чанки."""
    import hashlib as _h

    import fitz

    out: list[Chunk] = []
    for p in paths:
        if p.suffix.lower() != ".pdf":
            continue
        rel = str(p.relative_to(settings.data_dir))
        doc_id = _h.md5(rel.encode("utf-8")).hexdigest()[:12]
        with fitz.open(p) as d:
            for i, page in enumerate(d):
                infos = page.get_image_info()
                big = [x for x in infos if x["width"] >= 300 and x["height"] >= 300]
                if not big or page.get_text("text").strip():
                    continue
                chunk_id = f"{doc_id}:scan{i}"
                if chunk_id in existing_ids:
                    continue
                _set(stage="Распознаю сканы (vision)", detail=f"{p.name}, стр. {i + 1}")
                png = page.get_pixmap(dpi=140).tobytes("png")
                text = chat_vision("Ты — OCR и аналитик технической документации.",
                                   VISION_PROMPT, png, mime="image/png", max_tokens=1800)
                out.append(Chunk(
                    doc_id=doc_id, chunk_id=chunk_id,
                    text=f"[Скан, распознан VLM] {text}",
                    source_path=rel, category=UPLOAD_CATEGORY, journal=None,
                    year=None, location=f"стр. {i + 1} (скан)",
                ))
    return out


def _vision_image_chunks(paths: list[Path], existing_ids: set[str]) -> list[Chunk]:
    """Все встроенные изображения документов → VLM-описание → чанки.
    PDF: картинки со страниц с текстом (сканы уже покрыты отдельным проходом).
    DOCX/PPTX/XLSX: медиа-файлы из архива. Мелочь (<200px — иконки, логотипы) пропускаем."""
    import hashlib as _h
    import zipfile

    import fitz

    MIN_SIDE = 200
    OFFICE_MEDIA = {".docx": "word/media/", ".docm": "word/media/",
                    ".pptx": "ppt/media/", ".xlsx": "xl/media/"}
    out: list[Chunk] = []

    def describe(png: bytes, doc_id: str, rel: str, n: int, where: str):
        chunk_id = f"{doc_id}:img{n}"
        if chunk_id in existing_ids:
            return
        text = chat_vision("Ты — аналитик технической документации.",
                           IMG_PROMPT, png, mime="image/png", max_tokens=1200)
        out.append(Chunk(
            doc_id=doc_id, chunk_id=chunk_id,
            text=f"[Изображение, распознано VLM] {text}",
            source_path=rel, category=UPLOAD_CATEGORY, journal=None,
            year=None, location=where,
        ))

    for p in paths:
        rel = str(p.relative_to(settings.data_dir))
        doc_id = _h.md5(rel.encode("utf-8")).hexdigest()[:12]
        seen: set[str] = set()  # дедуп повторяющихся картинок (логотип на каждой странице)
        n = 0
        ext = p.suffix.lower()

        if ext == ".pdf":
            with fitz.open(p) as d:
                for i, page in enumerate(d):
                    if not page.get_text("text").strip():
                        continue  # страница-скан — обработана проходом 1б целиком
                    for info in page.get_image_info(xrefs=True):
                        if info["width"] < MIN_SIDE or info["height"] < MIN_SIDE:
                            continue
                        try:
                            pix = fitz.Pixmap(d, info["xref"])
                            if pix.colorspace is None:
                                continue
                            if pix.n > 3:
                                pix = fitz.Pixmap(fitz.csRGB, pix)
                            png = pix.tobytes("png")
                        except Exception:
                            continue
                        h = _h.md5(png).hexdigest()
                        if h in seen:
                            continue
                        seen.add(h)
                        n += 1
                        _set(stage="Анализирую изображения (vision)",
                             detail=f"{p.name}: стр. {i + 1}, изображение {n}")
                        describe(png, doc_id, rel, n, f"стр. {i + 1}, изображение")
        elif ext in OFFICE_MEDIA:
            with zipfile.ZipFile(p) as z:
                media = [m for m in z.namelist() if m.startswith(OFFICE_MEDIA[ext])
                         and Path(m).suffix.lower() in
                         {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}]
                for m in media:
                    data = z.read(m)
                    try:
                        img = fitz.open(stream=data, filetype=Path(m).suffix[1:])
                        rect = img[0].rect
                        if rect.width < MIN_SIDE or rect.height < MIN_SIDE:
                            continue
                        png = img[0].get_pixmap().tobytes("png")
                    except Exception:
                        continue
                    h = _h.md5(png).hexdigest()
                    if h in seen:
                        continue
                    seen.add(h)
                    n += 1
                    _set(stage="Анализирую изображения (vision)",
                         detail=f"{p.name}: изображение {n}")
                    describe(png, doc_id, rel, n, f"изображение {n}")
    return out


def start_pipeline(files: list[tuple[str, bytes]], index_images: bool = False) -> tuple[bool, str]:
    with _lock:
        if STATUS["running"]:
            return False, "индексация уже идёт — дождитесь завершения"
        bad = [n for n, _ in files if Path(n).suffix.lower() not in ALLOWED_EXT]
        if bad:
            return False, f"неподдерживаемый формат: {', '.join(bad[:3])} (нужны PDF/DOCX/PPTX/XLSX)"

        # дедуп по содержимому: тот же файл под любым именем не индексируется повторно
        hashes = _load_hashes()
        fresh, dupes = [], []
        for name, blob in files:
            h = hashlib.md5(blob).hexdigest()
            if h in hashes:
                dupes.append(f"{name} (уже в базе как «{hashes[h]}»)")
            else:
                hashes[h] = Path(name).name
                fresh.append((name, blob))
        if not fresh:
            return True, "все файлы уже в базе (идентичное содержимое): " + "; ".join(dupes[:5])
        _save_hashes(hashes)

        paths = save_uploads(fresh)
        STATUS.update(running=True, stage="Старт",
                      detail=("пропущены дубликаты: " + "; ".join(dupes[:5])) if dupes else "",
                      error=None, files=[p.name for p in paths], added_chunks=0)
    threading.Thread(target=_pipeline, args=(paths, index_images), daemon=True).start()
    msg = f"принято файлов: {len(paths)}"
    if dupes:
        msg += f", пропущено дубликатов: {len(dupes)}"
    return True, msg


def clear_all() -> tuple[bool, str]:
    """Полная очистка проиндексированных данных: jsonl, граф Neo4j, загруженные файлы.
    Исходный корпус кейса на диске не трогаем."""
    with _lock:
        if STATUS["running"]:
            return False, "идёт индексация — очистка невозможна"
        STATUS.update(running=False, stage="Очистка данных", detail="", error=None,
                      files=[], added_chunks=0)
    try:
        for name in ("chunks.jsonl", "extractions.jsonl", "embeddings.jsonl"):
            (settings.processed_dir / name).unlink(missing_ok=True)
        HASHES_PATH.unlink(missing_ok=True)

        from sciknot.graph.loader import get_driver
        d = get_driver()
        try:
            with d.session() as s:
                s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
        finally:
            d.close()

        updir = settings.data_dir / UPLOAD_CATEGORY
        if updir.exists():
            for p in updir.iterdir():
                if p.is_file():
                    p.unlink(missing_ok=True)

        STATUS.update(stage="Готово", detail="все проиндексированные данные удалены")
        return True, "данные очищены: граф, индексы, загруженные файлы"
    except Exception as e:
        STATUS.update(stage="Ошибка", error=str(e)[:400])
        return False, f"ошибка очистки: {e}"


def corpus_stats() -> dict:
    from sciknot.graph.loader import get_driver
    d = get_driver()
    try:
        with d.session() as s:
            by_cat = s.run("""
                MATCH (doc:Document)
                RETURN doc.category AS category, count(doc) AS docs
                ORDER BY docs DESC""").data()
            totals = s.run("""
                MATCH (c:Chunk) WITH count(c) AS chunks
                MATCH (d:Document) RETURN chunks, count(d) AS docs""").single()
        return {"by_category": by_cat,
                "docs": totals["docs"], "chunks": totals["chunks"]}
    finally:
        d.close()

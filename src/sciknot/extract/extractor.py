"""Извлечение сущностей и связей из чанков через LLM. Параллельно, с чекпоинтом."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from sciknot.config import settings
from sciknot.llm import chat_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — эксперт по горно-металлургическим технологиям (гидрометаллургия, пирометаллургия, \
обогащение, геомеханика, экология). Извлекаешь структурированные знания из фрагментов научных документов на русском и английском.

Верни строго JSON без пояснений:
{
  "materials": ["никель", "сульфаты", ...],           // материалы и вещества, им. падеж, нижний регистр
  "processes": ["электроэкстракция", "кучное выщелачивание", ...],
  "equipment": ["печь взвешенной плавки", "ванна электроэкстракции", ...],
  "experiments": ["опыт кучного выщелачивания при -20°c", ...],  // конкретные эксперименты/опыты/испытания с их ключевым условием
  "facilities": ["надеждинский металлургический завод", "лаборатория гидрометаллургии", ...],  // заводы, фабрики, лаборатории, институты
  "parameters": [{"name": "концентрация сульфатов", "value": "300", "unit": "мг/л", "operator": "<=", "about": "к какому процессу/материалу относится"}],
  "experts": [{"name": "Иванов И.И.", "affiliation": "Институт Гипроникель"}],  // ТОЛЬКО люди (авторы, исследователи, ФИО); организации сюда НЕ писать — они идут в facilities
  "geography": "RU" | "foreign" | "both" | null,       // о чьей практике фрагмент
  "relations": [{"source": "электроэкстракция", "type": "uses_material", "target": "никель"}],
  "topics": ["очистка воды", ...],                     // 1-4 тематических тега
  "summary": "одно предложение — суть фрагмента"
}

Типы связей (type): uses_material, produces_output, operates_at_condition, uses_equipment, applied_for, expert_in, contradicts, validated_by.
validated_by — вывод/эффект подтверждён экспериментом или источником («эффект X validated_by эксперимент Y»).
source/target — имена из materials/processes/equipment/experiments/facilities/topics/experts.
Термины нормализуй: единственное число, именительный падеж, нижний регистр (кроме ФИО и формул).
Если фрагмент — оглавление, список литературы или служебный текст без знаний, верни пустые списки.
Не выдумывай: только то, что явно есть в тексте. Числа и единицы переноси точно."""


def extract_chunk(chunk: dict) -> dict | None:
    try:
        result = chat_json(SYSTEM_PROMPT, chunk["text"], model=settings.extract_model)
    except Exception as e:
        log.warning("Экстракция не удалась для %s: %s", chunk["chunk_id"], e)
        return None
    result["chunk_id"] = chunk["chunk_id"]
    result["doc_id"] = chunk["doc_id"]
    return result


def run_extraction(
    chunks_path: Path | None = None,
    out_path: Path | None = None,
    workers: int = 12,
    limit: int | None = None,
) -> Path:
    chunks_path = chunks_path or settings.processed_dir / "chunks.jsonl"
    out_path = out_path or settings.processed_dir / "extractions.jsonl"

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
    log.info("Всего чанков: %d, уже готово: %d, в работу: %d", len(chunks), len(done), len(todo))

    lock = threading.Lock()
    n_ok = 0
    n_fail = 0
    with open(out_path, "a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(extract_chunk, c): c["chunk_id"] for c in todo}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="extract", unit="chunk"):
                result = fut.result()
                if result is None:
                    n_fail += 1
                    continue
                with lock:
                    f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    f.flush()
                n_ok += 1

    print(f"OK: извлечено {n_ok}, ошибок {n_fail} -> {out_path}")
    return out_path

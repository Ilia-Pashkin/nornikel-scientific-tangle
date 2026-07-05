"""Гибридный поиск: разбор запроса → векторный поиск + граф → синтез ответа с источниками."""

import json
import logging
import re
from dataclasses import dataclass, field

from sciknot.graph.loader import get_driver, norm_name
from sciknot.llm import answer_model, chat_json, chat_text, chat_text_stream, embed

log = logging.getLogger(__name__)

PARSE_PROMPT = """Ты разбираешь поисковый запрос к базе знаний по горно-металлургическим технологиям.
Верни строго JSON:
{
  "entities": ["электроэкстракция", "католит", ...],   // ключевые материалы/процессы/оборудование из запроса, норм. форма, нижний регистр
  "geography": "RU" | "foreign" | null,                 // если запрос явно про отечественную ИЛИ зарубежную практику
  "year_from": null | 2020,                             // если указан временной диапазон ("за последние 5 лет" => текущий год минус 5)
  "search_query": "переформулировка запроса для семантического поиска (плотная, с синонимами RU/EN)"
}
Текущий год: 2026."""

ANSWER_PROMPT = """Ты — аналитик карты знаний R&D «Научный клубок» (горно-металлургическая отрасль).
Отвечай ТОЛЬКО на основе приведённых фрагментов и фактов графа знаний. Не выдумывай.

Ответ — структурированный аналитический обзор со следующими разделами (markdown, ### заголовки).
Разделы обязательны; если по данным раздел пуст — одна честная строка почему.

### Краткий вывод
2–4 предложения по сути вопроса.

### Обзор источников
Сгруппируй найденное по методу/технологии/подходу. Внутри групп указывай год, географию
(отечественная/зарубежная практика) и уровень детализации источника (обзор / статья / эксперимент).

### Консенсус и разногласия
Что подтверждается несколькими независимыми источниками (укажи сколькими).
Где данные расходятся или противоречат друг другу — покажи обе позиции со ссылками.

### Достоверность
Оценка уверенности в ключевых выводах (высокая/средняя/низкая) с обоснованием:
число подтверждающих источников, их свежесть и уровень детализации.

### Пробелы знаний
Какие аспекты вопроса (комбинации материал–режим–условие) не покрыты или слабо покрыты
представленными данными. Какие технологии описаны только в отечественной или только
в зарубежной литературе (по представленным фрагментам).

### Рекомендации
- похожие кейсы и потенциально применимые решения из смежных областей (из фрагментов и графа);
- эксперты и организации, работавшие с аналогичными задачами (из списка ниже);
- смежные темы для углублённого изучения (из связей графа).

Правила: сегодня {today} — относительные периоды («за последние 5 лет») отсчитывай от этой даты.
Если вопрос сравнительный (вариант А vs вариант Б, отечественная vs зарубежная практика) —
включи в «Обзор источников» markdown-таблицу сравнения по ключевым параметрам (эффективность,
затраты, условия применимости, ограничения). После каждого утверждения — ссылка [N] на фрагмент;
числа и единицы переноси точно; у фрагментов без года в шапке возраст данных оценивай
по содержимому и помечай как предположение;
если фрагменты не отвечают на вопрос — скажи прямо в «Кратком выводе». Отвечай на русском."""


def _answer_prompt() -> str:
    import datetime
    return ANSWER_PROMPT.replace("{today}", datetime.date.today().strftime("%d.%m.%Y"))


@dataclass
class SearchResult:
    answer: str
    sources: list[dict] = field(default_factory=list)
    graph_facts: list[dict] = field(default_factory=list)
    parsed_query: dict = field(default_factory=dict)


def parse_query(query: str) -> dict:
    try:
        return chat_json(PARSE_PROMPT, query, model=answer_model(), max_tokens=2000)
    except Exception as e:
        log.warning("Разбор запроса не удался: %s", e)
        return {"entities": [], "geography": None, "year_from": None, "search_query": query}


def vector_search(driver, search_query: str, geography: str | None = None,
                  year_from: int | None = None, k: int = 10) -> list[dict]:
    qvec = embed([search_query])[0]
    cypher = """
    CALL db.index.vector.queryNodes('chunk_vec', $k_raw, $qvec) YIELD node, score
    MATCH (node)-[:PART_OF]->(d:Document)
    WHERE ($geo IS NULL OR node.geography = $geo OR node.geography = 'both' OR node.geography IS NULL)
      AND ($year_from IS NULL OR d.year IS NULL OR d.year >= $year_from)
    RETURN node.chunk_id AS chunk_id, node.text AS text, node.location AS location,
           node.geography AS geography, node.summary AS summary,
           d.source_path AS source_path, d.category AS category, d.journal AS journal, d.year AS year,
           score
    ORDER BY score DESC LIMIT $k
    """
    with driver.session() as s:
        rows = s.run(cypher, qvec=qvec, k_raw=k * 4, k=k,
                     geo=geography, year_from=year_from).data()
    return rows


def keyword_search(driver, query: str, entities: list[str],
                   geography: str | None = None, year_from: int | None = None,
                   k: int = 5) -> list[dict]:
    """Fulltext-поиск по тексту чанков — ловит точные формулировки, которые вектор промахивает."""
    words = [w for w in re.findall(r"[а-яёa-z]{5,}", query.lower())]
    terms = sorted(set(words) | {norm_name(e) for e in entities if len(e) >= 5})
    if not terms:
        return []
    lucene = " OR ".join(f'"{t}"' for t in terms[:15])
    cypher = """
    CALL db.index.fulltext.queryNodes('chunk_ft', $q) YIELD node, score
    MATCH (node)-[:PART_OF]->(d:Document)
    WHERE ($geo IS NULL OR node.geography = $geo OR node.geography = 'both' OR node.geography IS NULL)
      AND ($year_from IS NULL OR d.year IS NULL OR d.year >= $year_from)
    RETURN node.chunk_id AS chunk_id, node.text AS text, node.location AS location,
           node.geography AS geography, node.summary AS summary,
           d.source_path AS source_path, d.category AS category, d.journal AS journal, d.year AS year,
           score
    ORDER BY score DESC LIMIT $k
    """
    try:
        with driver.session() as s:
            return s.run(cypher, q=lucene, geo=geography, year_from=year_from, k=k).data()
    except Exception as e:
        log.warning("Fulltext-поиск не удался: %s", e)
        return []


def graph_neighborhood(driver, entities: list[str], limit: int = 40) -> list[dict]:
    """Факты графа вокруг сущностей запроса: связи + топ-параметры + эксперты."""
    if not entities:
        return []
    names = [norm_name(e) for e in entities]
    facts: list[dict] = []
    with driver.session() as s:
        # fulltext-матч сущностей (устойчив к падежам частично за счёт нормализации)
        rows = s.run(
            """
            UNWIND $names AS q
            CALL db.index.fulltext.queryNodes('entity_ft', q) YIELD node, score
            WITH DISTINCT node LIMIT 10
            MATCH (node)-[r:RELATES]-(other)
            RETURN labels(node)[0] AS src_type, node.name AS source, r.type AS rel,
                   labels(other)[0] AS dst_type, other.name AS target,
                   r.confidence AS confidence
            ORDER BY r.confidence DESC LIMIT $limit
            """,
            names=names, limit=limit,
        ).data()
        facts.extend({"kind": "relation", **r} for r in rows)

        experts = s.run(
            """
            UNWIND $names AS q
            CALL db.index.fulltext.queryNodes('entity_ft', q) YIELD node, score
            WITH DISTINCT node LIMIT 10
            MATCH (c:Chunk)-[:MENTIONS]->(node)
            MATCH (c)-[:MENTIONS_EXPERT]->(x:Expert)
            RETURN x.name AS name, x.affiliation AS affiliation, count(DISTINCT c) AS mentions
            ORDER BY mentions DESC LIMIT 8
            """,
            names=names,
        ).data()
        facts.extend({"kind": "expert", **r} for r in experts)
    return facts


def _retrieve(driver, query: str, parsed: dict, k: int) -> list[dict]:
    """Гибридный подбор чанков: вектор + fulltext, без дублей."""
    chunks = vector_search(
        driver,
        parsed.get("search_query") or query,
        geography=parsed.get("geography"),
        year_from=parsed.get("year_from"),
        k=k,
    )
    seen = {c["chunk_id"] for c in chunks}
    kw = keyword_search(driver, query, parsed.get("entities") or [],
                        geography=parsed.get("geography"), year_from=parsed.get("year_from"))
    extra = [c for c in kw if c["chunk_id"] not in seen][:4]
    for c in extra:
        c["score"] = 0.0
    return chunks + extra


def _build_synthesis_prompt(query: str, chunks: list[dict], facts: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, start=1):
        header = f"[{i}] {c['source_path']} ({c['location']}; категория: {c['category']}"
        if c.get("year"):
            header += f", год: {c['year']}"
        if c.get("geography"):
            header += f", география: {c['geography']}"
        header += ")"
        parts.append(header + "\n" + c["text"][:2500])
    context = "\n\n---\n\n".join(parts)

    facts_txt = ""
    rel_facts = [f for f in facts if f.get("kind") == "relation"][:25]
    if rel_facts:
        facts_txt += "\n\nФакты графа знаний (смежные сущности; подтверждены N источниками):\n" + "\n".join(
            f"- {f['source']} —{f['rel']}→ {f['target']} (источников: {f['confidence']})"
            for f in rel_facts
        )
    experts = [f for f in facts if f.get("kind") == "expert"][:8]
    if experts:
        facts_txt += "\n\nЭксперты и организации по теме (частота упоминаний рядом с темой):\n" + "\n".join(
            f"- {x['name']}" + (f", {x['affiliation']}" if x.get("affiliation") else "") +
            f" (упоминаний: {x['mentions']})"
            for x in experts
        )
    geo = {"RU": 0, "foreign": 0, "both": 0, None: 0}
    years = [c["year"] for c in chunks if c.get("year")]
    for c in chunks:
        geo[c.get("geography") if c.get("geography") in geo else None] += 1
    years_part = f"; годы источников: {min(years)}–{max(years)}" if years else ""
    facts_txt += (f"\n\nСтатистика подборки: фрагментов {len(chunks)}; "
                  f"география: отечественная {geo['RU']}, зарубежная {geo['foreign']}, "
                  f"обе {geo['both']}, не размечена {geo[None]}{years_part}")

    return f"Вопрос: {query}\n\nФрагменты:\n{context}{facts_txt}"


def synthesize(query: str, chunks: list[dict], facts: list[dict]) -> str:
    user = _build_synthesis_prompt(query, chunks, facts)
    return chat_text(_answer_prompt(), user, model=answer_model(), max_tokens=6000)


def ask_stream(query: str, k: int = 10):
    """Генератор событий для SSE: stage → parsed → sources → experts → delta* → done."""
    driver = get_driver()
    try:
        yield {"type": "stage", "stage": "Разбираю запрос…"}
        parsed = parse_query(query)
        yield {"type": "parsed", "data": parsed}

        yield {"type": "stage", "stage": "Ищу по векторному индексу и графу…"}
        chunks = _retrieve(driver, query, parsed, k)
        yield {"type": "sources", "data": [
            {k2: c.get(k2) for k2 in ("chunk_id", "source_path", "location", "category",
                                      "journal", "year", "geography", "summary", "score")}
            for c in chunks
        ]}
        facts = graph_neighborhood(driver, parsed.get("entities") or [])
        yield {"type": "experts", "data": [f for f in facts if f.get("kind") == "expert"][:8]}

        if not chunks:
            yield {"type": "delta", "text": "В проиндексированной базе не нашлось релевантных фрагментов."}
            yield {"type": "done"}
            return

        yield {"type": "stage", "stage": "Формулирую ответ…"}
        user = _build_synthesis_prompt(query, chunks, facts)
        for delta in chat_text_stream(_answer_prompt(), user, model=answer_model(), max_tokens=6000):
            yield {"type": "delta", "text": delta}
        yield {"type": "done"}
    finally:
        driver.close()


def find_gaps(limit: int = 20) -> list[dict]:
    """Пробелы в знаниях по всему корпусу: слабо освещённые процессы и темы,
    описанные только в отечественной или только в зарубежной литературе."""
    driver = get_driver()
    try:
        with driver.session() as s:
            weak = s.run(
                """
                MATCH (p:Process)<-[:MENTIONS]-(c:Chunk)
                WITH p, count(DISTINCT c.doc_id) AS docs
                WHERE docs <= 2
                RETURN 'процесс освещён слабо' AS gap_type, p.name AS name, docs AS coverage
                ORDER BY docs ASC, name LIMIT $limit
                """,
                limit=limit,
            ).data()
            geo_gaps = s.run(
                """
                MATCH (t:Topic)<-[:MENTIONS]-(c:Chunk)
                WITH t, collect(DISTINCT c.geography) AS geos, count(DISTINCT c.doc_id) AS docs
                WHERE docs >= 2 AND NOT ('RU' IN geos AND ('foreign' IN geos OR 'both' IN geos))
                RETURN CASE WHEN 'RU' IN geos THEN 'тема только в отечественной практике'
                            ELSE 'тема только в зарубежной практике' END AS gap_type,
                       t.name AS name, docs AS coverage
                ORDER BY docs DESC LIMIT $limit
                """,
                limit=limit,
            ).data()
        return weak + geo_gaps
    finally:
        driver.close()


def ask(query: str, k: int = 10) -> SearchResult:
    driver = get_driver()
    try:
        parsed = parse_query(query)
        chunks = _retrieve(driver, query, parsed, k)
        facts = graph_neighborhood(driver, parsed.get("entities") or [])
        answer = synthesize(query, chunks, facts) if chunks else \
            "В проиндексированной базе не нашлось релевантных фрагментов по этому запросу."
        return SearchResult(answer=answer, sources=chunks, graph_facts=facts, parsed_query=parsed)
    finally:
        driver.close()

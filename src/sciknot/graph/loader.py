"""Загрузка извлечённых знаний в Neo4j: документы, чанки, сущности, связи, векторный индекс."""

import json
import logging
import re
from pathlib import Path

from neo4j import GraphDatabase
from tqdm import tqdm

from sciknot.config import settings

log = logging.getLogger(__name__)

ENTITY_LABELS = {
    "materials": "Material",
    "processes": "Process",
    "equipment": "Equipment",
    "topics": "Topic",
    "experiments": "Experiment",
    "facilities": "Facility",
}

RELATION_TYPES = {
    "uses_material", "produces_output", "operates_at_condition",
    "uses_equipment", "applied_for", "expert_in", "contradicts", "validated_by",
}

VECTOR_DIM = 1024

# страховка от LLM: записи «экспертов», похожие на организации, грузим как Facility
ORG_PATTERN = re.compile(
    r"(?i)(ооо|зао|оао|пао|ao |гмк|нии|институт|универс|академ|лаборат|завод|фабрика|"
    r"комбинат|рудник|компан|центр|кафедра|фгуп|фгбу|corp|inc\b|ltd|gmbh|llc|"
    r"university|institute|laboratory|college|company)"
)


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def get_driver():
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


SCHEMA_QUERIES = [
    "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT material_name IF NOT EXISTS FOR (m:Material) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT process_name IF NOT EXISTS FOR (p:Process) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT equipment_name IF NOT EXISTS FOR (e:Equipment) REQUIRE e.name IS UNIQUE",
    "CREATE CONSTRAINT topic_name IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT experiment_name IF NOT EXISTS FOR (ex:Experiment) REQUIRE ex.name IS UNIQUE",
    "CREATE CONSTRAINT facility_name IF NOT EXISTS FOR (fc:Facility) REQUIRE fc.name IS UNIQUE",
    "CREATE CONSTRAINT expert_name IF NOT EXISTS FOR (x:Expert) REQUIRE x.name IS UNIQUE",
    f"""CREATE VECTOR INDEX chunk_vec IF NOT EXISTS
        FOR (c:Chunk) ON (c.embedding)
        OPTIONS {{indexConfig: {{`vector.dimensions`: {VECTOR_DIM}, `vector.similarity_function`: 'cosine'}}}}""",
    "CREATE FULLTEXT INDEX entity_ft IF NOT EXISTS FOR (n:Material|Process|Equipment|Topic|Expert|Experiment|Facility) ON EACH [n.name]",
    "CREATE FULLTEXT INDEX chunk_ft IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]",
]


def init_schema(driver):
    with driver.session() as s:
        for q in SCHEMA_QUERIES:
            s.run(q)
    log.info("Схема и индексы готовы")


def load_chunks(driver, chunks_path: Path):
    chunks = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
    with driver.session() as s:
        for i in tqdm(range(0, len(chunks), 200), desc="chunks", unit="batch"):
            batch = chunks[i : i + 200]
            s.run(
                """
                UNWIND $rows AS row
                MERGE (d:Document {doc_id: row.doc_id})
                  SET d.source_path = row.source_path, d.category = row.category,
                      d.journal = row.journal, d.year = row.year
                MERGE (c:Chunk {chunk_id: row.chunk_id})
                  SET c.text = row.text, c.location = row.location, c.doc_id = row.doc_id
                MERGE (c)-[:PART_OF]->(d)
                """,
                rows=batch,
            )


def load_embeddings(driver, emb_path: Path):
    rows = [json.loads(l) for l in open(emb_path, encoding="utf-8")]
    with driver.session() as s:
        for i in tqdm(range(0, len(rows), 100), desc="vectors", unit="batch"):
            batch = rows[i : i + 100]
            s.run(
                """
                UNWIND $rows AS row
                MATCH (c:Chunk {chunk_id: row.chunk_id})
                CALL db.create.setNodeVectorProperty(c, 'embedding', row.vector)
                """,
                rows=batch,
            )


def load_extractions(driver, ext_path: Path):
    exts = [json.loads(l) for l in open(ext_path, encoding="utf-8")]
    ent_rows = []       # (label, name, chunk_id)
    expert_rows = []
    param_rows = []
    rel_rows = []
    geo_rows = []

    for e in exts:
        cid = e["chunk_id"]
        summary = e.get("summary")
        geo = e.get("geography")
        if geo in ("RU", "foreign", "both"):
            geo_rows.append({"chunk_id": cid, "geo": geo, "summary": summary})
        elif summary:
            geo_rows.append({"chunk_id": cid, "geo": None, "summary": summary})

        for key, label in ENTITY_LABELS.items():
            for name in e.get(key) or []:
                n = norm_name(name)
                if 2 <= len(n) <= 120:
                    ent_rows.append({"label": label, "name": n, "chunk_id": cid})

        for x in e.get("experts") or []:
            if isinstance(x, dict) and x.get("name"):
                name = str(x["name"]).strip()
                if ORG_PATTERN.search(name):
                    # организация, ошибочно попавшая в эксперты -> Facility
                    n = norm_name(name)
                    if 2 <= len(n) <= 120:
                        ent_rows.append({"label": "Facility", "name": n, "chunk_id": cid})
                    continue
                expert_rows.append({
                    "name": name,
                    "affiliation": x.get("affiliation"),
                    "chunk_id": cid,
                })

        for p in e.get("parameters") or []:
            if isinstance(p, dict) and p.get("name"):
                num = None
                m = re.search(r"-?\d+(?:[.,]\d+)?", str(p.get("value") or ""))
                if m:
                    num = float(m.group(0).replace(",", "."))
                param_rows.append({
                    "chunk_id": cid,
                    "name": norm_name(p["name"]),
                    "value": str(p.get("value") or ""),
                    "num": num,
                    "unit": p.get("unit"),
                    "operator": p.get("operator"),
                    "about": p.get("about"),
                })

        for r in e.get("relations") or []:
            if isinstance(r, dict) and r.get("source") and r.get("target"):
                rtype = str(r.get("type") or "").strip()
                if rtype in RELATION_TYPES:
                    rel_rows.append({
                        "chunk_id": cid,
                        "source": norm_name(r["source"]),
                        "target": norm_name(r["target"]),
                        "type": rtype,
                    })

    with driver.session() as s:
        # Parameter-ноды создаются только этим загрузчиком — чистим перед повторной загрузкой,
        # чтобы прогон по расширенному extractions.jsonl не плодил дубликаты
        s.run("MATCH (p:Parameter) CALL (p) { DETACH DELETE p } IN TRANSACTIONS OF 5000 ROWS")

        for i in tqdm(range(0, len(geo_rows), 500), desc="chunk meta", unit="batch"):
            s.run(
                """UNWIND $rows AS row
                   MATCH (c:Chunk {chunk_id: row.chunk_id})
                   SET c.geography = row.geo, c.summary = row.summary""",
                rows=geo_rows[i : i + 500],
            )

        for label in set(r["label"] for r in ent_rows):
            rows = [r for r in ent_rows if r["label"] == label]
            for i in tqdm(range(0, len(rows), 500), desc=f"entities:{label}", unit="batch"):
                s.run(
                    f"""UNWIND $rows AS row
                        MERGE (n:{label} {{name: row.name}})
                        WITH n, row
                        MATCH (c:Chunk {{chunk_id: row.chunk_id}})
                        MERGE (c)-[:MENTIONS]->(n)""",
                    rows=rows[i : i + 500],
                )

        for i in tqdm(range(0, len(expert_rows), 500), desc="experts", unit="batch"):
            s.run(
                """UNWIND $rows AS row
                   MERGE (x:Expert {name: row.name})
                   SET x.affiliation = coalesce(row.affiliation, x.affiliation)
                   WITH x, row
                   MATCH (c:Chunk {chunk_id: row.chunk_id})
                   MERGE (c)-[:MENTIONS_EXPERT]->(x)""",
                rows=expert_rows[i : i + 500],
            )

        # аффилиации экспертов -> организации + связь «работает в»
        affil_rows = [{"expert": r["name"], "facility": norm_name(r["affiliation"])}
                      for r in expert_rows
                      if r.get("affiliation") and 2 <= len(norm_name(r["affiliation"])) <= 120]
        for i in tqdm(range(0, len(affil_rows), 500), desc="affiliations", unit="batch"):
            s.run(
                """UNWIND $rows AS row
                   MATCH (x:Expert {name: row.expert})
                   MERGE (f:Facility {name: row.facility})
                   MERGE (x)-[:AFFILIATED_WITH]->(f)""",
                rows=affil_rows[i : i + 500],
            )

        for i in tqdm(range(0, len(param_rows), 500), desc="parameters", unit="batch"):
            s.run(
                """UNWIND $rows AS row
                   MATCH (c:Chunk {chunk_id: row.chunk_id})
                   CREATE (p:Parameter {name: row.name, value: row.value, num: row.num,
                                        unit: row.unit, operator: row.operator, about: row.about})
                   CREATE (c)-[:HAS_PARAMETER]->(p)""",
                rows=param_rows[i : i + 500],
            )

        # связи между сущностями: source/target могут быть любого типа — матчим по имени
        for i in tqdm(range(0, len(rel_rows), 300), desc="relations", unit="batch"):
            s.run(
                """UNWIND $rows AS row
                   MATCH (a {name: row.source}) WHERE a:Material OR a:Process OR a:Equipment OR a:Topic OR a:Expert OR a:Experiment OR a:Facility
                   MATCH (b {name: row.target}) WHERE b:Material OR b:Process OR b:Equipment OR b:Topic OR b:Expert OR b:Experiment OR b:Facility
                   MERGE (a)-[r:RELATES {type: row.type}]->(b)
                   ON CREATE SET r.sources = [row.chunk_id], r.confidence = 1,
                                 r.created_at = date(), r.updated_at = date()
                   ON MATCH SET r.sources = CASE WHEN row.chunk_id IN r.sources THEN r.sources
                                                 ELSE r.sources + row.chunk_id END,
                                r.confidence = size(r.sources),
                                r.updated_at = date()""",
                rows=rel_rows[i : i + 300],
            )

    print(f"OK: сущностей {len(ent_rows)}, экспертов {len(expert_rows)}, "
          f"параметров {len(param_rows)}, связей {len(rel_rows)}")


def run_load(stages: set[str] | None = None):
    stages = stages or {"chunks", "embeddings", "extractions"}
    driver = get_driver()
    try:
        init_schema(driver)
        if "chunks" in stages:
            load_chunks(driver, settings.processed_dir / "chunks.jsonl")
        emb = settings.processed_dir / "embeddings.jsonl"
        if "embeddings" in stages and emb.exists():
            load_embeddings(driver, emb)
        ext = settings.processed_dir / "extractions.jsonl"
        if "extractions" in stages and ext.exists():
            load_extractions(driver, ext)
    finally:
        driver.close()

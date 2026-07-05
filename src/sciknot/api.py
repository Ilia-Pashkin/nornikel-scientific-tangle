"""FastAPI-бэкенд: SSE-стриминг ответов (с честной остановкой), граф, пробелы,
менеджер моделей, vision, аудит. Фронт — ванильный JS в src/sciknot/web."""

import datetime
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sciknot import dataops, llm, localllm
from sciknot.config import ROOT
from sciknot.graph.loader import get_driver, norm_name
from sciknot.search.hybrid import ask_stream, find_gaps

app = FastAPI(title="Научный клубок")

WEB = Path(__file__).parent / "web"
AUDIT_LOG = ROOT / "audit.log"


def audit(action: str, payload: str):
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "action": action, "payload": payload[:300],
        }, ensure_ascii=False) + "\n")


def ensure_local_backends() -> list[str]:
    """5.3.1: автозапуск зарегистрированных локальных моделей, если активны, но не подняты."""
    reg = localllm.load_registry()
    cfg = llm.current_config()
    started = []
    for base in {cfg["llm_base"], cfg["embed_base"]}:
        if "127.0.0.1" not in base and "localhost" not in base:
            continue
        entry = localllm.entry_for_base_url(reg, base)
        if entry and not localllm.is_running(entry["port"]):
            ok, msg = localllm.ensure_running(reg, entry)
            started.append(f"{entry['name']}: {msg}")
            if not ok:
                raise RuntimeError(f"Не удалось запустить «{entry['name']}»: {msg}")
    return started


# ---------- Поиск (SSE + Стоп) ----------

class AskRequest(BaseModel):
    query: str
    image_b64: str | None = None   # вложение-картинка к вопросу (base64, без data-uri префикса)
    image_mime: str = "image/png"


@app.post("/api/ask")
def api_ask(req: AskRequest):
    audit("search", req.query + (" [+изображение]" if req.image_b64 else ""))

    def gen():
        try:
            cfg = llm.current_config()
            if "127.0.0.1" in cfg["llm_base"] or "127.0.0.1" in cfg["embed_base"]:
                yield "data: " + json.dumps(
                    {"type": "stage", "stage": "Проверяю локальные модели…"}, ensure_ascii=False) + "\n\n"
            ensure_local_backends()
            query = req.query
            if req.image_b64:
                import base64
                yield "data: " + json.dumps(
                    {"type": "stage", "stage": f"Распознаю изображение ({llm.vision_model()})…"},
                    ensure_ascii=False) + "\n\n"
                desc = llm.chat_vision(
                    "Ты — эксперт по горно-металлургической документации. Отвечай на русском.",
                    "Опиши содержимое изображения максимально предметно: текст, графики (оси, кривые, "
                    "числовые значения), таблицы, схемы. Это описание станет частью поискового запроса.",
                    base64.b64decode(req.image_b64), mime=req.image_mime)
                yield "data: " + json.dumps(
                    {"type": "vision_desc", "text": desc}, ensure_ascii=False) + "\n\n"
                query = f"{req.query}\n\nК вопросу приложено изображение. Его содержимое: {desc}"
            for ev in ask_stream(query):
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
        except GeneratorExit:
            audit("search_stopped", req.query)
            raise
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- Граф и пробелы ----------

@app.get("/api/graph")
def api_graph(q: str, limit: int = 60, experts: bool = True):
    """Окрестность сущности: типизированные связи (цепочки материал→процесс→оборудование→результат),
    покрытие источниками (подсветка пробелов), эксперты по теме."""
    audit("graph", q)
    driver = get_driver()
    nodes: dict[str, dict] = {}
    edges = []
    try:
        with driver.session() as s:
            roots = s.run(
                """CALL db.index.fulltext.queryNodes('entity_ft', $q) YIELD node
                   WHERE NOT node:Expert RETURN node.name AS name LIMIT 5""",
                q=norm_name(q)).value()
            if not roots:
                return {"nodes": [], "edges": []}

            rows = s.run(
                """
                UNWIND $roots AS root
                MATCH (n {name: root})-[r:RELATES]-(other)
                RETURN labels(n)[0] AS st, n.name AS s, r.type AS rel,
                       r.confidence AS conf,
                       toString(coalesce(r.updated_at, r.created_at, '')) AS upd,
                       labels(other)[0] AS tt, other.name AS t
                ORDER BY r.confidence DESC LIMIT $lim
                """,
                roots=roots, lim=limit).data()
            for r in rows:
                nodes.setdefault(r["s"], {"id": r["s"], "group": r["st"]})
                nodes.setdefault(r["t"], {"id": r["t"], "group": r["tt"]})
                edges.append({"from": r["s"], "to": r["t"], "rtype": r["rel"],
                              "conf": r["conf"], "upd": r["upd"]})

            # покрытие источниками -> подсветка пробелов (⚠ слабо освещено)
            cov = s.run(
                """UNWIND $names AS nm
                   MATCH (n {name: nm})<-[:MENTIONS]-(c:Chunk)
                   RETURN nm AS name, count(DISTINCT c.doc_id) AS docs""",
                names=list(nodes)).data()
            cov_map = {r["name"]: r["docs"] for r in cov}
            for n in nodes.values():
                n["coverage"] = cov_map.get(n["id"], 0)
                n["weak"] = n["coverage"] <= 2
                n["root"] = n["id"] in roots

            # эксперты и организации по теме запроса
            if experts:
                xp = s.run(
                    """UNWIND $roots AS root
                       MATCH (c:Chunk)-[:MENTIONS]->({name: root})
                       MATCH (c)-[:MENTIONS_EXPERT]->(x:Expert)
                       WITH root, x, count(DISTINCT c) AS mentions
                       ORDER BY mentions DESC LIMIT 10
                       RETURN root, x.name AS name, x.affiliation AS aff, mentions""",
                    roots=roots).data()
                for r in xp:
                    label = r["name"] + (f" ({r['aff']})" if r["aff"] else "")
                    nodes.setdefault(r["name"], {"id": r["name"], "group": "Expert",
                                                 "coverage": r["mentions"], "weak": False,
                                                 "root": False, "expert_label": label})
                    edges.append({"from": r["root"], "to": r["name"],
                                  "rtype": "expert", "conf": r["mentions"]})

                # организации/лаборатории по теме (co-occurrence в тех же чанках)
                fc = s.run(
                    """UNWIND $roots AS root
                       MATCH (c:Chunk)-[:MENTIONS]->({name: root})
                       MATCH (c)-[:MENTIONS]->(f:Facility)
                       WITH root, f, count(DISTINCT c) AS mentions
                       ORDER BY mentions DESC LIMIT 6
                       RETURN root, f.name AS name, mentions""",
                    roots=roots).data()
                for r in fc:
                    nodes.setdefault(r["name"], {"id": r["name"], "group": "Facility",
                                                 "coverage": r["mentions"], "weak": False,
                                                 "root": False})
                    edges.append({"from": r["root"], "to": r["name"],
                                  "rtype": "facility", "conf": r["mentions"]})
    finally:
        driver.close()
    # дедуп рёбер (цепочки порождают повторы пар)
    uniq: dict[tuple, dict] = {}
    for e in edges:
        key = (e["from"], e["to"], e["rtype"])
        if key not in uniq or (e["conf"] or 0) > (uniq[key]["conf"] or 0):
            uniq[key] = e
    return {"nodes": list(nodes.values()), "edges": list(uniq.values())}


# ---------- Модели / подключения ----------

@app.get("/api/models")
def api_models():
    reg = localllm.load_registry()
    cfg = llm.current_config()
    slot_state = {}
    for name, slot in reg["slots"].items():
        running = localllm.is_running(slot["port"])
        slot_state[name] = {
            "port": slot["port"],
            "active_model": slot["active_model"],
            "running": running,
            "alias": localllm.running_alias(slot["port"]) if running else None,
        }
    return {
        "registry": reg,
        "slots": slot_state,
        "active": {"llm_base": cfg["llm_base"], "embed_base": cfg["embed_base"],
                   "answer_model": cfg["answer_model"], "embed_model": cfg["embed_model"],
                   "vision_model": cfg["vision_model"]},
    }


class SlotActivate(BaseModel):
    slot: str  # chat | embedding
    name: str
    port: int | None = None


@app.post("/api/slots/activate")
def api_slot_activate(req: SlotActivate):
    reg = localllm.load_registry()
    audit("slot_activate", f"{req.slot}: {req.name}")
    ok, msg = localllm.activate_slot(reg, req.slot, req.name, req.port)
    if ok:
        p = reg["slots"][req.slot]["port"]
        if req.slot == "chat":
            # локальная модель с mmproj сама выполняет vision
            llm.configure(llm_base=f"http://127.0.0.1:{p}/v1", llm_key="local",
                          answer_model=req.name, extract_model=req.name,
                          vision_model=req.name)
        else:
            llm.configure(embed_base=f"http://127.0.0.1:{p}/v1",
                          embed_key="local", embed_model=req.name)
    return {"ok": ok, "message": msg}


class SlotStop(BaseModel):
    slot: str


@app.post("/api/slots/stop")
def api_slot_stop(req: SlotStop):
    reg = localllm.load_registry()
    if req.slot not in reg["slots"]:
        return {"ok": False, "message": "нет такого слота"}
    audit("slot_stop", req.slot)
    return {"ok": True, "message": localllm.stop_port(reg["slots"][req.slot]["port"])}


class ModelAction(BaseModel):
    name: str
    action: str  # start | stop | activate | use_embed | delete


@app.post("/api/models/action")
def api_model_action(req: ModelAction):
    reg = localllm.load_registry()
    m = localllm.find_model(reg, req.name)
    if not m:
        return {"ok": False, "message": "модель не найдена"}
    audit(f"model_{req.action}", req.name)
    if req.action == "start":
        ok, msg = localllm.start_model(reg, m)
        return {"ok": ok, "message": msg}
    if req.action == "stop":
        return {"ok": True, "message": localllm.stop_model(m)}
    if req.action == "activate":
        llm.configure(llm_base=f"http://127.0.0.1:{m['port']}/v1", llm_key="local",
                      answer_model=m["name"], extract_model=m["name"])
        return {"ok": True, "message": f"чат → {m['name']}"}
    if req.action == "use_embed":
        llm.configure(embed_base=f"http://127.0.0.1:{m['port']}/v1",
                      embed_key="local", embed_model=m["name"])
        return {"ok": True, "message": f"эмбеддинги → {m['name']}"}
    if req.action == "delete":
        reg["local_models"] = [x for x in reg["local_models"] if x["name"] != req.name]
        localllm.save_registry(reg)
        return {"ok": True, "message": "удалена"}
    return {"ok": False, "message": "неизвестное действие"}


class ModelUpsert(BaseModel):
    entry: dict


@app.post("/api/models/upsert")
def api_model_upsert(req: ModelUpsert):
    reg = localllm.load_registry()
    name = req.entry.get("name")
    if not name or not req.entry.get("model_path"):
        return {"ok": False, "message": "нужны name и model_path"}
    existing = localllm.find_model(reg, name)
    if existing:
        existing.update(req.entry)
    else:
        reg["local_models"].append(req.entry)
    localllm.save_registry(reg)
    audit("model_upsert", name)
    return {"ok": True, "message": "сохранено",
            "command": " ".join(localllm.build_command(reg, localllm.find_model(reg, name)))}


class ApiConnAction(BaseModel):
    name: str
    action: str  # activate | delete
    scope: str = "both"  # chat | embedding | both — какой слот переводим в облако


@app.post("/api/connections/action")
def api_conn_action(req: ApiConnAction):
    reg = localllm.load_registry()
    a = next((x for x in reg["api_connections"] if x["name"] == req.name), None)
    if not a:
        return {"ok": False, "message": "подключение не найдено"}
    audit(f"api_{req.action}", f"{req.name} [{req.scope}]")
    if req.action == "activate":
        stopped = False
        if req.scope in ("chat", "both"):
            # слот уходит в облако — гасим локальный сервер на его порту (VRAM)
            p = reg["slots"]["chat"]["port"]
            if localllm.is_running(p):
                localllm.stop_port(p)
                stopped = True
            llm.configure(llm_base=a["base_url"], llm_key=a.get("api_key") or None,
                          extract_model=a["extract_model"], answer_model=a["answer_model"],
                          vision_model=a.get("vision_model") or "google/gemini-3.1-flash-lite")
        if req.scope in ("embedding", "both"):
            p = reg["slots"]["embedding"]["port"]
            if localllm.is_running(p):
                localllm.stop_port(p)
                stopped = True
            llm.configure(embed_base=a["base_url"], embed_key=a.get("api_key") or None,
                          embed_model=a["embed_model"])
        msg = f"онлайн: {a['name']}"
        if stopped:
            msg += " · локальная модель остановлена"
        return {"ok": True, "message": msg}
    if req.action == "delete":
        reg["api_connections"] = [x for x in reg["api_connections"] if x["name"] != req.name]
        localllm.save_registry(reg)
        return {"ok": True, "message": "удалено"}
    return {"ok": False, "message": "неизвестное действие"}


class ApiConnAdd(BaseModel):
    entry: dict


@app.post("/api/connections/add")
def api_conn_add(req: ApiConnAdd):
    reg = localllm.load_registry()
    reg["api_connections"].append(req.entry)
    localllm.save_registry(reg)
    audit("api_add", req.entry.get("name", ""))
    return {"ok": True}


class SettingsUpdate(BaseModel):
    llama_server: str | None = None
    default_launch: dict | None = None


@app.post("/api/settings")
def api_settings(req: SettingsUpdate):
    reg = localllm.load_registry()
    if req.llama_server:
        reg["llama_server"] = req.llama_server
    if req.default_launch:
        reg["default_launch"] = req.default_launch
    localllm.save_registry(reg)
    audit("settings", "")
    return {"ok": True}


@app.get("/api/gaps")
def api_gaps():
    audit("gaps", "")
    return {"gaps": find_gaps()}


# ---------- Данные: загрузка и индексация ----------

@app.post("/api/data/upload")
def api_data_upload(files: list[UploadFile] = File(...), index_images: bool = Form(False)):
    payload = [(f.filename or "file", f.file.read()) for f in files]
    audit("data_upload", (", ".join(n for n, _ in payload) +
                          (" [+изображения]" if index_images else ""))[:200])
    ok, msg = dataops.start_pipeline(payload, index_images=index_images)
    return {"ok": ok, "message": msg}


@app.get("/api/data/status")
def api_data_status():
    return dataops.STATUS


@app.get("/api/data/stats")
def api_data_stats():
    return dataops.corpus_stats()


@app.post("/api/data/clear")
def api_data_clear():
    audit("data_clear", "полная очистка проиндексированных данных")
    ok, msg = dataops.clear_all()
    return {"ok": ok, "message": msg}


# ---------- Аудит ----------

@app.get("/api/audit")
def api_audit():
    rows = []
    if AUDIT_LOG.exists():
        for line in AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()[-100:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return {"ok": True, "rows": list(reversed(rows))}


# ---------- Статика ----------

@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")

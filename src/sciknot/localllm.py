"""Менеджер локальных моделей llama.cpp: реестр, конфиги запуска, старт/стоп/статус.

Реестр models.json (в корне проекта, вне git):
- llama_server: путь к llama-server.exe (хранится путь, файлы не копируются)
- default_launch: общий конфиг запуска — подкладывается в новые модели
- local_models: [{name, model_path, mmproj_path, port, role: chat|embedding, launch: {...}}]
- api_connections: [{name, base_url, api_key, extract_model, answer_model, embed_model}]

Для multi-part GGUF (…-00001-of-00003.gguf) указывается первый файл — llama.cpp
подхватывает остальные сам.
"""

import json
import shlex
import subprocess
import time
from pathlib import Path

import httpx

from sciknot.config import ROOT

REGISTRY_PATH = ROOT / "models.json"
PID_DIR = ROOT / "data_processed"

DEFAULT_LAUNCH = {
    "ctx": 16384,
    "ngl": 99,
    "n_cpu_moe": None,          # для MoE: сколько слоёв экспертов держать на CPU
    "flash_attn": "on",
    "reasoning": "off",         # off | auto | on — для нашего пайплайна off
    "ub": 2048,
    "b": 4096,
    "threads": 16,
    "temp": None,               # None = дефолт сервера
    "extra": "",                # любые дополнительные флаги строкой (--mlock --cache-reuse 256 ...)
}

_DEFAULT_REGISTRY = {
    "llama_server": r"E:\Program Files\llama.cpp\llama-server.exe",
    "default_launch": DEFAULT_LAUNCH,
    "local_models": [
        {
            "name": "qwen3.5-4b",
            "model_path": r"E:\AI models\Qwen3.5 Little\Qwen3.5-4B-Q5_K_M.gguf",
            "mmproj_path": None,
            "port": 8086,
            "role": "chat",
            "launch": {**DEFAULT_LAUNCH, "ngl": 20},
        },
        {
            "name": "bge-m3",
            "model_path": r"E:\AI models\bge-m3\bge-m3-Q8_0.gguf",
            "mmproj_path": None,
            "port": 8087,
            "role": "embedding",
            "launch": {**DEFAULT_LAUNCH, "ctx": 8192, "ngl": 0, "ub": 4096},
        },
    ],
    "api_connections": [
        {
            "name": "routerai (DeepSeek V4)",
            "base_url": "https://routerai.ru/api/v1",
            "api_key": "",  # пусто = взять из .env
            "extract_model": "deepseek/deepseek-v4-flash",
            "answer_model": "deepseek/deepseek-v4-pro",
            "embed_model": "baai/bge-m3",
            "vision_model": "google/gemini-3.1-flash-lite",
        }
    ],
}


DEFAULT_SLOTS = {
    "chat": {"port": 8086, "active_model": None},
    "embedding": {"port": 8087, "active_model": None},
}


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        save_registry(_DEFAULT_REGISTRY)
        reg = json.loads(json.dumps(_DEFAULT_REGISTRY))
    else:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    # миграция: слоты (LLM/эмбеддинг c фиксированными портами)
    if "slots" not in reg:
        reg["slots"] = json.loads(json.dumps(DEFAULT_SLOTS))
        for m in reg["local_models"]:
            slot = "embedding" if m.get("role") == "embedding" else "chat"
            if reg["slots"][slot]["active_model"] is None:
                reg["slots"][slot]["active_model"] = m["name"]
        save_registry(reg)
    return reg


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def find_model(reg: dict, name: str) -> dict | None:
    return next((m for m in reg["local_models"] if m["name"] == name), None)


def build_command(reg: dict, entry: dict) -> list[str]:
    """Собирает команду llama-server из default_launch + per-model launch."""
    cfg = {**reg.get("default_launch", DEFAULT_LAUNCH), **(entry.get("launch") or {})}
    cmd = [
        reg["llama_server"],
        "-m", entry["model_path"],
        "--alias", entry["name"],
        "--host", "127.0.0.1",
        "--port", str(entry["port"]),
        "-c", str(cfg["ctx"]),
        "-ngl", str(cfg["ngl"]),
        "-ub", str(cfg["ub"]),
        "-b", str(cfg["b"]),
        "-t", str(cfg["threads"]),
    ]
    if entry.get("role") == "embedding":
        cmd += ["--embedding", "--pooling", "cls"]
    else:
        cmd += ["--jinja", "--reasoning", str(cfg.get("reasoning") or "off")]
        if entry.get("mmproj_path"):
            cmd += ["--mmproj", entry["mmproj_path"]]
    if cfg.get("flash_attn"):
        cmd += ["-fa", str(cfg["flash_attn"])]
    if cfg.get("n_cpu_moe") is not None:
        cmd += ["--n-cpu-moe", str(cfg["n_cpu_moe"])]
    if cfg.get("temp") is not None:
        cmd += ["--temp", str(cfg["temp"])]
    if cfg.get("extra"):
        cmd += shlex.split(str(cfg["extra"]))
    return cmd


def is_running(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2, trust_env=False)
        return r.status_code == 200
    except Exception:
        return False


def _pid_file(port: int) -> Path:
    return PID_DIR / f"llama_{port}.pid"


def start_model(reg: dict, entry: dict, wait_s: int = 300) -> tuple[bool, str]:
    """Запуск llama-server отдельным процессом. Возвращает (ok, message)."""
    port = entry["port"]
    if is_running(port):
        return True, f"уже запущен (порт {port})"
    if not Path(reg["llama_server"]).exists():
        return False, f"не найден llama-server: {reg['llama_server']}"
    if not Path(entry["model_path"]).exists():
        return False, f"не найден GGUF: {entry['model_path']}"

    PID_DIR.mkdir(exist_ok=True)
    log_path = PID_DIR / f"llama_{port}.log"
    cmd = build_command(reg, entry)
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=DETACHED)
    _pid_file(port).write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_running(port):
            return True, f"запущен (порт {port}, pid {proc.pid})"
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            return False, f"процесс завершился при старте: …{tail}"
        time.sleep(2)
    return False, f"не дождались /health за {wait_s} c (большая модель может грузиться дольше — проверьте {log_path.name})"


def running_alias(port: int) -> str | None:
    """Какая модель (alias) реально отвечает на порту."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=2, trust_env=False)
        return r.json()["data"][0]["id"]
    except Exception:
        return None


def activate_slot(reg: dict, slot_name: str, model_name: str, port: int | None = None) -> tuple[bool, str]:
    """Слот = один порт, одна активная модель. Смена модели: стоп текущей, старт выбранной."""
    if slot_name not in reg["slots"]:
        return False, f"нет слота {slot_name}"
    entry = find_model(reg, model_name)
    if not entry:
        return False, f"модель «{model_name}» не найдена в реестре"
    slot = reg["slots"][slot_name]
    if port:
        if slot["port"] != int(port) and is_running(slot["port"]):
            stop_port(slot["port"])  # порт слота меняется — гасим сервер на старом
        slot["port"] = int(port)
    p = slot["port"]
    alias = running_alias(p)
    if alias and alias != model_name:
        stop_port(p)
    slot["active_model"] = model_name
    save_registry(reg)
    if not is_running(p):
        ok, msg = start_model(reg, {**entry, "port": p})
        if not ok:
            return False, msg
        return True, f"«{model_name}» запущена на порту {p}"
    return True, f"«{model_name}» уже работает на порту {p}"


def stop_port(port: int) -> str:
    pf = _pid_file(port)
    pid = pf.read_text(encoding="utf-8").strip() if pf.exists() else None
    if pid:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        pf.unlink(missing_ok=True)
        return f"остановлен (pid {pid})"
    # pid-файла нет — ищем процесс по порту
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
        capture_output=True, text=True,
    )
    found = ps.stdout.strip().splitlines()
    if found and found[0].strip().isdigit():
        subprocess.run(["taskkill", "/PID", found[0].strip(), "/F"], capture_output=True)
        return f"остановлен (pid {found[0].strip()}, найден по порту)"
    return "не запущен"


def stop_model(entry: dict) -> str:
    return stop_port(entry["port"])


def ensure_running(reg: dict, entry: dict) -> tuple[bool, str]:
    """5.3.1: использовать если запущен, запустить если нет."""
    if is_running(entry["port"]):
        return True, "работает"
    return start_model(reg, entry)


def entry_for_base_url(reg: dict, base_url: str) -> dict | None:
    """Находит локальную модель по активному base_url вида http://127.0.0.1:PORT/v1."""
    # слоты — основной путь: активная модель слота на порту слота
    for slot in reg.get("slots", {}).values():
        if f":{slot['port']}/" in base_url or base_url.endswith(f":{slot['port']}"):
            m = find_model(reg, slot["active_model"]) if slot.get("active_model") else None
            return {**m, "port": slot["port"]} if m else None
    for m in reg["local_models"]:
        if f":{m['port']}/" in base_url or base_url.endswith(f":{m['port']}"):
            return m
    return None

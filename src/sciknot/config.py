import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


# LLM_*/EMBED_* позволяют развести чат и эмбеддинги по разным серверам
# (например, локальные llama.cpp на :8080 и :8081). По умолчанию — один эндпоинт.
_llm_base = os.getenv("LLM_BASE_URL") or os.getenv("ROUTERAI_BASE_URL", "https://routerai.ru/api/v1")
_llm_key = os.getenv("LLM_API_KEY") or os.getenv("ROUTERAI_API_KEY", "")


@dataclass(frozen=True)
class Settings:
    api_base: str = _llm_base
    api_key: str = _llm_key
    embed_base: str = os.getenv("EMBED_BASE_URL") or _llm_base
    embed_key: str = os.getenv("EMBED_API_KEY") or _llm_key
    extract_model: str = os.getenv("EXTRACT_MODEL", "deepseek/deepseek-v4-flash")
    answer_model: str = os.getenv("ANSWER_MODEL", "deepseek/deepseek-v4-pro")
    embed_model: str = os.getenv("EMBED_MODEL", "baai/bge-m3")
    # vision-модель для облачного режима (DeepSeek не мультимодален) — аналог mmproj у локальных
    vision_model: str = os.getenv("VISION_MODEL", "google/gemini-3.1-flash-lite")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "sciknot-pass")
    data_dir: Path = field(
        default_factory=lambda: ROOT / "data" / "Задача 2. Научный клубок" / "Источники информации"
    )
    processed_dir: Path = field(default_factory=lambda: ROOT / "data_processed")


settings = Settings()

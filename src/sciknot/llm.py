import json
import re

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from sciknot.config import settings

# Рантайм-конфигурация: стартует из .env, может быть изменена на лету (настройки в UI)
_runtime = {
    "llm_base": settings.api_base,
    "llm_key": settings.api_key or "local",
    "embed_base": settings.embed_base,
    "embed_key": settings.embed_key or "local",
    "extract_model": settings.extract_model,
    "answer_model": settings.answer_model,
    "embed_model": settings.embed_model,
    "vision_model": settings.vision_model,
}

_client: OpenAI
_embed_client: OpenAI


def _make_clients() -> None:
    global _client, _embed_client
    _client = OpenAI(base_url=_runtime["llm_base"], api_key=_runtime["llm_key"] or "local", timeout=180)
    # эмбеддинги могут жить на отдельном сервере (локальный llama.cpp --embedding)
    _embed_client = (
        _client
        if _runtime["embed_base"] == _runtime["llm_base"]
        else OpenAI(base_url=_runtime["embed_base"], api_key=_runtime["embed_key"] or "local", timeout=180)
    )


_make_clients()


def configure(**kwargs) -> None:
    """Смена эндпоинтов/моделей на лету. Пустые значения игнорируются."""
    for k, v in kwargs.items():
        if k in _runtime and v:
            _runtime[k] = v
    # возврат на эндпоинт из .env без явного ключа = восстановить ключ из .env
    if _runtime["llm_base"] == settings.api_base and not kwargs.get("llm_key"):
        _runtime["llm_key"] = settings.api_key or "local"
    if _runtime["embed_base"] == settings.embed_base and not kwargs.get("embed_key"):
        _runtime["embed_key"] = settings.embed_key or "local"
    _make_clients()


def current_config() -> dict:
    return dict(_runtime)


def answer_model() -> str:
    return _runtime["answer_model"]


def vision_model() -> str:
    return _runtime["vision_model"]


def _parse_json(text: str) -> dict:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json-блок."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Ответ без JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
def chat_json(system: str, user: str, model: str | None = None, max_tokens: int = 4000) -> dict:
    """Один вызов chat completion с JSON-ответом. Ретраи на сетевые/парсинг-ошибки."""
    resp = _client.chat.completions.create(
        model=model or _runtime["extract_model"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content or ""
    return _parse_json(content)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
def chat_text(system: str, user: str, model: str | None = None, max_tokens: int = 4000) -> str:
    resp = _client.chat.completions.create(
        model=model or _runtime["answer_model"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


def chat_text_stream(system: str, user: str, model: str | None = None, max_tokens: int = 6000):
    """Потоковая генерация. Закрытие генератора (кнопка «Стоп») обрывает HTTP-стрим к модели."""
    stream = _client.chat.completions.create(
        model=model or _runtime["answer_model"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        stream=True,
    )
    try:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    finally:
        stream.close()


def chat_vision(system: str, user: str, image_bytes: bytes, mime: str = "image/png",
                model: str | None = None, max_tokens: int = 2000) -> str:
    """Запрос с изображением (нужна vision-модель: локальная с mmproj или облачная VLM)."""
    import base64

    data_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    resp = _client.chat.completions.create(
        model=model or _runtime["vision_model"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30))
def embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    resp = _embed_client.embeddings.create(model=model or _runtime["embed_model"], input=texts)
    return [item.embedding for item in resp.data]

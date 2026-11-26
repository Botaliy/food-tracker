"""Photo (and/or a short note) -> what's on the plate, with portion weights.

One Messages API call with a JSON schema for the answer. The model's job is
to name the foods and judge grams; its per-100 g numbers are only a guess the
food database overrides (see app.foods).
"""

import base64
import json
import logging
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings

logger = logging.getLogger(__name__)


class RecognizedItem(BaseModel):
    name_ru: str
    name_en: str
    grams: float = Field(ge=0)
    kcal_100: float = Field(ge=0)
    protein_100: float = Field(ge=0)
    fat_100: float = Field(ge=0)
    carbs_100: float = Field(ge=0)
    confidence: str


class Recognition(BaseModel):
    title: str
    items: list[RecognizedItem]
    comment: str


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Короткое название приёма пищи по-русски, например «Курица с рисом»."},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name_ru": {"type": "string", "description": "Название продукта по-русски, коротко."},
                    "name_en": {
                        "type": "string",
                        "description": "Generic English name in the wording of the USDA SR Legacy database, with the cooking state: "
                        "'chicken broilers breast meat only roasted', 'rice white long-grain cooked', "
                        "'buckwheat groats roasted cooked', 'egg whole hard-boiled'. No brand names here.",
                    },
                    "grams": {"type": "number", "description": "Estimated weight of this portion in grams, as eaten."},
                    "kcal_100": {"type": "number", "description": "Your estimate of kcal per 100 g."},
                    "protein_100": {"type": "number", "description": "Protein, g per 100 g."},
                    "fat_100": {"type": "number", "description": "Fat, g per 100 g."},
                    "carbs_100": {"type": "number", "description": "Carbohydrates, g per 100 g."},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["name_ru", "name_en", "grams", "kcal_100", "protein_100", "fat_100", "carbs_100", "confidence"],
                "additionalProperties": False,
            },
        },
        "comment": {
            "type": "string",
            "description": "One short sentence in Russian on what is uncertain (hidden oil, sauce, portion size), or empty.",
        },
    },
    "required": ["title", "items", "comment"],
    "additionalProperties": False,
}

SYSTEM = """You estimate what a person is about to eat, for a calorie and macro tracker.

Input: a photo of a meal, a note from the user, or both. Report every food component with its weight in grams.

Portions:
- Judge size from references in the photo: a dinner plate is about 26 cm across, a fork about 19 cm, a tablespoon holds ~15 g of liquid, a slice of bread is ~30 g.
- Weigh food as eaten (cooked rice, cooked pasta), not dry.
- Include what is easy to miss: cooking oil on fried food, sauce, dressing, butter, sugar in drinks. A visible glossy fried dish usually carries 5-15 g of oil.
- If the user's note gives a weight or a count ("200 г", "2 яйца"), use it exactly.

Components:
- Split a plate into its parts when they are visible separately (chicken, rice, salad).
- Keep a dish whole when its parts are mixed (borscht, plov, lasagna, a sandwich) and estimate the dish as a whole.
- Packaged products: if a label or brand is readable, name it and use its numbers.

Numbers per 100 g are your best estimate; they are checked against a nutrition database.
Return no items if there is no food. Be honest about confidence: "low" when the portion or the food itself is unclear."""


class RecognizeError(Exception):
    """A message for the user, in Russian."""


@dataclass
class RecognizeResult:
    recognition: Recognition
    response: object = field(repr=False)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key or None, max_retries=2, timeout=90)


def recognize(image_jpeg: bytes | None, note: str, client: anthropic.Anthropic | None = None) -> RecognizeResult:
    """Call the model. The raw response is returned too, for usage accounting."""
    if image_jpeg is None and not note.strip():
        raise RecognizeError("Нужно фото или описание еды")

    settings = get_settings()
    content: list[dict] = []
    if image_jpeg is not None:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_jpeg).decode()},
        })
    content.append({"type": "text", "text": f"Заметка пользователя: {note.strip()}" if note.strip() else "Что на фото?"})

    kwargs: dict = {
        "model": settings.recognize_model,
        "max_tokens": 8000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": settings.recognize_effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    }
    # Server-side fallback reroutes a false-positive safety decline; Haiku has none.
    if not settings.recognize_model.startswith("claude-haiku"):
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    client = client or _client()
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.RateLimitError as exc:
        raise RecognizeError("Сервис распознавания перегружен, попробуй через минуту") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Claude API error %s: %s", exc.status_code, exc.message)
        raise RecognizeError("Не удалось распознать: ошибка сервиса") from exc
    except anthropic.APIConnectionError as exc:
        raise RecognizeError("Нет связи с сервисом распознавания") from exc

    if response.stop_reason == "refusal":
        raise RecognizeErrorWithResponse("Модель отказалась разбирать это фото", response)
    if response.stop_reason == "max_tokens":
        raise RecognizeErrorWithResponse("Ответ модели оборвался, попробуй ещё раз", response)

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        recognition = Recognition.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error("Unparseable recognition: %r", text[:500])
        raise RecognizeErrorWithResponse("Не удалось разобрать ответ модели", response) from exc
    return RecognizeResult(recognition=recognition, response=response)


class RecognizeErrorWithResponse(RecognizeError):
    """The call went through and was billed, but gave nothing usable."""

    def __init__(self, message: str, response) -> None:
        super().__init__(message)
        self.response = response

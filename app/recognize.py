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
    # Required in SCHEMA; optional here so an answer without them still parses.
    fiber_100: float | None = Field(None, ge=0)
    sugar_100: float | None = Field(None, ge=0)
    salt_100: float | None = Field(None, ge=0)
    package_grams: float | None = Field(None, ge=0)  # one pack of a packaged product; 0 / None: not packaged
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
                    "fiber_100": {"type": "number", "description": "Dietary fibre, g per 100 g."},
                    "sugar_100": {"type": "number", "description": "Total sugars (natural and added), g per 100 g."},
                    "salt_100": {"type": "number", "description": "Salt (sodium x 2.5), g per 100 g."},
                    "package_grams": {
                        "type": "number",
                        "description": "Net weight or volume of ONE pack (can, bottle, bar, cup) in g or ml when this is a "
                        "packaged product or a drink in its container; 0 for anything else.",
                    },
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["name_ru", "name_en", "grams", "kcal_100", "protein_100", "fat_100", "carbs_100",
                             "fiber_100", "sugar_100", "salt_100", "package_grams", "confidence"],
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

Packs (a can, a bottle, a bar, a yogurt cup, a bag of crisps):
- Don't judge a pack by eye. Read the net weight or volume off the label ("0,5 л" is 500, "330 ml" is 330); count 1 ml as 1 g.
- If it isn't readable, take the usual size of that container: a beer can is 500 (a small one 330), a soda can 330, a chocolate bar 90, a yogurt cup 125.
- Put the size of one pack in package_grams, and in grams that size times the number of packs in the photo: a pack is eaten whole unless the note says otherwise or it is plainly part-empty.

Numbers per 100 g are your best estimate; they are checked against a nutrition database.
Also estimate fibre, total sugars and salt per 100 g (salt, not sodium; include salt added in cooking for home-made dishes).
Return no items if there is no food. Be honest about confidence: "low" when the portion or the food itself is unclear."""


class RecognizeError(Exception):
    """A message for the user, in Russian."""


@dataclass
class RecognizeResult:
    recognition: Recognition
    response: object = field(repr=False)


# ---------- recipe mode: raw ingredients of a whole dish ----------

class RecipeRecognition(BaseModel):
    title: str
    items: list[RecognizedItem]
    cooked_grams: float | None = Field(None, ge=0)  # 0 / None: unknown, the sum of the ingredients is used
    comment: str


def _recipe_schema() -> dict:
    item = json.loads(json.dumps(SCHEMA["properties"]["items"]["items"]))
    props = item["properties"]
    props["name_ru"]["description"] = "Ingredient name in Russian, short, as the user would say it: «Морковь», «Рис круглый»."
    props["name_en"]["description"] = (
        "Generic English name of the RAW ingredient in the wording of the USDA SR Legacy database: "
        "'carrots raw', 'rice white short-grain raw', 'beef chuck raw', 'oil sunflower'. No brand names.")
    props["grams"]["description"] = "Weight of this raw ingredient for the whole dish, in grams, before cooking."
    del props["package_grams"]  # an ingredient is weighed, not counted in packs
    item["required"].remove("package_grams")
    for key in ("kcal_100", "protein_100", "fat_100", "carbs_100", "fiber_100", "sugar_100", "salt_100"):
        props[key]["description"] += " Of the raw ingredient."
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Название блюда по-русски, коротко: «Борщ», «Плов с курицей»."},
            "items": {"type": "array", "items": item},
            "cooked_grams": {
                "type": "number",
                "description": "Estimated weight of the whole finished dish in grams, or 0 if there is nothing to go on.",
            },
            "comment": {
                "type": "string",
                "description": "One or two short sentences in Russian: what was assumed (oil, salt, a weight converted "
                               "from pieces or spoons) and what is uncertain; or empty.",
            },
        },
        "required": ["title", "items", "cooked_grams", "comment"],
        "additionalProperties": False,
    }


RECIPE_SCHEMA = _recipe_schema()

RECIPE_SYSTEM = """You turn a home recipe into a list of raw ingredients, for a calorie tracker that computes the dish from them.

Input: the user's description of a dish, a photo (ingredients laid out, a written or printed recipe, a screenshot), or both.

Ingredients:
- List every ingredient of the WHOLE dish (the whole pot or tray, not one portion), RAW, as bought, with its weight in grams.
- Convert counts and kitchen measures to grams: a medium carrot ~70 g, a medium onion ~90 g, a medium potato ~120 g, an egg ~55 g without shell, a clove of garlic ~5 g, a glass (200 ml) of rice ~180 g, of buckwheat ~160 g, of flour ~130 g, of sugar ~180 g; a tablespoon of oil ~15 g, of sugar ~20 g, of flour ~10 g; a teaspoon of salt ~7 g, of sugar ~6 g.
- If a weight or volume is given, use it exactly. Liquids: 1 ml ~ 1 g.
- Never invent ingredients that are neither mentioned nor visible. The one exception: cooking oil or salt the dish clearly needs (frying, a soup) may be added with a modest amount, and then say so in the comment.
- Water: list it only when its amount is stated (0 kcal); it still counts for the cooked weight.

Cooked weight (cooked_grams), the finished dish as it would sit on the scale:
- Grains and pasta absorb water: rice roughly triples its dry weight, buckwheat ~2.5x, pasta ~2.2x.
- Soups and stews: the ingredients plus the stated water, minus ~10-15% boiled off.
- Roasting, frying and baking lose water: meat ~25-35% lighter, vegetables more.
- If there is nothing to judge by, return 0.

Per-100 g numbers are for the raw ingredient, your best estimate; they are checked against a nutrition database.
Return no items if there is no recipe or food in the input. Be honest about confidence."""


@dataclass
class RecipeResult:
    recipe: RecipeRecognition
    response: object = field(repr=False)


def _client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key
    # Without a key the SDK fails with a bare TypeError on the first request.
    if not key:
        logger.error("ANTHROPIC_API_KEY is not set")
        raise RecognizeError("Сервис ИИ не настроен: нет ключа API")
    return anthropic.Anthropic(api_key=key, max_retries=2, timeout=90)


def recognize(image_jpeg: bytes | None, note: str, hints: list[str] | tuple = (),
              client: anthropic.Anthropic | None = None) -> RecognizeResult:
    """Call the model. `hints` are the user's usual corrections (app.corrections).
    The raw response is returned too, for usage accounting."""
    if image_jpeg is None and not note.strip():
        raise RecognizeError("Нужно фото или описание еды")

    text = f"Заметка пользователя: {note.strip()}" if note.strip() else "Что на фото?"
    if hints:
        text += ("\n\nПрошлые правки этого пользователя (учитывай, если подходит к этой тарелке; фото и заметка важнее):\n"
                 + "\n".join(f"- {h}" for h in hints))
    response, data = _call(image_jpeg, text, SYSTEM, SCHEMA, client)
    try:
        recognition = Recognition.model_validate(data)
    except ValidationError as exc:
        logger.error("Unparseable recognition: %r", str(data)[:500])
        raise RecognizeErrorWithResponse("Не удалось разобрать ответ модели", response) from exc
    return RecognizeResult(recognition=recognition, response=response)


def recognize_recipe(image_jpeg: bytes | None, note: str, client: anthropic.Anthropic | None = None) -> RecipeResult:
    """The same call in its second mode: a whole dish's raw ingredients and its cooked weight."""
    if image_jpeg is None and not note.strip():
        raise RecognizeError("Нужно фото или описание рецепта")
    text = f"Рецепт от пользователя: {note.strip()}" if note.strip() else "Какой рецепт на фото?"
    response, data = _call(image_jpeg, text, RECIPE_SYSTEM, RECIPE_SCHEMA, client)
    try:
        recipe = RecipeRecognition.model_validate(data)
    except ValidationError as exc:
        logger.error("Unparseable recipe: %r", str(data)[:500])
        raise RecognizeErrorWithResponse("Не удалось разобрать ответ модели", response) from exc
    return RecipeResult(recipe=recipe, response=response)


def _call(image_jpeg: bytes | None, text: str, system: str, schema: dict,
          client: anthropic.Anthropic | None) -> tuple[object, object]:
    """One Messages API call with a JSON schema: the raw response and the parsed JSON."""
    settings = get_settings()
    content: list[dict] = []
    if image_jpeg is not None:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_jpeg).decode()},
        })
    content.append({"type": "text", "text": text})

    kwargs: dict = {
        "model": settings.recognize_model,
        "max_tokens": 8000,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": settings.recognize_effort, "format": {"type": "json_schema", "schema": schema}},
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
        return response, json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Unparseable answer: %r", text[:500])
        raise RecognizeErrorWithResponse("Не удалось разобрать ответ модели", response) from exc


class RecognizeErrorWithResponse(RecognizeError):
    """The call went through and was billed, but gave nothing usable."""

    def __init__(self, message: str, response) -> None:
        super().__init__(message)
        self.response = response

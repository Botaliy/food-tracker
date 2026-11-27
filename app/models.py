from datetime import UTC, datetime
from datetime import date as date_

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # None: settings.default_daily_limit_usd applies.
    daily_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64))
    kcal_goal: Mapped[int] = mapped_column(Integer, default=2000)
    protein_goal: Mapped[int] = mapped_column(Integer, default=120)
    fat_goal: Mapped[int] = mapped_column(Integer, default=70)
    carbs_goal: Mapped[int] = mapped_column(Integer, default=220)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Food(Base):
    """Nutrition per 100 g. source: usda | off (Open Food Facts) | custom | recipe.

    A recipe is a user's own dish: its per-100 g values are computed from recipe_items
    (app.recipes) and, unlike custom foods, it is private to its owner."""

    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    name_ru: Mapped[str | None] = mapped_column(String(300), nullable=True)
    kcal: Mapped[float] = mapped_column(Float)
    protein: Mapped[float] = mapped_column(Float)
    fat: Mapped[float] = mapped_column(Float)
    carbs: Mapped[float] = mapped_column(Float)
    # g per 100 g; None = unknown (never read as zero). Salt, not sodium.
    fiber: Mapped[float | None] = mapped_column(Float, nullable=True)
    sugar: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Who added a custom or scanned food (shown first in their own search); None for USDA.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Recipes only. cooked_grams: the finished dish (None = the sum of its ingredients);
    # portion_grams: the usual helping, the default when logging it.
    cooked_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    portion_grams: Mapped[float | None] = mapped_column(Float, nullable=True)

    recipe_items: Mapped[list["RecipeItem"]] = relationship(
        foreign_keys="RecipeItem.food_id", cascade="all, delete-orphan", order_by="RecipeItem.id"
    )


class RecipeItem(Base):
    """One raw ingredient of a recipe, shaped like MealItem: per-100 g values are copied in,
    so the dish keeps its numbers if the ingredient's food changes or goes away."""

    __tablename__ = "recipe_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("foods.id", ondelete="CASCADE"), index=True)  # the dish
    name: Mapped[str] = mapped_column(String(300))
    grams: Mapped[float] = mapped_column(Float)
    ingredient_food_id: Mapped[int | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(16))
    kcal_100: Mapped[float] = mapped_column(Float)
    protein_100: Mapped[float] = mapped_column(Float)
    fat_100: Mapped[float] = mapped_column(Float)
    carbs_100: Mapped[float] = mapped_column(Float)
    fiber_100: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = unknown
    sugar_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt_100: Mapped[float | None] = mapped_column(Float, nullable=True)


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    eaten_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # File name of the thumbnail in <data dir>/photos (app.photos); None = no photo.
    photo: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    items: Mapped[list["MealItem"]] = relationship(
        back_populates="meal", cascade="all, delete-orphan", order_by="MealItem.id"
    )


class MealItem(Base):
    """One component of a meal. Per-100 g values are copied in, so later edits
    to the food database don't rewrite what was already eaten."""

    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    grams: Mapped[float] = mapped_column(Float)
    food_id: Mapped[int | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # usda | off | custom | recipe | estimate | manual
    kcal_100: Mapped[float] = mapped_column(Float)
    protein_100: Mapped[float] = mapped_column(Float)
    fat_100: Mapped[float] = mapped_column(Float)
    carbs_100: Mapped[float] = mapped_column(Float)
    fiber_100: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = unknown
    sugar_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    salt_100: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The model's own weight for a recognized item, to learn from the user's corrections.
    ai_grams: Mapped[float | None] = mapped_column(Float, nullable=True)

    meal: Mapped[Meal] = relationship(back_populates="items")


class Weight(Base):
    """One weigh-in per user per local calendar day; a second entry for the day replaces the first."""

    __tablename__ = "weights"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date_] = mapped_column(Date)
    kg: Mapped[float] = mapped_column(Float)


class Profile(Base):
    """What the goal calculator needs, plus the key the iPhone Shortcut sends Health data with."""

    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    sex: Mapped[str | None] = mapped_column(String(1), nullable=True)  # m | f
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity: Mapped[float] = mapped_column(Float, default=1.375)  # see app.energy.ACTIVITY_LEVELS
    rate_kg_week: Mapped[float] = mapped_column(Float, default=0.0)  # negative = losing
    goal_weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    # sha256 of the Shortcut's token: the token itself is shown once and never stored.
    health_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # Reminders (sent only to devices that subscribed to push).
    remind_evening: Mapped[bool] = mapped_column(Boolean, default=True)
    remind_hour: Mapped[int] = mapped_column(Integer, default=21)  # local hour
    weekly_review_push: Mapped[bool] = mapped_column(Boolean, default=True)
    share_streak: Mapped[bool] = mapped_column(Boolean, default=False)  # shown to the others who share theirs
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)  # went through (or skipped) the first-run steps
    track_water: Mapped[bool] = mapped_column(Boolean, default=True)
    water_goal_ml: Mapped[int] = mapped_column(Integer, default=2000)
    last_evening_push: Mapped[date_ | None] = mapped_column(Date, nullable=True)
    last_weekly_push: Mapped[date_ | None] = mapped_column(Date, nullable=True)


class PushSubscription(Base):
    """A browser's Web Push endpoint: one per installed app per device."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Activity(Base):
    """One day of movement from Apple Health, sent by a Shortcut. A resend for the day replaces it."""

    __tablename__ = "activity"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date_] = mapped_column(Date)
    active_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Workout(Base):
    """One workout the user confirmed from the coach chat (typed or read off a watch screenshot).
    Several per day; None = unknown. A watch's daily active_kcal already includes these."""

    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date_] = mapped_column(Date, index=True)  # the user's local day
    started: Mapped[str | None] = mapped_column(String(5), nullable=True)  # local "HH:MM"
    kind: Mapped[str] = mapped_column(String(40))  # free text: «Бег», «Силовая»
    minutes: Mapped[int] = mapped_column(Integer)
    kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Water(Base):
    """Water drunk on a local day, in ml."""

    __tablename__ = "water"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date_] = mapped_column(Date)
    ml: Mapped[int] = mapped_column(Integer, default=0)


class ChatMessage(Base):
    """One message in the chat with the coach."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # Assistant only: what the coach proposed to write, as JSON [{id, type, payload, status}],
    # status pending | done | declined. Nothing is written until the user accepts (app.routers.api).
    actions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # User only: a screenshot went with it. The image itself is never stored.
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Review(Base):
    """A weekly review by the model, kept so the same day never pays twice. content is ReviewOut as JSON."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day: Mapped[date_] = mapped_column(Date)  # the user's local date it was made on; covers the 7 days up to it
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyKey(Base):
    """A client's retry key for a quick write (Idempotency-Key header): a write that already
    went through isn't applied twice. ref_id: what the first request created (a meal's id)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ApiCall(Base):
    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

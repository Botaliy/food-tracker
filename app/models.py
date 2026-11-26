from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    """Nutrition per 100 g. source: usda | off (Open Food Facts) | custom."""

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
    # A custom food belongs to whoever added it; None for shared databases.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    eaten_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

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
    source: Mapped[str] = mapped_column(String(16))  # usda | off | custom | estimate
    kcal_100: Mapped[float] = mapped_column(Float)
    protein_100: Mapped[float] = mapped_column(Float)
    fat_100: Mapped[float] = mapped_column(Float)
    carbs_100: Mapped[float] = mapped_column(Float)

    meal: Mapped[Meal] = relationship(back_populates="items")


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

"""Facts about a user's diary that the API and the reminder scheduler both need."""

from collections.abc import Callable
from statistics import median
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import usage
from app.models import Meal, User

STREAK_LOOKBACK_DAYS = 400
QUALITY_LOOKBACK_DAYS = 120  # the "in goal" and "protein" runs: shorter, they need meal totals
FREEZE_EVERY_DAYS = 7        # one missed day per week doesn't break the logging streak
MILESTONES = (7, 14, 30, 50, 100, 200, 365)

# A day is "in goal" inside a corridor: a 200 kcal snack day isn't a success.
IN_GOAL_LOW = 0.85
IN_GOAL_HIGH = 1.05
PROTEIN_OK = 0.9


# Reference points, not goals the user sets: fibre scales with how much one eats
# (14 g per 1000 kcal), salt has a flat WHO ceiling. Total sugars get none: natural
# sugar in fruit and milk counts too, so a "limit" on them would mislead.
FIBER_PER_1000_KCAL = 14
SALT_LIMIT_G = 5


def nutrient_refs(kcal_goal: int) -> dict:
    return {"fiber": round(FIBER_PER_1000_KCAL * kcal_goal / 1000), "salt": SALT_LIMIT_G}


def today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def local_day(dt: datetime, tz: str) -> date:
    """A stored (naive UTC) time as the user's calendar day."""
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).date()


def in_goal(kcal: float, goal: float) -> bool:
    return goal > 0 and IN_GOAL_LOW * goal <= kcal <= IN_GOAL_HIGH * goal


def logged_days(db: Session, user: User, since: date) -> set[date]:
    start, _ = usage.day_bounds_utc(user.timezone, since)
    return {
        local_day(dt, user.timezone)
        for dt in db.scalars(select(Meal.eaten_at).where(Meal.user_id == user.id, Meal.eaten_at >= start))
    }


def day_totals(db: Session, user: User, since: date) -> dict[date, tuple[float, float]]:
    """kcal and protein per local day."""
    start, _ = usage.day_bounds_utc(user.timezone, since)
    out: dict[date, list[float]] = {}
    meals = db.scalars(
        select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start).options(selectinload(Meal.items))
    )
    for m in meals:
        t = out.setdefault(local_day(m.eaten_at, user.timezone), [0.0, 0.0])
        for i in m.items:
            t[0] += i.kcal_100 * i.grams / 100
            t[1] += i.protein_100 * i.grams / 100
    return {d: (k, p) for d, (k, p) in out.items()}


def run(ok: Callable[[date], bool], on: date, freeze_every: int | None = None,
        limit: int = STREAK_LOOKBACK_DAYS) -> tuple[int, list[date]]:
    """Length of the run of good days ending `on` (or the day before, if `on` isn't good
    yet: the day isn't over). With freezes, a single bad day between good ones is skipped,
    at most once per `freeze_every` days. Returns (good days counted, frozen days)."""
    day = on if ok(on) else on - timedelta(days=1)
    n, frozen = 0, []
    for _ in range(limit):
        if ok(day):
            n += 1
        elif (freeze_every and ok(day - timedelta(days=1))
              and (not frozen or (frozen[-1] - day).days >= freeze_every)):
            frozen.append(day)
        else:
            break
        day -= timedelta(days=1)
    return n, frozen


def streak(db: Session, user: User, on: date) -> tuple[int, bool]:
    """Days in a row with at least one meal, freezes included. Also: is `on` logged yet."""
    logged = logged_days(db, user, on - timedelta(days=STREAK_LOOKBACK_DAYS))
    n, _ = run(lambda d: d in logged, on, FREEZE_EVERY_DAYS)
    return n, on in logged


GUESS_DAYS = 7
GUESS_MIN = 3  # guesses in the week before the score is shown to the others


def guess_score(db: Session, user: User, on: date) -> dict | None:
    """«Угадай калории» over the week up to `on`: how many plates were guessed and the median
    miss in percent of what was finally logged. None without a guess."""
    start, _ = usage.day_bounds_utc(user.timezone, on - timedelta(days=GUESS_DAYS - 1))
    meals = db.scalars(select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.guess_kcal.is_not(None))
                       .options(selectinload(Meal.items))).all()
    misses = []
    for m in meals:
        kcal = sum(i.kcal_100 * i.grams / 100 for i in m.items)
        if kcal > 0:
            misses.append(abs(m.guess_kcal - kcal) / kcal)
    return {"n": len(misses), "off_percent": round(median(misses) * 100)} if misses else None


# «Дуэль недели»: one contest a week among those who share streaks, Monday to Sunday.
# The metric alternates by ISO week so the same person doesn't win on habit alone.
DUELS = (("in_goal", "дней в норме"), ("protein", "дней с белком"))


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def duel_metric(monday: date) -> tuple[str, str]:
    return DUELS[monday.isocalendar().week % len(DUELS)]


def duel_score(db: Session, user: User, monday: date, upto: date) -> int:
    """Good days of this week's metric from `monday` through `upto`."""
    metric, _ = duel_metric(monday)
    totals = day_totals(db, user, monday)
    n = 0
    for d, (kcal, protein) in totals.items():
        if not monday <= d <= upto:
            continue
        if metric == "in_goal":
            n += in_goal(kcal, user.kcal_goal)
        else:
            n += user.protein_goal > 0 and protein >= PROTEIN_OK * user.protein_goal
    return n


def duel(db: Session, users: list[User], monday: date, upto: date | None = None) -> dict:
    """The standing of the week starting on `monday`; `upto` cuts it short (default: the whole week,
    which for the week going on means each person's days so far)."""
    upto = upto or monday + timedelta(days=6)
    metric, title = duel_metric(monday)
    people = sorted(({"id": u.id, "name": u.username, "score": duel_score(db, u, monday, upto)} for u in users),
                    key=lambda p: (-p["score"], p["name"]))
    return {"metric": metric, "title": title, "from": monday.isoformat(), "people": people}


def reserve(profile, on: date) -> int:
    """Calories set aside for the evening of `on`."""
    if profile is None or profile.reserve_day != on or not profile.reserve_kcal:
        return 0
    return profile.reserve_kcal


def streaks(db: Session, user: User, on: date) -> dict:
    """Everything the day screen shows about runs: logging (with its freeze), in goal, protein."""
    logged = logged_days(db, user, on - timedelta(days=STREAK_LOOKBACK_DAYS))
    days, frozen = run(lambda d: d in logged, on, FREEZE_EVERY_DAYS)
    recent_freeze = next((d for d in frozen if (on - d).days < FREEZE_EVERY_DAYS), None)
    totals = day_totals(db, user, on - timedelta(days=QUALITY_LOOKBACK_DAYS))
    goal_days, _ = run(lambda d: d in totals and in_goal(totals[d][0], user.kcal_goal), on, limit=QUALITY_LOOKBACK_DAYS)
    protein_days, _ = run(
        lambda d: d in totals and user.protein_goal > 0 and totals[d][1] >= PROTEIN_OK * user.protein_goal,
        on, limit=QUALITY_LOOKBACK_DAYS)
    return {
        "days": days,
        "today": on in logged,
        "frozen": recent_freeze.isoformat() if recent_freeze else None,
        "freeze_ready": recent_freeze is None,
        "next_milestone": next((m for m in MILESTONES if m > days), None),
        "in_goal": goal_days,
        "protein": protein_days,
        "guess": guess_score(db, user, on),
    }

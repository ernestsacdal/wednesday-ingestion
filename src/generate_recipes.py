"""Half-Price Dinners — weekly recipe generation from this week's half-price set.

Reality the design had to bend to: raw dinner ingredients (mince, chicken,
fresh veg) almost never go half-price. What DOES is packaged meal-helpers —
recipe bases, jar sauces, meal kits, instant noodles. So a "dinner" here is
built on the HERO MODEL: one (or two) genuinely half-price hero products
(priced, half-marked, linked to their product page) plus a short "you'll also
need" staples line shown WITHOUT prices. The saving shown is the hero's real
saving. Nothing fabricated, nothing mis-priced.

One generation serves the whole (account-less) user base, so cost is flat
regardless of users. The model is handed this week's real half-price
meal-relevant products (name + id) and may only reference them by id; every
price/total is computed rule-based here, never by the LLM.

Two paths share validation + costing + writer:
  --seed       no LLM; composes a dinner per dish-type from real on-sale heroes
               (so the app renders immediately, before a Groq key exists).
  (default)    one Groq Llama 3.3 70B call; requires GROQ_API_KEY. Without the
               key the run logs a skip and exits 0 (never reddens the cron).

    python -m src.generate_recipes --seed --write-db --verbose
    python -m src.generate_recipes --write-db --verbose      # needs GROQ_API_KEY

Requires SUPABASE_DB_URL (+ GROQ_API_KEY for the LLM path).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
import requests

from src.env import load_dotenv
from src.scrapers.base import configure_logging

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"
_MIN_LLM_RECIPES = 4
_MIN_SEED_RECIPES = 2

# Names containing any of these are not dinner ingredients — drop before any
# composition (baby/pet food, toiletries, snacks, drinks).
_JUNK = (
    "month", "baby", "infant", "toddler", " cat ", "cat ", " dog", "puppy",
    "kitten", " pet ", "treat", "wipe", "sponge", "deodorant", "shampoo",
    "soap", "cleaner", "detergent", "laundr", "popcorn", " chip", "chip ",
    "lolli", "biscuit", "candy", "dishwash", "toilet", "tissue", "litter",
    "formula", "rusk", "teether", "cordial", "soft drink", "energy drink",
    "chocolate", "lip ", "razor", "vitamin", "supplement", "moistur",
    "shapes", "cracker", "crisp", "snack", "muesli bar", "chips",
)


def _is_junk(name: str) -> bool:
    n = name.lower()
    return any(j in n for j in _JUNK)


@dataclass
class Candidate:
    product_id: str
    name: str
    category: str
    retailer: str
    regular_cents: int
    sale_cents: int


@dataclass
class Recipe:
    title: str
    serves: int
    ingredients: list[dict]  # half-price HEROES: [{product_id, label}]
    pantry: list[str]        # "you'll also need" staples (unpriced)
    instructions: str
    tags: list[str] = field(default_factory=list)
    estimated_cost_cents: int = 0
    regular_cost_cents: int = 0


# Dinner types built around a half-price hero. `match` = name keywords that
# identify a hero of this type; `hero_noun` is used in the steps so they read
# cleanly regardless of the exact product; `staples` is the unpriced line.
_DISHES = [
    {
        "key": "curry", "title": "Easy curry night", "hero_noun": "curry base",
        "match": ["curry recipe base", "curry base", "butter chicken", "massaman",
                  "green curry", "red curry", "korma", "tikka", "rogan", "dutch curry"],
        "staples": ["your choice of protein (chicken, beef or chickpeas)", "steamed rice"],
        "steps": "1. Brown your protein in a little oil.\n"
                 "2. Stir through the curry base.\n"
                 "3. Simmer 10-15 minutes and serve over rice.",
        "tags": ["curry", "easy"],
    },
    {
        "key": "pasta_base", "title": "Creamy pasta night", "hero_noun": "pasta base",
        "match": ["pasta & sauce", "spaghetti bowl", "pasta bake", "mac & cheese",
                  "macc & chees", "pasta and sauce"],
        "staples": ["a splash of milk", "your choice of protein or vegetables"],
        "steps": "1. Prepare the pasta base as per the pack.\n"
                 "2. Stir in cooked chicken, bacon or vegetables.\n"
                 "3. Serve hot.",
        "tags": ["pasta", "quick"],
    },
    {
        "key": "pasta_sauce", "title": "Pasta night", "hero_noun": "sauce",
        "match": ["pasta sauce", "passata", "napoletana", "bolognese sauce",
                  "arrabbiata", "marinara"],
        "staples": ["a pack of pasta", "grated cheese"],
        "steps": "1. Cook the pasta and drain.\n"
                 "2. Warm the sauce (add mince or vegetables if you like).\n"
                 "3. Toss through the pasta and top with cheese.",
        "tags": ["pasta"],
    },
    {
        "key": "stir_fry", "title": "Stir-fry night", "hero_noun": "sauce",
        "match": ["stir fry", "stir-fry", "simmer sauce", "cook in sauce",
                  "hokkien", "pad thai sauce", "satay sauce", "teriyaki sauce"],
        "staples": ["your choice of protein", "mixed vegetables", "rice or noodles"],
        "steps": "1. Stir-fry your protein until golden.\n"
                 "2. Add vegetables and the sauce; toss 4-5 minutes.\n"
                 "3. Serve over rice or noodles.",
        "tags": ["stir-fry", "quick"],
    },
    {
        "key": "noodle", "title": "Loaded noodles", "hero_noun": "noodles",
        "match": ["ramen", "noodle soup", "cup noodle", "instant noodle", "laksa", "tom yum"],
        "staples": ["an egg", "a handful of greens"],
        "steps": "1. Cook the noodles as per the pack.\n"
                 "2. Top with a soft-boiled egg and some greens.\n"
                 "3. Serve straight away.",
        "tags": ["noodles", "quick"],
    },
    {
        "key": "risotto", "title": "Risotto night", "hero_noun": "risotto",
        "match": ["risotto"],
        "staples": ["a knob of butter", "grated parmesan"],
        "steps": "1. Prepare the risotto as per the pack.\n"
                 "2. Stir through butter and parmesan at the end.\n"
                 "3. Rest 2 minutes and serve.",
        "tags": ["rice"],
    },
    {
        "key": "soup", "title": "Soup & toast", "hero_noun": "soup",
        "match": ["cup a soup", "soup with", "minestrone", "pumpkin soup", "chicken soup"],
        "staples": ["crusty bread or toast", "butter"],
        "steps": "1. Heat the soup as per the pack.\n"
                 "2. Toast and butter some bread.\n"
                 "3. Serve together.",
        "tags": ["soup", "light"],
    },
]


def load_candidates(db_url: str, log: logging.Logger) -> tuple[dict[str, Candidate], str]:
    """All current-week half-price products (junk excluded), keyed by id, + week."""
    with psycopg.connect(db_url, connect_timeout=20) as conn, conn.cursor() as cur:
        cur.execute("select max(week_start) from specials")
        week = cur.fetchone()[0]
        if week is None:
            return {}, ""
        cur.execute(
            """
            select p.id::text, p.name, coalesce(p.category, 'Uncategorised'),
                   p.retailer, s.regular_price_cents, s.sale_price_cents
            from specials s join products p on p.id = s.product_id
            where s.week_start = %(w)s and s.is_half_price
              and s.sale_price_cents > 0 and s.regular_price_cents > 0
            """,
            {"w": week},
        )
        by_id = {
            r[0]: Candidate(r[0], r[1], r[2], r[3], r[4], r[5])
            for r in cur.fetchall() if not _is_junk(r[1])
        }
    log.info("recipes.candidates loaded=%d week=%s (junk excluded)", len(by_id), week)
    return by_id, str(week)


# --------------------------------------------------------------------------- #
# Validation + costing (shared)
# --------------------------------------------------------------------------- #

def _validate_and_cost(recipe: Recipe, cands: dict[str, Candidate], log: logging.Logger) -> bool:
    """Drop recipes whose hero ids don't resolve; compute the real hero basket."""
    if not recipe.title or not recipe.instructions or not recipe.ingredients:
        return False
    if not (1 <= recipe.serves <= 12):
        recipe.serves = 4
    est = reg = 0
    seen: set[str] = set()
    clean: list[dict] = []
    for ing in recipe.ingredients:
        pid = ing.get("product_id")
        c = cands.get(pid)
        if c is None or pid in seen:
            continue  # hallucinated / off-list / duplicate -> drop
        seen.add(pid)
        clean.append({"product_id": pid, "label": str(ing.get("label") or "").strip()[:40]})
        est += c.sale_cents
        reg += c.regular_cents
    # Hero model: at least one resolvable half-price hero.
    if not clean:
        log.warning("recipes.reject title=%r no resolved heroes", recipe.title)
        return False
    recipe.ingredients = clean
    recipe.estimated_cost_cents = est
    recipe.regular_cost_cents = reg
    recipe.pantry = [str(p).strip()[:40] for p in recipe.pantry][:6]
    return True


# --------------------------------------------------------------------------- #
# Seed path (no LLM)
# --------------------------------------------------------------------------- #

def seed_recipes(cands: dict[str, Candidate], log: logging.Logger) -> list[Recipe]:
    items = sorted(cands.values(), key=lambda c: c.sale_cents)  # cheapest hero per dish
    used: set[str] = set()
    out: list[Recipe] = []
    for dish in _DISHES:
        hero = next(
            (c for c in items
             if c.product_id not in used
             and any(m in c.name.lower() for m in dish["match"])),
            None,
        )
        if hero is None:
            continue
        used.add(hero.product_id)
        out.append(Recipe(
            title=dish["title"], serves=4,
            ingredients=[{"product_id": hero.product_id, "label": "1 pack"}],
            pantry=dish["staples"], instructions=dish["steps"], tags=dish["tags"],
        ))
    log.info("recipes.seed composed=%d", len(out))
    return out


# --------------------------------------------------------------------------- #
# LLM path (Groq)
# --------------------------------------------------------------------------- #

def _meal_relevant(cands: dict[str, Candidate]) -> list[Candidate]:
    """Half-price items that read like meal components — the LLM's menu."""
    hints = [m for d in _DISHES for m in d["match"]] + [
        "sauce", "noodle", "pasta", "rice", "curry", "soup", "beans",
        "tomato", "coconut", "stock", "gravy", "wrap", "tortilla", "cheese",
    ]
    out = [c for c in cands.values() if any(h in c.name.lower() for h in hints)]
    out.sort(key=lambda c: c.sale_cents)
    return out[:45]


def _groq_recipes(menu: list[Candidate], api_key: str, log: logging.Logger) -> list[Recipe]:
    listing = [{"id": c.product_id, "name": c.name} for c in menu]
    prompt = (
        "You are a practical Australian home cook. Below are products that are "
        "HALF-PRICE this week (mostly meal bases, sauces, kits, noodles). Compose "
        "6 to 8 simple weeknight dinners, each built around ONE or TWO of these "
        "half-price products (referenced by exact id) as the 'hero'. For the "
        "everyday staples someone adds (protein, rice, veg, pasta, cheese), DO NOT "
        "use ids - list them as plain text in 'pantry'. Do NOT invent products or "
        "ids. Do NOT mention prices. Return STRICT JSON only: "
        '{"recipes":[{"title":str,"serves":int,'
        '"ingredients":[{"product_id":str,"label":str}],'
        '"pantry":[str],"instructions":str,"tags":[str]}]}. '
        "ingredients = the half-price hero products only; label is a quantity like "
        "'1 pack'. instructions = short numbered steps separated by newlines.\n\n"
        "HALF-PRICE PRODUCTS:\n" + json.dumps(listing, ensure_ascii=False)
    )
    resp = requests.post(
        _GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": _GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    out: list[Recipe] = []
    for r in data.get("recipes", []):
        out.append(Recipe(
            title=str(r.get("title", "")).strip()[:80],
            serves=int(r.get("serves", 4) or 4),
            ingredients=list(r.get("ingredients", [])),
            pantry=[str(p).strip()[:40] for p in (r.get("pantry") or [])][:6],
            instructions=str(r.get("instructions", "")).strip(),
            tags=[str(t).strip()[:20] for t in (r.get("tags") or [])][:5],
        ))
    log.info("recipes.groq returned=%d", len(out))
    return out


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

def write_recipes(db_url: str, week: str, recipes: list[Recipe], log: logging.Logger) -> None:
    now = datetime.now(timezone.utc)
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext('wednesday-ingest'))")
            cur.execute("delete from recipes where week_start = %s", (week,))
            for r in recipes:
                cur.execute(
                    """
                    insert into recipes
                        (week_start, title, description, ingredients, instructions,
                         estimated_cost_cents, regular_cost_cents, serves, pantry,
                         tags, generated_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        week, r.title, None, json.dumps(r.ingredients), r.instructions,
                        r.estimated_cost_cents, r.regular_cost_cents, r.serves,
                        r.pantry, r.tags, now,
                    ),
                )
        conn.commit()
    log.info("recipes.written week=%s count=%d", week, len(recipes))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _current_week_recipe_count(db_url: str, week: str) -> int:
    with psycopg.connect(db_url, connect_timeout=20) as c, c.cursor() as cur:
        cur.execute("select count(*) from recipes where week_start = %s", (week,))
        return cur.fetchone()[0]


def run(*, db_url: str, log: logging.Logger, seed: bool, write_db: bool,
        if_missing: bool = False) -> int:
    api_key = os.environ.get("GROQ_API_KEY")
    if not seed and not api_key:
        log.info("recipes.skip no_key — set GROQ_API_KEY for live generation, or use --seed")
        return 0

    cands, week = load_candidates(db_url, log)
    if not week or len(cands) < 20:
        log.error("recipes.skip insufficient_candidates count=%d week=%s", len(cands), week or "none")
        return 1

    # --if-missing: the daily cron seeds dinners only when the (rolled-over) week
    # has none yet, so dinners track the Wednesday week change without clobbering
    # the richer weekly LLM batch. Promos run a week, so once filled, leave them.
    if if_missing and write_db:
        existing = _current_week_recipe_count(db_url, week)
        if existing > 0:
            log.info("recipes.skip if_missing — %d recipes already exist for week=%s", existing, week)
            return 0

    if seed:
        raw = seed_recipes(cands, log)
        floor = _MIN_SEED_RECIPES
    else:
        raw = _groq_recipes(_meal_relevant(cands), api_key, log)
        floor = _MIN_LLM_RECIPES

    recipes = [r for r in raw if _validate_and_cost(r, cands, log)]
    log.info("recipes.validated kept=%d of=%d", len(recipes), len(raw))
    if len(recipes) < floor:
        log.error("recipes.fail validated=%d < floor=%d", len(recipes), floor)
        return 1

    for r in recipes:
        save = r.regular_cost_cents - r.estimated_cost_cents
        log.info("  %-22s $%.2f save $%.2f  +%d staples",
                 r.title, r.estimated_cost_cents / 100, save / 100, len(r.pantry))

    if write_db:
        write_recipes(db_url, week, recipes, log)
    else:
        log.info("recipes.dry_run (no --write-db); nothing written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate_recipes")
    parser.add_argument("--seed", action="store_true",
                        help="Compose from real on-sale heroes without the LLM.")
    parser.add_argument("--write-db", action="store_true", help="Write recipes to the DB.")
    parser.add_argument("--if-missing", action="store_true",
                        help="Skip when the current week already has recipes (daily gap-fill).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    return run(db_url=db_url, log=log, seed=args.seed, write_db=args.write_db,
               if_missing=args.if_missing)


if __name__ == "__main__":
    sys.exit(main())

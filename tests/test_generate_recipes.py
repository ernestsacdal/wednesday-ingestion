"""Recipe slotting + validation, plus regression coverage for the Groq-output
hardening (malformed JSON / null ingredients must never crash the run)."""
import logging

from src.generate_recipes import (
    Candidate,
    Recipe,
    _groq_recipes,
    _is_junk,
    _slot_match,
    _stale_heroes,
    _validate_and_cost,
)
from src import generate_recipes as gr

LOG = logging.getLogger("test")


class TestIsJunk:
    def test_flags_non_dinner_items(self):
        assert _is_junk("Baby Formula Stage 1")
        assert _is_junk("Cadbury Chocolate Block")
        assert _is_junk("Toilet Paper 12pk")

    def test_passes_real_ingredients(self):
        assert not _is_junk("Leggo's Pasta Sauce 500g")
        assert not _is_junk("San Remo Penne 500g")


class TestSlotMatch:
    def test_word_boundary_rejects_substring_decoy(self):
        # "wrap" must not match inside "Artwrap" (a party sparkler, not a tortilla).
        assert not _slot_match("Artwrap Star Sparkler", ["wrap"], raw=False)

    def test_real_match(self):
        assert _slot_match("San Remo Macaroni 500g", ["macaroni"], raw=False)

    def test_prepared_meal_decoy_excluded_for_ingredient_slot(self):
        # Cup-a-pasta / flavour-beans grab keyword matches but aren't ingredients.
        assert not _slot_match("Continental Cup A Pasta Macaroni", ["macaroni"], raw=False)
        assert not _slot_match("Heinz Baked Beans Pizza Flavour", ["pizza"], raw=False)

    def test_raw_slot_allows_prepared_meal(self):
        # The noodle/soup primary slots (raw=True) bypass the decoy exclusion.
        assert _slot_match("Continental Cup A Soup Asian Laksa", ["laksa"], raw=True)


def _cands():
    return {
        "a": Candidate("a", "Pasta Sauce", "Pantry", "coles", 400, 200),
        "b": Candidate("b", "Penne 500g", "Pantry", "coles", 300, 150),
    }


def _recipe(ingredients):
    return Recipe(title="Pasta night", serves=4, ingredients=ingredients,
                  pantry=["onion"], instructions="1. Cook.", tags=["pasta"])


class TestValidateAndCost:
    def test_two_resolvable_heroes_pass_and_cost(self):
        r = _recipe([{"product_id": "a", "label": "1 jar"},
                     {"product_id": "b", "label": "1 pack"}])
        assert _validate_and_cost(r, _cands(), LOG) is True
        assert r.estimated_cost_cents == 350   # 200 + 150
        assert r.regular_cost_cents == 700     # 400 + 300

    def test_under_two_resolvable_rejected(self):
        r = _recipe([{"product_id": "a", "label": "1 jar"}])
        assert _validate_and_cost(r, _cands(), LOG) is False

    def test_hallucinated_ids_dropped(self):
        # Only one id resolves -> below the 2-hero floor -> rejected.
        r = _recipe([{"product_id": "a", "label": "1 jar"},
                     {"product_id": "ZZZ", "label": "1 pack"}])
        assert _validate_and_cost(r, _cands(), LOG) is False

    def test_duplicate_id_counted_once(self):
        r = _recipe([{"product_id": "a", "label": "1 jar"},
                     {"product_id": "a", "label": "1 jar"}])
        assert _validate_and_cost(r, _cands(), LOG) is False


def _week_recipe(title, pids):
    return {"id": "r-" + title, "title": title,
            "ingredients": [{"product_id": p, "label": "1 pack"} for p in pids]}


class TestStaleHeroes:
    """Truth table for the daily revalidation trigger."""

    def test_all_heroes_still_half_price(self):
        rows = [_week_recipe("Pasta night", ["a", "b"])]
        assert _stale_heroes(rows, _cands()) == []

    def test_one_pruned_hero_marks_recipe_stale(self):
        rows = [_week_recipe("Pasta night", ["a", "b"]),
                _week_recipe("Stir-fry night", ["a", "gone"])]
        assert _stale_heroes(rows, _cands()) == [("Stir-fry night", ["gone"])]

    def test_empty_ingredients_not_stale(self):
        # Existence/floor is verify_data's job; no heroes means nothing pruned.
        assert _stale_heroes([_week_recipe("Odd", [])], _cands()) == []

    def test_no_recipes_no_stale(self):
        assert _stale_heroes([], _cands()) == []

    def test_missing_product_id_key_is_stale(self):
        # A malformed stored ingredient should force regeneration, not pass.
        rows = [{"id": "r", "title": "Broken", "ingredients": [{"label": "1 pack"}]}]
        assert _stale_heroes(rows, _cands()) == [("Broken", [None])]


class _FakeResp:
    def __init__(self, content: str):
        self._content = content
        self.text = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestGroqHardening:
    """A-I1 / A-I2: a bad LLM response must degrade to [], never crash."""

    def _run(self, monkeypatch, content):
        monkeypatch.setattr(gr.requests, "post", lambda *a, **k: _FakeResp(content))
        menu = [Candidate("a", "Pasta Sauce", "Pantry", "coles", 400, 200)]
        return _groq_recipes(menu, "fake-key", LOG)

    def test_malformed_json_returns_empty(self, monkeypatch):
        assert self._run(monkeypatch, '{"recipes": [') == []

    def test_missing_choices_returns_empty(self, monkeypatch):
        monkeypatch.setattr(gr.requests, "post",
                            lambda *a, **k: type("R", (), {"raise_for_status": lambda s: None,
                                                            "json": lambda s: {}, "text": "{}"})())
        assert _groq_recipes([Candidate("a", "X", "Y", "coles", 1, 1)], "k", LOG) == []

    def test_null_ingredients_does_not_crash(self, monkeypatch):
        content = ('{"recipes":[{"title":"X","serves":4,"ingredients":null,'
                   '"pantry":[],"instructions":"do","tags":[]}]}')
        out = self._run(monkeypatch, content)
        assert len(out) == 1 and out[0].ingredients == []

    def test_non_dict_recipe_skipped(self, monkeypatch):
        out = self._run(monkeypatch, '{"recipes":["just a string", 42]}')
        assert out == []

"""Atomic week-rollover guards (ADR-0001): the solo-roll guard in the Woolies
refresh and the stale-dump new-week gate in the Coles refresh. No writer may
create a new promo week unless the week arrives whole and fresh."""
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import refresh_coles_hotprices as rc
from src import refresh_woolies_specials as rw
from src.send_alerts import most_recent_wednesday

LOG = logging.getLogger("test")

EXPECTED = most_recent_wednesday(datetime.now(timezone.utc).date())
LAST_WEEK = EXPECTED - timedelta(days=7)


class _Bail(Exception):
    """Raised by patched deep machinery to prove the guard let the run proceed."""


def _bail(*_a, **_k):
    raise _Bail


class TestSoloRollGuard:
    """refresh_woolies must never be the FIRST writer of a new week."""

    def _setup(self, monkeypatch, *, db_max, coles_rows):
        monkeypatch.setattr(rw, "max_week_start", lambda db: db_max)
        monkeypatch.setattr(rw, "_coles_rows_for_week", lambda db, w: coles_rows)
        # force_fallback=True routes straight to the dump scraper; patching it
        # with _Bail proves execution got PAST the guard.
        monkeypatch.setattr(rw, "_scrape_woolies_dump", _bail)

    def test_first_writer_solo_skips(self, monkeypatch):
        # db_max = last week + no Coles rows for the new week -> deliberate skip.
        self._setup(monkeypatch, db_max=LAST_WEEK, coles_rows=0)
        got = rw.refresh_woolies(db_url="x", log=LOG, force_fallback=True)
        assert got == rw.SKIP_WEEK_NOT_ROLLED

    def test_empty_db_solo_skips(self, monkeypatch):
        self._setup(monkeypatch, db_max=None, coles_rows=0)
        got = rw.refresh_woolies(db_url="x", log=LOG, force_fallback=True)
        assert got == rw.SKIP_WEEK_NOT_ROLLED

    def test_week_already_rolled_proceeds_even_without_coles(self, monkeypatch):
        # Once max == expected, later live upgrades must never be blocked.
        self._setup(monkeypatch, db_max=EXPECTED, coles_rows=0)
        with pytest.raises(_Bail):
            rw.refresh_woolies(db_url="x", log=LOG, force_fallback=True)

    def test_coles_present_proceeds(self, monkeypatch):
        self._setup(monkeypatch, db_max=None, coles_rows=1070)
        with pytest.raises(_Bail):
            rw.refresh_woolies(db_url="x", log=LOG, force_fallback=True)

    def test_main_exit_map(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
        for returned, exit_code in [(rw.SKIP_WEEK_NOT_ROLLED, 0), (0, 1), (1347, 0)]:
            monkeypatch.setattr(rw, "refresh_woolies", lambda *a, _r=returned, **k: _r)
            assert rw.main([]) == exit_code


def _coles_out(week, fresh):
    return SimpleNamespace(
        specials=[SimpleNamespace(week_start=week)],
        run=SimpleNamespace(fresh_week_items=fresh),
    )


class TestStaleDumpGate:
    """refresh_coles must not CREATE a new week from a pre-Wednesday dump."""

    NEW = date(2026, 7, 15)
    OLD = date(2026, 7, 8)

    def _setup(self, monkeypatch, *, out, db_max):
        monkeypatch.setattr(rc, "scrape", lambda log: out)
        monkeypatch.setattr(rc, "max_week_start", lambda db: db_max)
        monkeypatch.setattr(rc, "bulk_write_to_db", _bail)

    def test_new_week_with_stale_dump_blocked(self, monkeypatch):
        self._setup(monkeypatch, out=_coles_out(self.NEW, fresh=3), db_max=self.OLD)
        assert rc.refresh_coles(db_url="x", log=LOG) == 0

    def test_new_week_with_fresh_dump_writes(self, monkeypatch):
        self._setup(monkeypatch, out=_coles_out(self.NEW, fresh=1500), db_max=self.OLD)
        with pytest.raises(_Bail):
            rc.refresh_coles(db_url="x", log=LOG)

    def test_same_week_rewrite_never_gated(self, monkeypatch):
        # Mid-week refreshes carry few fresh-on-Wednesday items; must not block.
        self._setup(monkeypatch, out=_coles_out(self.NEW, fresh=0), db_max=self.NEW)
        with pytest.raises(_Bail):
            rc.refresh_coles(db_url="x", log=LOG)

    def test_empty_db_requires_freshness(self, monkeypatch):
        self._setup(monkeypatch, out=_coles_out(self.NEW, fresh=0), db_max=None)
        assert rc.refresh_coles(db_url="x", log=LOG) == 0

    def test_missing_fresh_count_treated_as_stale(self, monkeypatch):
        self._setup(monkeypatch, out=_coles_out(self.NEW, fresh=None), db_max=self.OLD)
        assert rc.refresh_coles(db_url="x", log=LOG) == 0

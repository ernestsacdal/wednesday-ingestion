"""Weekly watchlist digest sender (Phase 4).

Sends ONE batched Expo push per device per week: "N of your items are
half-price this week: A, B, C." Devices register via the app's
sync_device_watchlist RPC into device_watchlists (migration 0019); this module
intersects each device's watched products with the current week's half-price
specials and pushes through Expo's HTTP API (free, no auth).

Designed to run DAILY at the end of the ingestion pipeline (~12:15pm AEST).
The once-per-week guarantee is the DB unique (device_id, week_start,
alert_type) in device_alerts_log — so a failed Wednesday self-heals Thursday,
and devices that opt in mid-week still get their digest, while nobody is ever
pushed twice for the same week.

Step order per run:
  1. receipts sweep    — check yesterday's Expo tickets; DeviceNotRegistered
                         nulls that device's token (next-day cadence keeps us
                         inside Expo's ~24h receipt window, no sleeping)
  2. orphan GC         — delete devices not synced in 60 days (PRD section 7.4)
  3. week gate         — only send when specials' max(week_start) IS the
                         current Wednesday (never alert on a stale week)
  4. digest query      — eligible devices x this week's half-price watched items
  5. build + send      — canonical copy, batches of <=100
  6. dedup log write   — device_alerts_log, on conflict do nothing

CLI:
    python -m src.send_alerts --dry-run            # print, no sends/writes
    python -m src.send_alerts --device <uuid>      # one device (acceptance gate)
    python -m src.send_alerts --receipts-only      # only step 1
    python -m src.send_alerts --force              # bypass the week gate

Requires SUPABASE_DB_URL. The canonical message copy below MUST stay in sync
with mobile/src/lib/notifications.ts#buildDigestContent (the dev preview).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
import requests

from src.env import load_dotenv

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

_SEND_BATCH = 100      # Expo's per-request message cap
_RECEIPT_BATCH = 300
_MAX_LISTED = 3        # names listed in the body; the rest fold into "+ N more"
_MAX_NAME_CHARS = 40
_GC_DAYS = 60
_HTTP_TIMEOUT = 30

_EXPO_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "accept-encoding": "gzip, deflate",
}


def most_recent_wednesday(today: date) -> date:
    """Same week convention as the scrapers (Monday=0 ... Wednesday=2)."""
    return today - timedelta(days=(today.weekday() - 2) % 7)


def _truncate(name: str) -> str:
    trimmed = name.strip()
    return trimmed[: _MAX_NAME_CHARS - 1] + "…" if len(trimmed) > _MAX_NAME_CHARS else trimmed


def build_message(token: str, names: list[str]) -> dict:
    """Canonical weekly-digest payload (PRD section 2.1).

    Mirrored by mobile/src/lib/notifications.ts#buildDigestContent so the
    in-app dev preview exercises identical strings.
    """
    n = len(names)
    listed = [_truncate(x) for x in names[:_MAX_LISTED]]
    extra = n - len(listed)
    item_list = ", ".join(listed) + (f" + {extra} more" if extra > 0 else "")
    if n == 1:
        body = f"1 of your items is half-price this week: {item_list}."
    else:
        body = f"{n} of your items are half-price this week: {item_list}."
    return {
        "to": token,
        "title": "Half-price this week",
        "body": body,
        "sound": "default",
        "data": {"url": "/watchlist"},
    }


@dataclass
class AlertStats:
    receipts_checked: int = 0
    receipts_errored: int = 0
    tokens_nulled: int = 0
    gc_deleted: int = 0
    eligible: int = 0
    sent: int = 0
    errored: int = 0
    skipped_stale_week: bool = False


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _sweep_receipts(conn: psycopg.Connection, log: logging.Logger, stats: AlertStats) -> None:
    """Resolve pending tickets from earlier runs; null dead tokens."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id::text, expo_ticket_id, device_id
              from device_alerts_log
             where status = 'sent'
               and expo_ticket_id is not null
               and sent_at < now() - interval '30 minutes'
            """
        )
        pending = cur.fetchall()
    if not pending:
        return

    by_ticket = {ticket: (row_id, device) for row_id, ticket, device in pending}
    for batch in _chunks(list(by_ticket.keys()), _RECEIPT_BATCH):
        try:
            resp = requests.post(
                EXPO_RECEIPTS_URL, json={"ids": batch},
                headers=_EXPO_HEADERS, timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            receipts = resp.json().get("data", {})
        except Exception as e:  # noqa: BLE001 — receipts are best-effort
            log.warning("alerts.receipts_fetch_failed err=%s", str(e)[:120])
            continue

        with conn.cursor() as cur:
            for ticket_id, receipt in receipts.items():
                row = by_ticket.get(ticket_id)
                if row is None:
                    continue
                row_id, device_id = row
                stats.receipts_checked += 1
                if receipt.get("status") == "ok":
                    cur.execute(
                        "update device_alerts_log set status='receipt_ok' where id=%s::uuid",
                        (row_id,),
                    )
                else:
                    stats.receipts_errored += 1
                    detail = (receipt.get("details") or {}).get("error") or receipt.get("message")
                    cur.execute(
                        """update device_alerts_log
                              set status='receipt_error', error_detail=%s
                            where id=%s::uuid""",
                        (str(detail)[:500], row_id),
                    )
                    if (receipt.get("details") or {}).get("error") == "DeviceNotRegistered":
                        cur.execute(
                            "update device_watchlists set expo_push_token=null where device_id=%s",
                            (device_id,),
                        )
                        stats.tokens_nulled += 1
        conn.commit()
    log.info(
        "alerts.receipts checked=%d errored=%d tokens_nulled=%d",
        stats.receipts_checked, stats.receipts_errored, stats.tokens_nulled,
    )


def _gc_orphans(conn: psycopg.Connection, log: logging.Logger, stats: AlertStats) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "delete from device_watchlists where last_synced_at < now() - interval '%s days'"
            % _GC_DAYS
        )
        stats.gc_deleted = cur.rowcount
    conn.commit()
    if stats.gc_deleted:
        log.info("alerts.gc deleted=%d (not synced in %d days)", stats.gc_deleted, _GC_DAYS)


def _eligible_digests(
    conn: psycopg.Connection, week: date, only_device: str | None,
) -> list[tuple[str, str, list, list]]:
    """[(device_id, token, product_ids, names)] for devices due a digest."""
    sql = """
        select dw.device_id, dw.expo_push_token,
               array_agg(p.id   order by s.discount_pct desc, p.name) as product_ids,
               array_agg(p.name order by s.discount_pct desc, p.name) as names
          from device_watchlists dw
          join specials s on s.product_id = any(dw.watched_product_ids)
                         and s.week_start = %(week)s
                         and s.is_half_price
          join products p on p.id = s.product_id
         where dw.expo_push_token is not null
           and dw.notification_cadence <> 'off'
           and not exists (select 1 from device_alerts_log l
                            where l.device_id = dw.device_id
                              and l.week_start = %(week)s
                              and l.alert_type = 'weekly_digest')
    """
    params: dict = {"week": week}
    if only_device:
        sql += " and dw.device_id = %(device)s"
        params["device"] = only_device.lower()
    sql += " group by dw.device_id, dw.expo_push_token"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _send_and_log(
    conn: psycopg.Connection,
    week: date,
    digests: list[tuple[str, str, list, list]],
    log: logging.Logger,
    stats: AlertStats,
) -> None:
    for batch in _chunks(digests, _SEND_BATCH):
        messages = [build_message(token, names) for (_d, token, _ids, names) in batch]
        try:
            resp = requests.post(
                EXPO_SEND_URL, json=messages, headers=_EXPO_HEADERS, timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            tickets = resp.json().get("data", [])
        except Exception as e:  # noqa: BLE001 — a failed batch just retries next run
            log.warning("alerts.send_batch_failed n=%d err=%s", len(batch), str(e)[:120])
            stats.errored += len(batch)
            continue

        rows = []
        with conn.cursor() as cur:
            for (device_id, _token, product_ids, names), ticket in zip(batch, tickets):
                ok = ticket.get("status") == "ok"
                detail = None
                if not ok:
                    detail = (ticket.get("details") or {}).get("error") or ticket.get("message")
                    stats.errored += 1
                    if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                        cur.execute(
                            "update device_watchlists set expo_push_token=null where device_id=%s",
                            (device_id,),
                        )
                        stats.tokens_nulled += 1
                else:
                    stats.sent += 1
                rows.append((
                    device_id, "weekly_digest", week, product_ids, len(names),
                    ticket.get("id"), "sent" if ok else "errored",
                    str(detail)[:500] if detail else None,
                ))

            ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s)"] * len(rows))
            flat = [v for r in rows for v in r]
            cur.execute(
                f"""
                insert into device_alerts_log
                    (device_id, alert_type, week_start, product_ids, item_count,
                     expo_ticket_id, status, error_detail)
                values {ph}
                on conflict (device_id, week_start, alert_type) do nothing
                """,
                flat,
            )
        conn.commit()


def run_alerts(
    db_url: str,
    *,
    log: logging.Logger,
    dry_run: bool = False,
    only_device: str | None = None,
    force: bool = False,
    receipts_only: bool = False,
) -> AlertStats:
    stats = AlertStats()
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        if not dry_run:
            _sweep_receipts(conn, log, stats)
            if receipts_only:
                return stats
            _gc_orphans(conn, log, stats)

        # Week gate: only digest when the specials table is on the CURRENT week.
        expected = most_recent_wednesday(datetime.now(timezone.utc).date())
        with conn.cursor() as cur:
            cur.execute("select max(week_start) from specials")
            actual = cur.fetchone()[0]
        if actual != expected and not force:
            stats.skipped_stale_week = True
            log.info("alerts.skip stale_week expected=%s actual=%s", expected, actual)
            return stats
        week = actual

        digests = _eligible_digests(conn, week, only_device)
        stats.eligible = len(digests)
        log.info("alerts.eligible devices=%d week=%s", stats.eligible, week)

        if dry_run:
            for device_id, token, _ids, names in digests:
                msg = build_message(token, names)
                log.info("alerts.dry_run device=%s items=%d body=%r",
                         device_id, len(names), msg["body"])
            return stats

        if digests:
            _send_and_log(conn, week, digests, log, stats)

    log.info(
        "alerts.done eligible=%d sent=%d errored=%d gc=%d receipts=%d",
        stats.eligible, stats.sent, stats.errored, stats.gc_deleted, stats.receipts_checked,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    from src.scrapers.base import configure_logging

    parser = argparse.ArgumentParser(prog="send_alerts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent; no sends, no writes.")
    parser.add_argument("--device", default=None,
                        help="Limit to one device_id (acceptance testing).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the current-week gate.")
    parser.add_argument("--receipts-only", action="store_true",
                        help="Only sweep pending Expo receipts, then exit.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    run_alerts(
        db_url, log=log, dry_run=args.dry_run, only_device=args.device,
        force=args.force, receipts_only=args.receipts_only,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

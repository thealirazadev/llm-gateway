"""The lgw admin CLI. Talks to the database directly; no running gateway required.

Exit codes: 0 success, 1 operation failure, 2 usage or environment error.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth import generate_key
from app.config import get_settings
from app.db import build_engine, session_scope, set_engine
from app.models import VirtualKey, utcnow

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _iso(value: datetime | None) -> str:
    return "-" if value is None else f"{value.replace(microsecond=0).isoformat()}Z"


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def _money(value: Decimal | None, places: str = "0.01") -> str:
    return "-" if value is None else str(Decimal(value).quantize(Decimal(places)))


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        _fail(f"{field} must be a number, got '{raw}'")
        raise SystemExit(EXIT_USAGE) from None
    if value < 0:
        _fail(f"{field} must not be negative")
        raise SystemExit(EXIT_USAGE)
    return value


def cmd_keys_create(args: argparse.Namespace) -> int:
    budget = None if args.budget is None else _parse_decimal(args.budget, "budget")
    raw_key, key_hash, last4 = generate_key()
    with session_scope() as session:
        if session.scalar(select(VirtualKey).where(VirtualKey.name == args.name)) is not None:
            _fail(f"key '{args.name}' already exists")
            return EXIT_FAILURE
        session.add(
            VirtualKey(
                name=args.name,
                key_hash=key_hash,
                key_last4=last4,
                monthly_budget_usd=budget,
                cache_enabled=bool(args.cache),
                active=True,
                created_at=utcnow(),
            )
        )
    print(f"key: {raw_key} (shown once, store it now)", file=sys.stderr)
    budget_text = "unlimited" if budget is None else f"${_money(budget)}/month"
    print(
        f"created key '{args.name}'  budget={budget_text}  "
        f"cache={'on' if args.cache else 'off'}"
    )
    return EXIT_OK


def cmd_keys_list(_: argparse.Namespace) -> int:
    with session_scope() as session:
        keys = list(session.scalars(select(VirtualKey).order_by(VirtualKey.created_at)))
    if not keys:
        print("no keys created yet")
        return EXIT_OK
    rows = [
        [
            key.name,
            key.key_last4,
            _money(key.monthly_budget_usd),
            "on" if key.cache_enabled else "off",
            "yes" if key.active and key.revoked_at is None else "no",
            _iso(key.created_at),
        ]
        for key in keys
    ]
    _print_table(["NAME", "LAST4", "BUDGET_USD", "CACHE", "ACTIVE", "CREATED"], rows)
    return EXIT_OK


def cmd_keys_revoke(args: argparse.Namespace) -> int:
    if not args.yes:
        try:
            answer = input(f"Revoke key {args.name}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            print("aborted")
            return EXIT_OK
    with session_scope() as session:
        key = session.scalar(select(VirtualKey).where(VirtualKey.name == args.name))
        if key is None:
            _fail(f"key '{args.name}' not found")
            return EXIT_FAILURE
        key.active = False
        key.revoked_at = utcnow()
    print(f"revoked key '{args.name}'")
    return EXIT_OK


def cmd_keys_set_budget(args: argparse.Namespace) -> int:
    budget = _parse_decimal(args.usd, "budget")
    with session_scope() as session:
        key = session.scalar(select(VirtualKey).where(VirtualKey.name == args.name))
        if key is None:
            _fail(f"key '{args.name}' not found")
            return EXIT_FAILURE
        key.monthly_budget_usd = budget
    print(f"budget for '{args.name}' set to ${_money(budget)}/month (applies immediately)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lgw", description="llm-gateway admin CLI")
    parser.add_argument("--db", help="database URL, overrides DATABASE_URL")
    commands = parser.add_subparsers(dest="group", required=True)

    keys = commands.add_parser("keys", help="manage virtual keys").add_subparsers(
        dest="command", required=True
    )

    create = keys.add_parser("create", help="create a key and print it once")
    create.add_argument("--name", required=True)
    create.add_argument("--budget", help="monthly budget in USD")
    create.add_argument("--cache", action="store_true", help="opt this key into the cache")
    create.set_defaults(func=cmd_keys_create)

    listing = keys.add_parser("list", help="list keys")
    listing.set_defaults(func=cmd_keys_list)

    revoke = keys.add_parser("revoke", help="revoke a key immediately")
    revoke.add_argument("name")
    revoke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    revoke.set_defaults(func=cmd_keys_revoke)

    set_budget = keys.add_parser("set-budget", help="set a key's monthly budget")
    set_budget.add_argument("name")
    set_budget.add_argument("usd")
    set_budget.set_defaults(func=cmd_keys_set_budget)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        set_engine(build_engine(args.db or get_settings().database_url))
    except (RuntimeError, SQLAlchemyError) as exc:
        _fail(str(exc))
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    except SQLAlchemyError as exc:
        _fail(f"database error: {exc}")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
CLI tool for managing API keys in the QRNG gRPC service.

Must be run from the qrng-grpc project directory so that config.yaml
and ./qrng_grpc.db resolve correctly.

Usage:
    python manage_keys.py list
    python manage_keys.py create --name "my-key" --device firefly-1
    python manage_keys.py create --name "admin-key" --device "*" --admin
    python manage_keys.py update --id <key-id> --rate-limit 500
    python manage_keys.py disable --id <key-id>
    python manage_keys.py enable --id <key-id>
    python manage_keys.py delete --id <key-id>
    python manage_keys.py usage --id <key-id> [--days 7]
"""

import asyncio
import argparse
import sys

from app.config import get_config
from app.database import get_database


def fmt_bytes(n):
    if n is None:
        return "default"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}TB"


def fmt_limit(val, suffix=""):
    return "default" if val is None else f"{val}{suffix}"


async def cmd_list(db):
    keys = await db.list_api_keys()
    if not keys:
        print("No API keys found.")
        return

    print(f"{'ID':<36}  {'PREFIX':<10}  {'NAME':<20}  {'DEVICE':<12}  "
          f"{'ADMIN':<6}  {'ENABLED':<8}  {'RATE/min':<10}  {'DAILY':<12}  "
          f"{'MAX/REQ':<10}  LAST USED")
    print("-" * 158)
    for k in keys:
        print(
            f"{k['id']:<36}  "
            f"{k['key_prefix']:<10}  "
            f"{k['name']:<20}  "
            f"{k['primary_device_id']:<12}  "
            f"{'yes' if k['is_admin'] else 'no':<6}  "
            f"{'yes' if k['enabled'] else 'no':<8}  "
            f"{fmt_limit(k['rate_limit']):<10}  "
            f"{fmt_bytes(k['daily_byte_limit']):<12}  "
            f"{fmt_bytes(k['max_bytes_per_request']):<10}  "
            f"{k['last_used_at'] or 'never'}"
        )


async def cmd_create(db, args):
    raw_key, info = await db.create_api_key(
        name=args.name,
        primary_device_id=args.device,
        is_admin=args.admin,
        rate_limit=args.rate_limit,
        daily_byte_limit=args.daily_bytes,
        max_bytes_per_request=args.max_bytes,
    )

    print("=" * 60)
    print("API KEY CREATED — store the key securely.")
    print("It will NOT be shown again.")
    print("=" * 60)
    print(f"  Key:                {raw_key}")
    print(f"  ID:                 {info['id']}")
    print(f"  Name:               {info['name']}")
    print(f"  Device:             {info['primary_device_id']}")
    print(f"  Admin:              {'yes' if info['is_admin'] else 'no'}")
    print(f"  Rate limit:         {fmt_limit(info['rate_limit'], '/min')}")
    print(f"  Daily limit:        {fmt_bytes(info['daily_byte_limit'])}")
    print(f"  Max bytes/request:  {fmt_bytes(info['max_bytes_per_request'])}")
    print(f"  Created:            {info['created_at']}")
    print("=" * 60)


async def cmd_update(db, args):
    key = await db.get_api_key_by_id(args.id)
    if not key:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        sys.exit(1)

    kwargs = {}
    if args.name is not None:
        kwargs["name"] = args.name
    if args.device is not None:
        kwargs["primary_device_id"] = args.device
    if args.rate_limit is not None:
        kwargs["rate_limit"] = args.rate_limit
    if args.daily_bytes is not None:
        kwargs["daily_byte_limit"] = args.daily_bytes
    if args.max_bytes is not None:
        kwargs["max_bytes_per_request"] = args.max_bytes

    if not kwargs:
        print("Nothing to update — specify at least one option.", file=sys.stderr)
        sys.exit(1)

    await db.update_api_key(args.id, **kwargs)

    updated = await db.get_api_key_by_id(args.id)
    print(f"Updated key '{updated['name']}' ({updated['key_prefix']}...):")
    for field, label in [
        ("name",                 "Name"),
        ("primary_device_id",    "Device"),
        ("rate_limit",           "Rate limit"),
        ("daily_byte_limit",     "Daily limit"),
        ("max_bytes_per_request","Max bytes/request"),
    ]:
        val = updated[field]
        if field in ("daily_byte_limit", "max_bytes_per_request"):
            val = fmt_bytes(val)
        elif field == "rate_limit":
            val = fmt_limit(val, "/min")
        print(f"  {label:<20} {val}")


async def cmd_delete(db, args):
    key = await db.get_api_key_by_id(args.id)
    if not key:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        sys.exit(1)
    confirm = input(f"Delete key '{key['name']}' ({key['key_prefix']}...)? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return
    await db.delete_api_key(args.id)
    print(f"Deleted key '{key['name']}'.")


async def cmd_enable(db, args):
    ok = await db.update_api_key(args.id, enabled=True)
    if not ok:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        sys.exit(1)
    print(f"Key {args.id} enabled.")


async def cmd_disable(db, args):
    ok = await db.update_api_key(args.id, enabled=False)
    if not ok:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        sys.exit(1)
    print(f"Key {args.id} disabled.")


async def cmd_usage(db, args):
    stats = await db.get_usage_stats(args.id, days=args.days)
    if not stats:
        print(f"Error: no key found with ID {args.id}", file=sys.stderr)
        sys.exit(1)

    print(f"Usage for '{stats['key_name']}' (device: {stats['primary_device_id']})")
    print(f"  Period:          last {stats['period_days']} days")
    print(f"  Total requests:  {stats['total_requests']}")
    print(f"  Total bytes:     {fmt_bytes(stats['total_bytes'])}")
    print(f"  Today requests:  {stats['today_requests']}")
    print(f"  Today bytes:     {fmt_bytes(stats['today_bytes'])}")
    if stats['daily_byte_limit']:
        print(f"  Daily limit:     {fmt_bytes(stats['daily_byte_limit'])}")
    if stats['history']:
        print()
        print(f"  {'DATE':<12}  {'REQUESTS':>10}  {'BYTES':>12}")
        print(f"  {'-'*38}")
        for day in stats['history']:
            print(f"  {day['date']:<12}  {day['requests']:>10}  {fmt_bytes(day['bytes_served']):>12}")


async def main():
    parser = argparse.ArgumentParser(
        description="Manage API keys for the QRNG gRPC service."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all API keys")

    p_create = sub.add_parser("create", help="Create a new API key")
    p_create.add_argument("--name", required=True, help="Descriptive name for the key")
    p_create.add_argument("--device", required=True,
                          help="Primary device ID (e.g. firefly-1, firefly-2, or * for any)")
    p_create.add_argument("--admin", action="store_true", help="Grant admin privileges")
    p_create.add_argument("--rate-limit", type=int, metavar="RPM",
                          help="Requests per minute (default: from config)")
    p_create.add_argument("--daily-bytes", type=int, metavar="BYTES",
                          help="Daily byte limit (default: from config)")
    p_create.add_argument("--max-bytes", type=int, metavar="BYTES",
                          help="Max bytes per request (default: from config)")

    p_update = sub.add_parser("update", help="Update settings on an existing API key")
    p_update.add_argument("--id", required=True, help="Key ID")
    p_update.add_argument("--name", help="New name")
    p_update.add_argument("--device", help="New primary device ID")
    p_update.add_argument("--rate-limit", type=int, metavar="RPM",
                          help="New requests-per-minute limit")
    p_update.add_argument("--daily-bytes", type=int, metavar="BYTES",
                          help="New daily byte limit")
    p_update.add_argument("--max-bytes", type=int, metavar="BYTES",
                          help="New max bytes per request")

    p_delete = sub.add_parser("delete", help="Delete an API key")
    p_delete.add_argument("--id", required=True, help="Key ID")

    p_enable = sub.add_parser("enable", help="Enable a disabled API key")
    p_enable.add_argument("--id", required=True, help="Key ID")

    p_disable = sub.add_parser("disable", help="Disable an API key")
    p_disable.add_argument("--id", required=True, help="Key ID")

    p_usage = sub.add_parser("usage", help="Show usage stats for a key")
    p_usage.add_argument("--id", required=True, help="Key ID")
    p_usage.add_argument("--days", type=int, default=7, help="History window in days (default: 7)")

    args = parser.parse_args()

    db = get_database()
    await db.connect()
    try:
        if args.command == "list":
            await cmd_list(db)
        elif args.command == "create":
            await cmd_create(db, args)
        elif args.command == "update":
            await cmd_update(db, args)
        elif args.command == "delete":
            await cmd_delete(db, args)
        elif args.command == "enable":
            await cmd_enable(db, args)
        elif args.command == "disable":
            await cmd_disable(db, args)
        elif args.command == "usage":
            await cmd_usage(db, args)
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

"""Command line: run the server or manage users.

  python -m studio serve [--host 0.0.0.0] [--port 8080]
  python -m studio create-user USERNAME --role admin [--email you@example.org]
  python -m studio tick          # one background pass (process media, publish, reminders)
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy import select


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studio")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", default="contributor", choices=["contributor", "editor", "approver", "admin"])
    cu.add_argument("--email", default="")
    cu.add_argument("--proxy-username", default=None)
    sub.add_parser("tick")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False)
        return 0

    from . import db
    from .config import load_settings

    db.init(load_settings())
    if args.cmd == "tick":
        from .scheduler import tick

        tick()
        return 0

    from .models import User
    from .security import hash_password

    password = getpass.getpass("Password (10+ characters): ")
    if len(password) < 10:
        print("Password too short.", file=sys.stderr)
        return 1
    with db.session_scope() as s:
        if s.scalar(select(User).where(User.username == args.username)):
            print("That username exists.", file=sys.stderr)
            return 1
        s.add(User(username=args.username, role=args.role, email=args.email,
                   proxy_username=args.proxy_username, password_hash=hash_password(password)))
    print(f"Created {args.username} ({args.role}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

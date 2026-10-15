"""infra-mcp serve | token create <read|write> <label> | token list | token revoke <id>"""

import argparse
import json
import sys

from . import config, tokens


def main() -> None:
    parser = argparse.ArgumentParser(prog="infra-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="lance le serveur MCP (Streamable HTTP)")

    tok = sub.add_parser("token", help="gère les tokens d'accès").add_subparsers(
        dest="action", required=True
    )
    create = tok.add_parser("create")
    create.add_argument("level", choices=list(tokens.LEVELS))
    create.add_argument("label", help="à quoi sert ce token, ex. claude-code-web")
    tok.add_parser("list")
    revoke = tok.add_parser("revoke")
    revoke.add_argument("token_id")

    args = parser.parse_args()

    if args.cmd == "serve":
        from .server import mcp

        mcp.run(transport="http", host=config.HOST, port=config.PORT, path="/mcp")
    elif args.action == "create":
        token = tokens.create(config.TOKENS_FILE, args.level, args.label)
        print(token)
        print("Ce token ne sera plus jamais affiché : copie-le maintenant.", file=sys.stderr)
    elif args.action == "list":
        print(json.dumps(tokens.listing(config.TOKENS_FILE), indent=2, ensure_ascii=False))
    elif args.action == "revoke":
        if not tokens.revoke(config.TOKENS_FILE, args.token_id):
            sys.exit(f"token inconnu : {args.token_id}")
        print("révoqué")


if __name__ == "__main__":
    main()

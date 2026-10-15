"""infra-mcp serve | lint [dossier] | cloud-init <hôte> | token create <read|write> <label> | token list | token revoke <id>"""

import argparse
import json
import sys
from pathlib import Path

from . import config, tokens


def main() -> None:
    parser = argparse.ArgumentParser(prog="infra-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="lance le serveur MCP (Streamable HTTP)")
    lint_cmd = sub.add_parser("lint", help="vérifie les garde-fous d'un repo de config Ansible")
    lint_cmd.add_argument("ansible_dir", nargs="?", type=Path, help="défaut : INFRA_MCP_ANSIBLE_DIR")
    ci_cmd = sub.add_parser("cloud-init", help="user-data cloud-init pour une nouvelle machine de l'inventaire")
    ci_cmd.add_argument("host")

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
    elif args.cmd == "cloud-init":
        import subprocess

        from .cloudinit import user_data
        from .runner import _bin

        res = subprocess.run(
            [_bin("ansible-inventory"), "--host", args.host], cwd=config.ANSIBLE_DIR, capture_output=True, text=True
        )
        if res.returncode != 0:
            sys.exit(f"{args.host} : absent de l'inventaire ({res.stderr.strip()[-300:]})")
        try:
            print(user_data(args.host, json.loads(res.stdout)), end="")
        except ValueError as exc:
            sys.exit(str(exc))
    elif args.cmd == "lint":
        from . import lint

        problems = lint.run(args.ansible_dir or config.ANSIBLE_DIR)
        for problem in problems:
            print(problem)
        if problems:
            sys.exit(f"{len(problems)} problème(s)")
        print("ok")
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

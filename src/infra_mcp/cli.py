"""infra-mcp serve | lint [dossier] | sync [dossier] | cloud-init <hôte> | secret set <nom> | registry set <registre> <username> | token create <read|write> <label> | token list | token revoke <id>"""

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
    sync_cmd = sub.add_parser("sync", help="avance le repo de config au dernier commit de sa branche, s'il passe le lint")
    sync_cmd.add_argument("ansible_dir", nargs="?", type=Path, help="défaut : INFRA_MCP_ANSIBLE_DIR")
    ci_cmd = sub.add_parser("cloud-init", help="user-data cloud-init pour une nouvelle machine de l'inventaire")
    ci_cmd.add_argument("host")

    sec = sub.add_parser("secret", help="secrets du vault, depuis un clone du repo de config").add_subparsers(
        dest="action", required=True
    )
    sec_set = sec.add_parser("set", help="ajoute ou remplace un secret (valeur saisie sans écho, ou lue sur stdin)")
    sec_set.add_argument("name", help="nom du secret Docker, ex. playtrail_mailer_dsn")
    sec_set.add_argument("--repo", type=Path, default=Path("."), help="racine du repo de config (défaut : dossier courant)")

    reg = sub.add_parser("registry", help="registres privés, depuis un clone du repo de config").add_subparsers(
        dest="action", required=True
    )
    reg_set = reg.add_parser("set", help="ajoute ou remplace le token d'un registre (saisi sans écho, ou lu sur stdin)")
    reg_set.add_argument("registry", help="nom d'hôte du registre, ex. ghcr.io")
    reg_set.add_argument("username", help="compte qui possède le token")
    reg_set.add_argument("--repo", type=Path, default=Path("."), help="racine du repo de config (défaut : dossier courant)")

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
    elif args.cmd == "sync":
        from .sync import SyncError, sync

        try:
            print(sync(args.ansible_dir or config.ANSIBLE_DIR))
        except SyncError as exc:
            sys.exit(str(exc))
    elif args.cmd == "lint":
        from . import lint

        problems = lint.run(args.ansible_dir or config.ANSIBLE_DIR)
        for problem in problems:
            print(problem)
        if problems:
            sys.exit(f"{len(problems)} problème(s)")
        print("ok")
    elif args.cmd == "secret":
        from .vault import VaultError, set_secret

        try:
            print(set_secret(args.repo, args.name, _read_value(), _vault_password(args.repo)))
        except VaultError as exc:
            sys.exit(str(exc))
    elif args.cmd == "registry":
        from .vault import VaultError, set_registry

        try:
            print(set_registry(args.repo, args.registry, args.username, _read_value("Token du registre"), _vault_password(args.repo)))
        except VaultError as exc:
            sys.exit(str(exc))
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


def _read_value(label: str = "Valeur") -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read().removesuffix("\n")
    from getpass import getpass

    value = getpass(f"{label} (sans écho) : ")
    if getpass("Encore une fois : ") != value:
        sys.exit("les deux saisies diffèrent")
    return value


def _vault_password(repo: Path) -> str:
    import os
    from getpass import getpass

    from .vault import VARS_DIR

    if path := os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE"):
        return Path(path).expanduser().read_text().strip()
    # getpass lit le terminal, même quand la valeur arrive par stdin.
    password = getpass("Mot de passe vault : ")
    if not (repo / VARS_DIR / "vault.yml").exists() and getpass("Nouveau vault, encore une fois : ") != password:
        sys.exit("les deux saisies diffèrent")
    return password


if __name__ == "__main__":
    main()

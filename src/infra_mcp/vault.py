"""infra-mcp secret set : ajoute ou remplace un secret du vault, dans un clone du repo de config.

À lancer sur le poste de l'admin, pas sur le contrôleur : la valeur est saisie
sans écho (ou lue sur l'entrée standard), chiffrée dans
group_vars/swarm_managers/vault.yml sous vault_<nom>, et le secret est déclaré
dans swarm_secrets (stacks.yml du même dossier). Il reste à le lister dans les
secrets de la stack qui l'utilise, puis à committer.

infra-mcp registry set : même chose pour le token d'un registre privé (ghcr.io…),
chiffré sous vault_registry_<registre> et déclaré dans swarm_registries.
"""

import json
import re
from pathlib import Path

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret

from .lint import REGISTRY_RE

VARS_DIR = Path("inventory/group_vars/swarm_managers")
# vault_<nom> doit être un nom de variable Ansible : ni tiret ni point.
NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


class VaultError(Exception):
    pass


def _vault(password: str) -> tuple[VaultLib, VaultSecret]:
    secret = VaultSecret(password.encode())
    return VaultLib([(DEFAULT_VAULT_ID_MATCH, secret)]), secret


def read_vault(path: Path, password: str) -> dict:
    if not path.exists():
        return {}
    lib, _ = _vault(password)
    try:
        data = yaml.safe_load(lib.decrypt(path.read_bytes())) or {}
    except AnsibleError as exc:
        raise VaultError(f"{path} : déchiffrement impossible (mot de passe vault incorrect ?)") from exc
    if not isinstance(data, dict):
        raise VaultError(f"{path} : un dictionnaire de variables vault_* est attendu")
    return data


def write_vault(path: Path, password: str, data: dict) -> None:
    lib, secret = _vault(password)
    text = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    path.write_bytes(lib.encrypt(text, secret))


def _insert(stacks_file: Path, var: str, entry: str) -> None:
    """Ajoute entry (lignes indentées) sous var: dans stacks.yml, en gardant le reste du fichier."""
    text = stacks_file.read_text() if stacks_file.exists() else ""
    header = re.search(rf"^{var}:[ \t]*(\{{\}})?[ \t]*(#.*)?$", text, re.M)
    if header:
        end = header.end() + 1 if header.end() < len(text) else header.end()
        line = f"{var}:" + (f" {header.group(2)}" if header.group(2) else "") + "\n"
        text = text[: header.start()] + line + entry + text[end:]
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + f"{var}:\n" + entry
    stacks_file.write_text(text)


def declare(stacks_file: Path, name: str) -> bool:
    """Ajoute name à swarm_secrets dans stacks.yml, en gardant le reste du fichier. False s'il y est déjà."""
    text = stacks_file.read_text() if stacks_file.exists() else ""
    config = yaml.safe_load(text) or {}
    if name in (config.get("swarm_secrets") or {}):
        return False
    entry = f'  {name}: "{{{{ vault_{name} }}}}"\n'
    _insert(stacks_file, "swarm_secrets", entry)
    if (yaml.safe_load(stacks_file.read_text()).get("swarm_secrets") or {}).get(name) != f"{{{{ vault_{name} }}}}":
        stacks_file.write_text(text)
        raise VaultError(f"{stacks_file} : swarm_secrets n'a pas pu être complété, ajoute la ligne à la main :\n{entry}")
    return True


def _vars_dir(repo: Path) -> Path:
    vars_dir = repo / VARS_DIR
    if not vars_dir.is_dir():
        raise VaultError(f"{vars_dir} absent : lancer la commande à la racine du repo de config")
    return vars_dir


def _store(vars_dir: Path, key: str, value: str, password: str) -> bool:
    """Chiffre value sous key dans vault.yml. True si la variable existait."""
    if not value:
        raise VaultError("valeur vide")
    vault_file = vars_dir / "vault.yml"
    data = read_vault(vault_file, password)
    replaced = key in data
    data[key] = value
    write_vault(vault_file, password, data)
    return replaced


def registry_key(registry: str) -> str:
    return "vault_registry_" + re.sub(r"[^a-z0-9]", "_", registry)


def _check_registry(stacks_file: Path, registry: str, username: str) -> bool:
    """Vérifie registre et compte. True si le registre est déjà déclaré (avec ce compte)."""
    if not REGISTRY_RE.fullmatch(registry):
        raise VaultError(f"{registry} : nom d'hôte de registre invalide (ex. ghcr.io)")
    if not username or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", username):
        raise VaultError(f"{username!r} : username invalide")
    wanted = {"username": username, "password": f"{{{{ {registry_key(registry)} }}}}"}
    text = stacks_file.read_text() if stacks_file.exists() else ""
    current = ((yaml.safe_load(text) or {}).get("swarm_registries") or {}).get(registry)
    if current is not None and current != wanted:
        raise VaultError(f"{stacks_file} : {registry} est déjà dans swarm_registries, autrement ; "
                         f"corrige l'entrée à la main :\n{_registry_entry(registry, username)}")
    return current is not None


def _registry_entry(registry: str, username: str) -> str:
    return f"  {registry}:\n    username: {username}\n    password: \"{{{{ {registry_key(registry)} }}}}\"\n"


def _declare_registry(stacks_file: Path, registry: str, username: str) -> None:
    text = stacks_file.read_text() if stacks_file.exists() else ""
    entry = _registry_entry(registry, username)
    _insert(stacks_file, "swarm_registries", entry)
    wanted = {"username": username, "password": f"{{{{ {registry_key(registry)} }}}}"}
    if (yaml.safe_load(stacks_file.read_text()).get("swarm_registries") or {}).get(registry) != wanted:
        stacks_file.write_text(text)
        raise VaultError(f"{stacks_file} : swarm_registries n'a pas pu être complété, ajoute à la main :\n{entry}")


def set_registry(repo: Path, registry: str, username: str, token: str, password: str) -> str:
    """Chiffre le token sous vault_registry_<registre> et déclare le registre. Renvoie ce qui a été fait."""
    vars_dir = _vars_dir(repo)
    stacks_file = vars_dir / "stacks.yml"
    declared = _check_registry(stacks_file, registry, username)
    key = registry_key(registry)
    replaced = _store(vars_dir, key, token, password)
    vault_rel = (vars_dir / "vault.yml").relative_to(repo)
    done = [f"{vault_rel} : {key} {'remplacée' if replaced else 'ajoutée'}"]
    if not declared:
        _declare_registry(stacks_file, registry, username)
        done.append(f"{stacks_file.relative_to(repo)} : {registry} ajouté à swarm_registries")
    done.append(f"au prochain apply stacks : le manager se connecte à {registry}, les nœuds reçoivent les identifiants")
    return "\n".join(done)


def set_secret(repo: Path, name: str, value: str, password: str) -> str:
    """Chiffre value sous vault_<name> et déclare le secret. Renvoie ce qui a été fait."""
    if not NAME_RE.fullmatch(name):
        raise VaultError(f"{name} : nom invalide (minuscules, chiffres et _ ; commence par une lettre)")
    vars_dir = _vars_dir(repo)
    stacks_file = vars_dir / "stacks.yml"
    config = (yaml.safe_load(stacks_file.read_text()) if stacks_file.exists() else None) or {}
    if name in (config.get("swarm_secrets_generated") or []):
        raise VaultError(f"{name} est dans swarm_secrets_generated : sa valeur est créée sur le serveur")
    vault_file = vars_dir / "vault.yml"
    key = f"vault_{name}"
    replaced = _store(vars_dir, key, value, password)
    declared = declare(stacks_file, name)
    done = [f"{vault_file.relative_to(repo)} : {key} {'remplacée' if replaced else 'ajoutée'}"]
    if declared:
        done.append(f"{stacks_file.relative_to(repo)} : {name} ajouté à swarm_secrets")
    if replaced:
        done.append("au prochain apply stacks : nouvelle version du secret, stacks qui l'utilisent redéployées")
    else:
        done.append(f"reste à lister {name} dans les secrets de la stack (swarm_stacks) et dans son compose (external: true)")
    return "\n".join(done)


# --- infra-mcp vault : plusieurs changements, écrits d'un coup -----------------------
#
# La session (vault_cli.py) prépare une liste de Change, vérifiés un par un contre
# l'état du repo (VaultRepo.check), puis apply() écrit tout : vault.yml une seule
# fois, stacks.yml et backup.yml par retouches de texte vérifiées en relisant le
# YAML. En cas d'erreur, les fichiers sont remis comme avant (snapshot/restore).

BACKUP_FILE = "backup.yml"
# Variables vault lues par backup_credentials (README, « Sauvegardes »).
BACKUP_VARS = {"password": "backup_password", "s3_access_key": "backup_s3_access_key", "s3_secret_key": "backup_s3_secret_key"}

KINDS = {
    "secret": "secret Docker",
    "variable": "variable vault",
    "generated": "secret généré sur le serveur",
    "registry": "token du registre",
    "backup_config": "configuration des sauvegardes",
    "delete": "suppression",
    "delete_registry": "suppression du registre",
}


class Change:
    """kind : secret, variable, generated, registry, backup_config, delete ou delete_registry.
    name : nom sans vault_, nom d'hôte du registre, ou dépôt restic (backup_config)."""

    def __init__(self, kind: str, name: str, value: str = "", username: str = "", generated: bool = False):
        self.kind, self.name, self.value, self.username, self.generated = kind, name, value, username, generated

    @property
    def key(self) -> tuple:
        """Deux changements de même clé : le second remplace le premier."""
        if self.kind == "backup_config":
            return ("backup",)
        if self.kind in ("registry", "delete_registry"):
            return ("registry", self.name)
        return ("name", self.name)


def _yaml_files(root: Path):
    for path in sorted((root / "inventory").rglob("*")):
        if path.suffix in (".yml", ".yaml") and path.is_file() and not _is_vault(path):
            yield path


def _is_vault(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(14) == b"$ANSIBLE_VAULT"


class VaultRepo:
    """Le repo de config tel qu'il est sur le disque, vault déchiffré."""

    def __init__(self, root: Path, password: str):
        self.root = Path(root)
        self.password = password
        self.vars_dir = _vars_dir(self.root)
        self.vault_file = self.vars_dir / "vault.yml"
        self.stacks_file = self.vars_dir / "stacks.yml"
        self.vault = read_vault(self.vault_file, password)
        self.config = (yaml.safe_load(self.stacks_file.read_text()) if self.stacks_file.exists() else None) or {}

    # État courant
    @property
    def secrets(self) -> dict:
        return self.config.get("swarm_secrets") or {}

    @property
    def generated(self) -> list:
        return self.config.get("swarm_secrets_generated") or []

    @property
    def registries(self) -> dict:
        return self.config.get("swarm_registries") or {}

    def summary(self) -> list[str]:
        variables = sorted(k.removeprefix("vault_") for k in self.vault
                           if k.removeprefix("vault_") not in self.secrets and not k.startswith("vault_registry_"))
        return [
            f"secrets Docker : {', '.join(sorted(self.secrets)) or 'aucun'}",
            f"variables vault seules : {', '.join(variables) or 'aucune'}",
            f"secrets générés sur le serveur : {', '.join(self.generated) or 'aucun'}",
            f"registres : {', '.join(sorted(self.registries)) or 'aucun'}",
        ]

    def backup_defined(self) -> Path | None:
        """Fichier qui définit déjà backup_repository, s'il y en a un."""
        for path in _yaml_files(self.root):
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict) and "backup_repository" in data:
                return path
        return None

    def backup_credentials_ok(self, path: Path) -> bool:
        creds = (yaml.safe_load(path.read_text()) or {}).get("backup_credentials") or {}
        return all(creds.get(k) == f"{{{{ vault_{v} }}}}" for k, v in BACKUP_VARS.items())

    def uses(self, name: str) -> list[str]:
        """Où name (secret ou variable vault_<name>) sert encore, hors de sa propre déclaration."""
        found = [f"stack {s.get('name')}" for s in self.config.get("swarm_stacks") or []
                 if isinstance(s, dict) and name in (s.get("secrets") or [])]
        declaration = re.compile(rf"^\s+{re.escape(name)}:\s*[\"']\{{\{{\s*vault_{re.escape(name)}\s*\}}\}}[\"']\s*$")
        ref = re.compile(rf"\bvault_{re.escape(name)}\b")
        for path in _yaml_files(self.root):
            for line in path.read_text().splitlines():
                if line.lstrip().startswith("#") or (path == self.stacks_file and declaration.match(line)):
                    continue
                if ref.search(line):
                    found.append(str(path.relative_to(self.root)))
                    break
        return found

    def registry_uses(self, registry: str) -> list[str]:
        found = []
        for compose in sorted((self.root / "stacks").glob("*/compose.yml")):
            services = (yaml.safe_load(compose.read_text()) or {}).get("services") or {}
            if any(str((s or {}).get("image", "")).startswith(f"{registry}/") for s in services.values()):
                found.append(f"stack {compose.parent.name}")
        return found

    def exists(self, change: Change) -> bool:
        """Vrai si le changement remplace quelque chose (ou, pour delete, s'il y a de quoi supprimer)."""
        if change.kind in ("secret", "variable"):
            return f"vault_{change.name}" in self.vault
        if change.kind == "generated":
            return change.name in self.generated
        if change.kind == "registry":
            return change.name in self.registries
        return False

    def deletion(self, target: str) -> Change:
        """Change de suppression pour target (nom ou registre), refusé s'il sert encore."""
        if target in self.registries:
            if uses := self.registry_uses(target):
                raise VaultError(f"{target} : des images en viennent encore ({', '.join(uses)})")
            return Change("delete_registry", target)
        if target not in self.generated and f"vault_{target}" not in self.vault and target not in self.secrets:
            raise VaultError(f"{target} : ni dans le vault, ni dans swarm_secrets_generated, ni dans swarm_registries")
        if uses := self.uses(target):
            raise VaultError(f"{target} sert encore ({', '.join(uses)}) : retire-le d'abord")
        return Change("delete", target)

    def check(self, change: Change) -> None:
        """Lève VaultError si le changement ne peut pas être appliqué."""
        kind, name = change.kind, change.name
        if kind in ("secret", "variable", "generated") and not NAME_RE.fullmatch(name):
            raise VaultError(f"{name} : nom invalide (minuscules, chiffres et _ ; commence par une lettre)")
        if kind in ("secret", "variable") and name in self.generated:
            raise VaultError(f"{name} est dans swarm_secrets_generated : sa valeur est créée sur le serveur")
        if kind == "variable" and name in self.secrets:
            raise VaultError(f"{name} est un secret Docker (swarm_secrets) : choisis « secret Docker » pour le remplacer")
        if kind == "generated" and (name in self.secrets or f"vault_{name}" in self.vault):
            raise VaultError(f"{name} est déjà dans le vault : supprime-le d'abord")
        if kind == "generated" and name in self.generated:
            raise VaultError(f"{name} est déjà dans swarm_secrets_generated")
        if kind == "registry":
            _check_registry(self.stacks_file, name, change.username)
        if kind in ("secret", "variable", "registry") and not change.value:
            raise VaultError("valeur vide")
        if kind == "backup_config":
            if where := self.backup_defined():
                raise VaultError(f"backup_repository est déjà défini dans {where.relative_to(self.root)}")
            if not name or re.search(r"[\s{}\"'\\]", name):
                raise VaultError(f"{name!r} : dépôt restic invalide (ex. s3:https://s3.gra.io.cloud.ovh.net/mon-bucket)")

    def describe(self, change: Change) -> str:
        kind, name = change.kind, change.name
        if kind == "delete_registry":
            return f"{name} : registre supprimé"
        if kind == "delete":
            what = ("secret généré" if name in self.generated
                    else "secret Docker" if name in self.secrets else "variable vault")
            return f"{name} : {what} supprimé"
        if kind == "backup_config":
            return f"sauvegardes : dépôt {name}, {BACKUP_FILE} créé"
        verb = "remplacé" if self.exists(change) else "ajouté"
        how = " (valeur générée)" if change.generated else ""
        extra = f", compte {change.username}" if kind == "registry" else ""
        return f"{name} : {KINDS[kind]} {verb}{extra}{how}"


def snapshot(repo: VaultRepo) -> dict:
    files = [repo.vault_file, repo.stacks_file, repo.vars_dir / BACKUP_FILE]
    return {p: (p.read_bytes() if p.exists() else None) for p in files}


def restore(saved: dict) -> None:
    for path, content in saved.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)


def _top_section(text: str, var: str) -> tuple[int, int] | None:
    """(début, fin) des lignes d'un bloc var: (en-tête exclu) : lignes indentées, vides ou commentées."""
    lines = text.splitlines(keepends=True)
    offset = 0
    for i, line in enumerate(lines):
        if re.match(rf"{re.escape(var)}:[ \t]*(#.*)?$", line.rstrip("\n")):
            start = end = offset + len(line)
            for nxt in lines[i + 1:]:
                if nxt.strip() and not nxt.startswith((" ", "\t", "#")):
                    break
                end += len(nxt)
            return start, end
        offset += len(line)
    return None


def _edit_stacks(stacks_file: Path, var: str, edit, expected) -> None:
    """Applique edit(texte) -> texte, puis vérifie que var vaut expected une fois relu ; sinon
    remet le fichier comme avant."""
    text = stacks_file.read_text() if stacks_file.exists() else ""
    before = yaml.safe_load(text) or {}
    new = edit(text)
    try:
        after = yaml.safe_load(new) or {}
        # var vaut ce qui est attendu, et rien d'autre n'a bougé.
        ok = (after.get(var) or type(expected)()) == expected and \
            {k: v for k, v in after.items() if k != var} == {k: v for k, v in before.items() if k != var}
    except yaml.YAMLError:
        ok = False
    if not ok:
        raise VaultError(f"{stacks_file} : {var} n'a pas pu être modifié automatiquement, fais-le à la main")
    stacks_file.write_text(new)


def _set_header(head: str, var: str, empty: str) -> str:
    """Dernière ligne de head (l'en-tête var:) réécrite en var: {} ou var: [], commentaire gardé."""
    before, _, header = head.rstrip("\n").rpartition("\n")
    header = re.sub(rf"^{re.escape(var)}:[ \t]*", f"{var}: {empty}", header, count=1)
    header = header.replace(f"{empty}#", f"{empty}  #")
    return (before + "\n" if before else "") + header + "\n"


def _remove_entry(stacks_file: Path, var: str, key: str) -> None:
    """Retire key (et ses lignes plus indentées) du dictionnaire var de stacks.yml."""
    current = dict((yaml.safe_load(stacks_file.read_text()) or {}).get(var) or {})
    current.pop(key, None)

    def edit(text):
        section = _top_section(text, var)
        if section is None:
            return text
        start, end = section
        kept, skipping = [], False
        for line in text[start:end].splitlines(keepends=True):
            if re.match(rf"  {re.escape(key)}:", line):
                skipping = True
                continue
            if skipping and line.startswith("   "):
                continue
            skipping = False
            kept.append(line)
        head = _set_header(text[:start], var, "{}") if not current else text[:start]
        return head + "".join(kept) + text[end:]

    _edit_stacks(stacks_file, var, edit, current)


def _list_change(stacks_file: Path, var: str, item: str, add: bool) -> None:
    text = stacks_file.read_text() if stacks_file.exists() else ""
    current = list((yaml.safe_load(text) or {}).get(var) or [])
    wanted = current + [item] if add else [x for x in current if x != item]

    def edit(text):
        inline = re.search(rf"^{re.escape(var)}:[ \t]*(\[[^\]\n]*\])", text, re.M)
        if inline:
            return text[: inline.start(1)] + "[" + ", ".join(wanted) + "]" + text[inline.end(1):]
        section = _top_section(text, var)
        if section is None:
            return text + ("" if not text or text.endswith("\n") else "\n") + f"{var}:\n  - {item}\n"
        start, end = section
        if add:
            return text[:start] + f"  - {item}\n" + text[start:]
        body = "".join(line for line in text[start:end].splitlines(keepends=True)
                       if not re.fullmatch(rf"\s*-\s*{re.escape(item)}\s*(#.*)?\n?", line))
        head = _set_header(text[:start], var, "[]") if not wanted else text[:start]
        return head + body + text[end:]

    _edit_stacks(stacks_file, var, edit, wanted)


def _write_backup_config(path: Path, repository: str) -> None:
    creds = "".join(f'  {k}: "{{{{ vault_{v} }}}}"\n' for k, v in BACKUP_VARS.items())
    path.write_text(
        "# Sauvegardes restic des stacks (README de mcp-admin, « Sauvegardes »), écrit par\n"
        "# infra-mcp vault. Les valeurs sont chiffrées dans vault.yml du même dossier.\n"
        f"backup_repository: {json.dumps(repository)}\n"
        f"backup_credentials:\n{creds}"
    )


def apply(repo: VaultRepo, changes: list[Change]) -> list[str]:
    """Écrit tous les changements (déjà vérifiés). Renvoie ce qui a été fait ; en cas d'erreur,
    les fichiers sont remis comme avant."""
    saved = snapshot(repo)
    data = dict(repo.vault)
    done = []
    try:
        for c in changes:
            done.append(repo.describe(c))
            if c.kind in ("secret", "variable"):
                data[f"vault_{c.name}"] = c.value
                if c.kind == "secret":
                    declare(repo.stacks_file, c.name)
            elif c.kind == "generated":
                if c.name not in repo.generated:
                    _list_change(repo.stacks_file, "swarm_secrets_generated", c.name, add=True)
            elif c.kind == "registry":
                data[registry_key(c.name)] = c.value
                if c.name not in repo.registries:
                    _declare_registry(repo.stacks_file, c.name, c.username)
            elif c.kind == "backup_config":
                _write_backup_config(repo.vars_dir / BACKUP_FILE, c.name)
            elif c.kind == "delete_registry":
                _remove_entry(repo.stacks_file, "swarm_registries", c.name)
                data.pop(registry_key(c.name), None)
            elif c.kind == "delete":
                if c.name in repo.generated:
                    _list_change(repo.stacks_file, "swarm_secrets_generated", c.name, add=False)
                else:
                    data.pop(f"vault_{c.name}", None)
                    if c.name in repo.secrets:
                        _remove_entry(repo.stacks_file, "swarm_secrets", c.name)
            # Les changements suivants voient l'état écrit (ex. deux registres, deux secrets).
            repo.config = (yaml.safe_load(repo.stacks_file.read_text()) if repo.stacks_file.exists() else None) or {}
        if data != repo.vault:
            write_vault(repo.vault_file, repo.password, data)
            repo.vault = data
    except BaseException:
        restore(saved)
        raise
    return done

"""infra-mcp secret set : ajoute ou remplace un secret du vault, dans un clone du repo de config.

À lancer sur le poste de l'admin, pas sur le contrôleur : la valeur est saisie
sans écho (ou lue sur l'entrée standard), chiffrée dans
group_vars/swarm_managers/vault.yml sous vault_<nom>, et le secret est déclaré
dans swarm_secrets (stacks.yml du même dossier). Il reste à le lister dans les
secrets de la stack qui l'utilise, puis à committer.
"""

import re
from pathlib import Path

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret

VARS_DIR = Path("inventory/group_vars/swarm_managers")
# vault_<nom> doit être un nom de variable Ansible : ni tiret ni point.
NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_HEADER_RE = re.compile(r"^swarm_secrets:[ \t]*(\{\})?[ \t]*(#.*)?$", re.M)


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


def declare(stacks_file: Path, name: str) -> bool:
    """Ajoute name à swarm_secrets dans stacks.yml, en gardant le reste du fichier. False s'il y est déjà."""
    text = stacks_file.read_text() if stacks_file.exists() else ""
    config = yaml.safe_load(text) or {}
    if name in (config.get("swarm_secrets") or {}):
        return False
    entry = f'  {name}: "{{{{ vault_{name} }}}}"\n'
    header = _HEADER_RE.search(text)
    if header:
        end = header.end() + 1 if header.end() < len(text) else header.end()
        line = "swarm_secrets:" + (f" {header.group(2)}" if header.group(2) else "") + "\n"
        text = text[: header.start()] + line + entry + text[end:]
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + "swarm_secrets:\n" + entry
    if (yaml.safe_load(text).get("swarm_secrets") or {}).get(name) != f"{{{{ vault_{name} }}}}":
        raise VaultError(f"{stacks_file} : swarm_secrets n'a pas pu être complété, ajoute la ligne à la main :\n{entry}")
    stacks_file.write_text(text)
    return True


def set_secret(repo: Path, name: str, value: str, password: str) -> str:
    """Chiffre value sous vault_<name> et déclare le secret. Renvoie ce qui a été fait."""
    if not NAME_RE.fullmatch(name):
        raise VaultError(f"{name} : nom invalide (minuscules, chiffres et _ ; commence par une lettre)")
    if not value:
        raise VaultError("valeur vide")
    vars_dir = repo / VARS_DIR
    if not vars_dir.is_dir():
        raise VaultError(f"{vars_dir} absent : lancer la commande à la racine du repo de config")
    stacks_file = vars_dir / "stacks.yml"
    config = (yaml.safe_load(stacks_file.read_text()) if stacks_file.exists() else None) or {}
    if name in (config.get("swarm_secrets_generated") or []):
        raise VaultError(f"{name} est dans swarm_secrets_generated : sa valeur est créée sur le serveur")
    vault_file = vars_dir / "vault.yml"
    data = read_vault(vault_file, password)
    key = f"vault_{name}"
    replaced = key in data
    data[key] = value
    write_vault(vault_file, password, data)
    declared = declare(stacks_file, name)
    done = [f"{vault_file.relative_to(repo)} : {key} {'remplacée' if replaced else 'ajoutée'}"]
    if declared:
        done.append(f"{stacks_file.relative_to(repo)} : {name} ajouté à swarm_secrets")
    if replaced:
        done.append("au prochain apply stacks : nouvelle version du secret, stacks qui l'utilisent redéployées")
    else:
        done.append(f"reste à lister {name} dans les secrets de la stack (swarm_stacks) et dans son compose (external: true)")
    return "\n".join(done)

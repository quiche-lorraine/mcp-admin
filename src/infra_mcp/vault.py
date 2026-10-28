"""infra-mcp secret set : ajoute ou remplace un secret du vault, dans un clone du repo de config.

À lancer sur le poste de l'admin, pas sur le contrôleur : la valeur est saisie
sans écho (ou lue sur l'entrée standard), chiffrée dans
group_vars/swarm_managers/vault.yml sous vault_<nom>, et le secret est déclaré
dans swarm_secrets (stacks.yml du même dossier). Il reste à le lister dans les
secrets de la stack qui l'utilise, puis à committer.

infra-mcp registry set : même chose pour le token d'un registre privé (ghcr.io…),
chiffré sous vault_registry_<registre> et déclaré dans swarm_registries.
"""

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


def set_registry(repo: Path, registry: str, username: str, token: str, password: str) -> str:
    """Chiffre le token sous vault_registry_<registre> et déclare le registre. Renvoie ce qui a été fait."""
    if not REGISTRY_RE.fullmatch(registry):
        raise VaultError(f"{registry} : nom d'hôte de registre invalide (ex. ghcr.io)")
    if not username or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]*", username):
        raise VaultError(f"{username!r} : username invalide")
    vars_dir = _vars_dir(repo)
    stacks_file = vars_dir / "stacks.yml"
    key = "vault_registry_" + re.sub(r"[^a-z0-9]", "_", registry)
    wanted = {"username": username, "password": f"{{{{ {key} }}}}"}
    text = stacks_file.read_text() if stacks_file.exists() else ""
    current = ((yaml.safe_load(text) or {}).get("swarm_registries") or {}).get(registry)
    entry = f"  {registry}:\n    username: {username}\n    password: \"{wanted['password']}\"\n"
    if current is not None and current != wanted:
        raise VaultError(f"{stacks_file} : {registry} est déjà dans swarm_registries, autrement ; "
                         f"corrige l'entrée à la main :\n{entry}")
    replaced = _store(vars_dir, key, token, password)
    vault_rel = (vars_dir / "vault.yml").relative_to(repo)
    done = [f"{vault_rel} : {key} {'remplacée' if replaced else 'ajoutée'}"]
    if current is None:
        _insert(stacks_file, "swarm_registries", entry)
        if (yaml.safe_load(stacks_file.read_text()).get("swarm_registries") or {}).get(registry) != wanted:
            stacks_file.write_text(text)
            raise VaultError(f"{stacks_file} : swarm_registries n'a pas pu être complété, ajoute à la main :\n{entry}")
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

"""Garde-fous d'un repo de config infra-mcp : infra-mcp lint <dossier ansible>.

Ces règles protègent des promesses de l'outil ; elles vivent donc avec lui et non
dans le repo de config, où on pourrait les retirer en même temps qu'on les enfreint.

- plan reste une simulation : ni check_mode: false, ni lookup pipe (dans le repo
  de config comme dans la collection infra_mcp.core) ;
- le repo de config n'apporte que des données : ni code, ni tâche sur le
  contrôleur, ni lecture de ses fichiers (voir data_only_violations) ;
- secrets : fichiers vault nommés vault.yml, variables vault_* utilisées seulement
  par swarm_secrets ;
- pas de group_vars/x.yml à côté de group_vars/x/ (Ansible ignore alors le fichier) ;
- stacks : politique de sécurité des compose et cohérence avec l'inventaire.
"""

import configparser
import os
import re
from pathlib import Path

import yaml

COLLECTION_DIR = Path(__file__).resolve().parent.parent / "ansible_collections" / "infra_mcp" / "core"

# --- plan reste une simulation ---------------------------------------------------

_PIPE_RE = re.compile(r"""(lookup|query|q)\(\s*['"](ansible\.builtin\.)?pipe['"]|with_pipe\b""")


class _Loader(yaml.SafeLoader):
    """Accepte les tags Ansible (!vault, !unsafe…) sans les interpréter."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _check_mode_violations(node, where="tâche sans nom"):
    if isinstance(node, dict):
        where = f"« {node['name']} »" if "name" in node else where
        for key, value in node.items():
            if key == "check_mode" and value is not True:
                yield f"{where} : check_mode: {value!r}"
            yield from _check_mode_violations(value, where)
    elif isinstance(node, list):
        for item in node:
            yield from _check_mode_violations(item, where)


def real_execution_violations(path: Path) -> list[str]:
    text = path.read_text()
    found = list(_check_mode_violations(yaml.load(text, Loader=_Loader)))
    found += [f"ligne {text[:m.start()].count(chr(10)) + 1} : lookup pipe" for m in _PIPE_RE.finditer(text)]
    return found


# --- Le repo de config n'apporte que des données --------------------------------
# plan (token read) exécute les playbooks du repo sur le contrôleur, qui détient la
# clé SSH du parc et le mot de passe vault. Le repo ne doit donc pas pouvoir y faire
# exécuter du code (plugin, script, ProxyCommand…) ni lire ses fichiers (lookup,
# tâche déléguée au contrôleur, src absolu) puis les afficher dans la sortie.
# Défense en profondeur : la relecture des PR reste la vraie barrière, le repo de
# config vaut root sur le parc.

_SKIP_DIRS = {".git", ".ansible", "__pycache__"}
_PLUGIN_DIRS = {"library", "module_utils", "collections", "plugins"}
_SAFE_LOOKUPS = {
    "vars", "varnames", "template", "items", "list", "dict", "indexed_items",
    "subelements", "nested", "together", "sequence",
}
_LOOKUP_RE = re.compile(r"""\b(?:lookup|query|q)\(\s*['"]([\w.]+)['"]""")
_WITH_RE = re.compile(r"\bwith_(\w+)\s*:")
_CONTROLLER_KEYS = {"delegate_to", "local_action", "connection", "fetch", "ansible.builtin.fetch"}
_PATH_KEYS = {"src", "file", "include_vars", "ansible.builtin.include_vars", "vars_files"}
_CONNECTION_VAR_RE = re.compile(
    r"^ansible_(connection|ssh_common_args|ssh_extra_args|ssh_args|scp_extra_args|sftp_extra_args"
    r"|ssh_executable|shell_executable|become_exe|become_flags|\w+_interpreter)$"
)
_SAFE_INTERPRETER_RE = re.compile(r"^(auto|auto_silent|auto_legacy|auto_legacy_silent|/[\w./+-]+)$")
_CFG_ALLOWED = {
    "defaults": {
        "inventory", "roles_path", "host_key_checking", "interpreter_python", "stdout_callback",
        "callback_result_format", "forks", "timeout", "gathering", "retry_files_enabled",
        "display_skipped_hosts", "deprecation_warnings", "any_errors_fatal", "force_handlers",
    },
    "ssh_connection": {"pipelining", "retries", "transfer_method"},
    "privilege_escalation": {"become", "become_method", "become_user"},
}


def _repo_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for d in dirnames:
            yield Path(dirpath) / d, True
        for f in sorted(filenames):
            yield Path(dirpath) / f, False


def _is_vault_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(14) == b"$ANSIBLE_VAULT"
    except OSError:
        return False


def _data_violations(node, path_keys_ok=True):
    """Clés et valeurs interdites dans un YAML du repo de config."""
    if isinstance(node, dict):
        for key, value in node.items():
            key = str(key)
            if key in _CONTROLLER_KEYS:
                yield f"{key} : interdit (exécution sur le contrôleur)"
            # hosts d'un play (texte ou liste), pas le mapping hosts: de l'inventaire,
            # où une machine peut légitimement avoir l'adresse 127.0.0.1.
            if key == "hosts" and isinstance(value, (str, list)):
                if any(h in str(value) for h in ("localhost", "127.0.0.1")):
                    yield "hosts: localhost : interdit (exécution sur le contrôleur)"
            if _CONNECTION_VAR_RE.match(key):
                if not (key.endswith("_interpreter") and _SAFE_INTERPRETER_RE.match(str(value))):
                    yield f"{key} : interdit (agit sur la connexion, permet d'exécuter des commandes)"
            if key in _PATH_KEYS:
                for v in value if isinstance(value, list) else [value]:
                    if isinstance(v, str) and (v.startswith(("/", "~")) or ".." in v):
                        yield f"{key}: {v} : chemin hors du repo interdit"
            yield from _data_violations(value)
    elif isinstance(node, list):
        for item in node:
            yield from _data_violations(item)


def _vault_refs(node, top=True):
    """Références à vault_* ailleurs que dans swarm_secrets."""
    if isinstance(node, dict):
        for key, value in node.items():
            if top and key == "swarm_secrets":
                continue
            yield from _vault_refs(value, top=False)
    elif isinstance(node, list):
        for item in node:
            yield from _vault_refs(item, top=False)
    elif isinstance(node, str) and "vault_" in node:
        yield node


def data_only_violations(config_dir: Path) -> list[str]:
    found = []
    for path, is_dir in _repo_files(config_dir):
        rel = path.relative_to(config_dir)
        if is_dir:
            if path.name in _PLUGIN_DIRS or path.name.endswith("_plugins"):
                found.append(f"{rel}/ : dossier de plugins ou de modules interdit")
            continue
        if path.suffix == ".py":
            found.append(f"{rel} : fichier Python interdit (exécuté sur le contrôleur)")
        if os.access(path, os.X_OK):
            found.append(f"{rel} : fichier exécutable interdit (inventaire script, outil…)")
        if path.suffix not in (".yml", ".yaml"):
            continue
        if _is_vault_file(path):
            if path.name != "vault.yml":
                found.append(f"{rel} : fichier chiffré à nommer vault.yml (ses variables vault_* sont masquées)")
            continue
        text = path.read_text()
        if re.search(r"!vault\b", text):
            found.append(f"{rel} : valeur !vault en ligne interdite, utilise un fichier vault.yml")
        for m in _LOOKUP_RE.finditer(text):
            if m.group(1).removeprefix("ansible.builtin.") not in _SAFE_LOOKUPS:
                found.append(f"{rel} : lookup {m.group(1)} interdit (lit ou exécute sur le contrôleur)")
        for m in _WITH_RE.finditer(text):
            if m.group(1) not in _SAFE_LOOKUPS:
                found.append(f"{rel} : with_{m.group(1)} interdit (lookup sur le contrôleur)")
        data = yaml.load(text, Loader=_Loader)
        found += [f"{rel} : {v}" for v in _data_violations(data)]
        if ".github" not in rel.parts:
            found += [f"{rel} : {v!r} : vault_* n'est utilisable que dans swarm_secrets (sinon il apparaît dans la sortie de plan)"
                      for v in _vault_refs(data)]
    cfg_path = config_dir / "ansible.cfg"
    if cfg_path.exists():
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(cfg_path)
        for section in cfg.sections():
            for key in cfg[section]:
                if key not in _CFG_ALLOWED.get(section, set()):
                    found.append(f"ansible.cfg : [{section}] {key} non autorisé")
    return found


# --- Disposition des variables ---------------------------------------------------


def vars_layout_violations(ansible_dir: Path) -> list[str]:
    found = []
    for kind in ("group_vars", "host_vars"):
        base = ansible_dir / "inventory" / kind
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and (base / f"{d.name}.yml").exists():
                    found.append(f"{kind}/{d.name}.yml et {kind}/{d.name}/ ensemble : Ansible ignore le fichier")
    return found


# --- Stacks ----------------------------------------------------------------------

EXCEPTIONS = {
    "privileged", "host-network", "host-pid", "host-ipc", "cap-add", "devices",
    "security-opt", "docker-socket", "host-path",
}
DOCKER_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}
SENSITIVE = ("/etc", "/root", "/home", "/proc", "/sys", "/boot", "/dev", "/run", "/var/run", "/var/lib/docker")
HARMLESS = {"/etc/localtime", "/etc/timezone"}  # lecture seule, courants pour l'heure
# Réseaux partagés entre stacks (swarm_networks) : créés et chiffrés par le socle.
NETWORK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RESERVED_NETWORKS = {"bridge", "host", "none", "ingress", "docker_gwbridge"}


def _volume_host_path(definition):
    """Un volume nommé du driver local avec o=bind (ou type=none) monte un chemin de l'hôte."""
    opts = (definition or {}).get("driver_opts") or {}
    if "bind" in str(opts.get("o", "")).split(",") or str(opts.get("type", "")) == "none":
        return str(opts.get("device", ""))
    return None


def _bind_sources(service, volumes=None):
    volumes = volumes or {}
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0].startswith(("/", ".", "~")):
                yield parts[0], "ro" in parts[2:3]
            elif len(parts) >= 2 and (host := _volume_host_path(volumes.get(parts[0]))) is not None:
                yield host, "ro" in parts[2:3]
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            yield volume.get("source", ""), bool(volume.get("read_only"))
        elif isinstance(volume, dict) and (host := _volume_host_path(volumes.get(volume.get("source")))) is not None:
            yield host, bool(volume.get("read_only"))


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _image_problem(image):
    if not image:
        return "image manquante"
    if "@sha256:" in image:
        return None
    name = image.rsplit("/", 1)[-1]
    if ":" not in name:
        return f"image sans tag : {image} (tag explicite obligatoire)"
    if name.endswith(":latest"):
        return f"image en latest : {image} (tag explicite obligatoire)"
    return None


def compose_violations(compose: dict, shared_networks=()) -> list[str]:
    found = []
    services = compose.get("services") or {}
    if not services:
        found.append("aucun service")
    # docker stack deploy remplace $VAR / ${VAR} par l'environnement root du manager :
    # « ${HOME}:/data » monte /root. Seul $$ (un $ littéral) est accepté.
    for value in sorted({v for v in _strings(compose) if re.search(r"(?<!\$)\$(?!\$)", v.replace("$$", ""))}):
        found.append(f"interpolation interdite : {value!r} (évaluée avec l'environnement root du manager)")
    # Une stack crée ses réseaux (chiffrés par le socle) ; seuls les réseaux partagés,
    # créés eux aussi par le socle, peuvent être external.
    for net, definition in (compose.get("networks") or {}).items():
        external = (definition or {}).get("external")
        if external in (None, False):
            continue
        if external is not True or "name" in definition:
            found.append(f"réseau {net} : external: true, sans name: (un réseau partagé porte le nom de sa clé)")
        elif net not in shared_networks:
            found.append(f"réseau {net} : external interdit hors des réseaux partagés de swarm_networks"
                         " (les autres réseaux d'une stack sont créés par elle, et chiffrés)")
    for name, svc in services.items():
        allowed = svc.get("x-allow") or {}
        if not isinstance(allowed, dict) or not all(isinstance(r, str) and r.strip() for r in allowed.values()):
            found.append(f"{name} : x-allow doit associer chaque exception à sa raison")
            allowed = {}
        for rule in set(allowed) - EXCEPTIONS:
            found.append(f"{name} : exception inconnue {rule} (connues : {', '.join(sorted(EXCEPTIONS))})")

        def check(rule, condition, message):
            if condition and rule not in allowed:
                found.append(f"{name} : {message} (exception : x-allow: {{{rule}: \"raison\"}})")

        if problem := _image_problem(svc.get("image")):
            found.append(f"{name} : {problem}")
        networks = svc.get("networks") or []
        check("privileged", svc.get("privileged") is True, "privileged")
        check("host-network", svc.get("network_mode") == "host" or "host" in networks, "réseau de l'hôte")
        check("host-pid", svc.get("pid") == "host", "pid de l'hôte")
        check("host-ipc", svc.get("ipc") == "host", "ipc de l'hôte")
        check("cap-add", bool(svc.get("cap_add")), "cap_add")
        check("devices", bool(svc.get("devices")), "devices")
        check("security-opt", any("unconfined" in str(o) for o in svc.get("security_opt") or []), "security_opt unconfined")
        for source, read_only in _bind_sources(svc, compose.get("volumes") or {}):
            path = source.rstrip("/") or "/"
            if not path.startswith("/"):
                found.append(f"{name} : montage relatif {source} (le compose est déployé hors du repo : volume nommé ou chemin absolu)")
            elif path in DOCKER_SOCKETS:
                check("docker-socket", True, f"montage du socket Docker {source}")
            elif path in HARMLESS and read_only:
                continue
            elif path == "/" or path.startswith(SENSITIVE):
                check("host-path", True, f"montage d'un chemin sensible de l'hôte {source}")
        labels = (svc.get("deploy") or {}).get("labels")
        if labels is not None and not isinstance(labels, dict):
            found.append(f"{name} : deploy.labels doit être un mapping (le playbook y ajoute infra.stack-hash)")
    for kind in ("secrets", "configs"):
        for key, definition in (compose.get(kind) or {}).items():
            if kind == "secrets" and not (definition or {}).get("external"):
                found.append(f"secret {key} : doit être external (créé par le playbook depuis le vault)")
            if kind == "configs" and "file" in (definition or {}):
                found.append(f"config {key} : file: n'est pas encore pris en charge (le compose est déployé hors du repo)")
    return found


def _stacks_config(ansible_dir: Path) -> dict:
    path = ansible_dir / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def _shared_networks(config: dict) -> list:
    networks = config.get("swarm_networks") or []
    return networks if isinstance(networks, list) else []


def stacks_inventory_violations(ansible_dir: Path) -> list[str]:
    config = _stacks_config(ansible_dir)
    if not config:
        return []
    found = []
    if not isinstance(config.get("swarm_networks") or [], list):
        found.append("swarm_networks : liste de noms de réseaux attendue")
    for net in _shared_networks(config):
        if not isinstance(net, str) or not NETWORK_NAME_RE.fullmatch(net) or net in RESERVED_NETWORKS:
            found.append(f"swarm_networks : nom invalide ou réservé : {net!r}")
    declared = set((config.get("swarm_secrets") or {}).keys())
    names = [s["name"] for s in config.get("swarm_stacks") or []]
    found += [f"stack {n} : dans swarm_stacks et swarm_stacks_absent" for n in set(names) & set(config.get("swarm_stacks_absent") or [])]
    for stack in config.get("swarm_stacks") or []:
        compose_path = ansible_dir / "stacks" / stack["name"] / "compose.yml"
        if not compose_path.is_file():
            found.append(f"stack {stack['name']} : {compose_path} absent")
            continue
        used = set((yaml.safe_load(compose_path.read_text()).get("secrets") or {}).keys())
        listed = set(stack.get("secrets") or [])
        if used - listed:
            found.append(f"stack {stack['name']} : secrets du compose absents de swarm_stacks : {sorted(used - listed)}")
        if listed - declared:
            found.append(f"stack {stack['name']} : secrets absents de swarm_secrets : {sorted(listed - declared)}")
    return found


# --- Ensemble --------------------------------------------------------------------


def run(ansible_dir: Path) -> list[str]:
    """Tous les problèmes, préfixés par le fichier concerné."""
    ansible_dir = Path(ansible_dir)
    problems = []
    for root in (ansible_dir, COLLECTION_DIR):
        for path in sorted(root.rglob("*.yml")):
            problems += [f"{path} : {v}" for v in real_execution_violations(path)]
    problems += [f"{ansible_dir} : {v}" for v in data_only_violations(ansible_dir)]
    problems += vars_layout_violations(ansible_dir)
    shared = _shared_networks(_stacks_config(ansible_dir))
    for path in sorted((ansible_dir / "stacks").glob("*/compose.yml")):
        problems += [f"{path} : {v}" for v in compose_violations(yaml.safe_load(path.read_text()), shared)]
    problems += stacks_inventory_violations(ansible_dir)
    return problems

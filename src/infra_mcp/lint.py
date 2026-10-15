"""Garde-fous d'un repo de config infra-mcp : infra-mcp lint <dossier ansible>.

Ces règles protègent des promesses de l'outil ; elles vivent donc avec lui et non
dans le repo de config, où on pourrait les retirer en même temps qu'on les enfreint.

- plan reste une simulation : ni check_mode: false, ni lookup pipe (dans le repo
  de config comme dans la collection infra_mcp.core) ;
- pas de group_vars/x.yml à côté de group_vars/x/ (Ansible ignore alors le fichier) ;
- stacks : politique de sécurité des compose et cohérence avec l'inventaire.
"""

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


def _bind_sources(service):
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0].startswith(("/", ".", "~")):
                yield parts[0], "ro" in parts[2:3]
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            yield volume.get("source", ""), bool(volume.get("read_only"))


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


def compose_violations(compose: dict) -> list[str]:
    found = []
    services = compose.get("services") or {}
    if not services:
        found.append("aucun service")
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
        for source, read_only in _bind_sources(svc):
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


def stacks_inventory_violations(ansible_dir: Path) -> list[str]:
    path = ansible_dir / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    if not path.exists():
        return []
    config = yaml.safe_load(path.read_text()) or {}
    declared = set((config.get("swarm_secrets") or {}).keys())
    names = [s["name"] for s in config.get("swarm_stacks") or []]
    found = [f"stack {n} : dans swarm_stacks et swarm_stacks_absent" for n in set(names) & set(config.get("swarm_stacks_absent") or [])]
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
    problems += vars_layout_violations(ansible_dir)
    for path in sorted((ansible_dir / "stacks").glob("*/compose.yml")):
        problems += [f"{path} : {v}" for v in compose_violations(yaml.safe_load(path.read_text()))]
    problems += stacks_inventory_violations(ansible_dir)
    return problems

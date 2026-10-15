"""Traefik devant infra-mcp : repérage d'une instance existante, route dynamique.

Appelé par infra-mcp-admin (python -I -m infra_mcp.traefik ...). Les commandes de
repérage écrivent des lignes clé=valeur, que le script lit sans les évaluer.

  docker              < sortie de « docker inspect <conteneur> »
  process <pid>       Traefik lancé sur la machine elle-même
  route <domaine> <url du service> [--entrypoint N] [--resolver N]
  check-domain <domaine>
"""

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath

import yaml

DOMAIN_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
BACKEND_RE = re.compile(r"http://(?:[0-9.]+|\[[0-9a-f:]+\]):[0-9]{1,5}")
# Emplacements où Traefik cherche sa configuration statique sans --configFile.
DEFAULT_CONFIGS = [f"/etc/traefik/traefik.{ext}" for ext in ("toml", "yaml", "yml")]
ROUTE_NAME = "infra-mcp"


# --- Configuration statique -------------------------------------------------------


def _flatten(data, prefix=""):
    """{"a": {"b": 1}, "c": {}} -> {"a.b": 1, "c": {}} ; les noms gardent leur casse."""
    out = {}
    if isinstance(data, dict) and data:
        for key, value in data.items():
            out.update(_flatten(value, f"{prefix}{key}."))
    elif prefix:
        out[prefix[:-1]] = data
    return out


def parse_args(args: list[str]) -> dict:
    """Options de la ligne de commande : --a.b=c, --a.b c, --flag."""
    flat = {}
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg.startswith("--"):
            continue
        key, sep, value = arg[2:].partition("=")
        if not sep:
            if i < len(args) and not args[i].startswith("--"):
                value = args[i]
                i += 1
            else:
                value = "true"
        flat[key] = value
    return flat


def parse_env(env: list[str]) -> dict:
    """TRAEFIK_PROVIDERS_FILE_DIRECTORY=/x -> providers.file.directory."""
    flat = {}
    for item in env:
        key, _, value = item.partition("=")
        if key.startswith("TRAEFIK_"):
            flat[key[len("TRAEFIK_") :].replace("_", ".").lower()] = value
    return flat


def parse_config(path: Path) -> dict:
    text = path.read_text()
    data = tomllib.loads(text) if path.suffix == ".toml" else yaml.safe_load(text)
    return _flatten(data or {})


def _get(flat: dict, key: str):
    key = key.lower()
    for k, v in flat.items():
        if k.lower() == key:
            return v
    return None


def config_path(args: list[str]) -> str | None:
    return _get(parse_args(args), "configFile")


def static_facts(flat: dict) -> dict:
    """Ce qu'il faut savoir pour y brancher infra-mcp."""
    facts = {
        "file_dir": _get(flat, "providers.file.directory") or "",
        "file_name": _get(flat, "providers.file.filename") or "",
        "entrypoint": "",
        "resolver": "",
    }
    for key, value in sorted(flat.items()):
        m = re.fullmatch(r"(?i)entrypoints\.([^.]+)\.address", key)
        if m and re.search(r":443(/tcp)?$", str(value)):
            facts["entrypoint"] = m.group(1)
            break
    resolvers = sorted(
        {m.group(1) for k in flat if (m := re.fullmatch(r"(?i)certificatesresolvers\.([^.]+)\..+", k))}
    )
    preferred = facts["entrypoint"] and _get(flat, f"entryPoints.{facts['entrypoint']}.http.tls.certResolver")
    facts["resolver"] = preferred if preferred in resolvers else (resolvers[0] if resolvers else "")
    return facts


def load_static(args: list[str], env: list[str], read) -> tuple[dict, str]:
    """Configuration statique effective et sa source.

    Traefik n'en combine pas plusieurs : un fichier (--configFile, ou à défaut
    un emplacement standard) l'emporte sur les options, qui l'emportent sur les
    variables d'environnement. `read(chemin)` renvoie le fichier lisible
    correspondant (Path) ou None."""
    candidates = [config_path(args)] if config_path(args) else DEFAULT_CONFIGS
    for candidate in candidates:
        path = read(candidate)
        if path is not None:
            return parse_config(path), str(path)
    flat = parse_args(args)
    flat.pop("configFile", None)
    if flat:
        return flat, "options de la ligne de commande"
    return parse_env(env), "variables d'environnement"


# --- Où tourne Traefik ------------------------------------------------------------


def host_path(path: str, mounts: list[tuple[str, str]]) -> str | None:
    """Chemin vu par le conteneur -> chemin sur la machine (montage le plus précis)."""
    p = PurePosixPath(path)
    best = None
    for dest, source in mounts:
        d = PurePosixPath(dest)
        if (p == d or d in p.parents) and (best is None or len(d.parts) > len(best[0].parts)):
            best = (d, source)
    if best is None:
        return None
    return str(PurePosixPath(best[1]) / p.relative_to(best[0]))


def docker_facts(inspect: dict) -> dict:
    mounts = [(m["Destination"], m["Source"]) for m in inspect.get("Mounts") or [] if m.get("Source")]

    def read(container_path):
        on_host = host_path(container_path, mounts)
        return Path(on_host) if on_host and Path(on_host).is_file() else None

    args = [inspect.get("Path", ""), *(inspect.get("Args") or [])]
    flat, source = load_static(args, inspect.get("Config", {}).get("Env") or [], read)
    facts = static_facts(flat)
    facts["source"] = source
    for key in ("file_dir", "file_name"):
        facts[key] = (host_path(facts[key], mounts) or "") if facts[key] else ""

    # Adresse de la machine joignable depuis le conteneur : la passerelle de son
    # réseau (ou la boucle locale s'il partage le réseau de la machine).
    facts["backend_host"] = facts["client_subnet"] = ""
    if inspect.get("HostConfig", {}).get("NetworkMode") == "host":
        facts["backend_host"] = "127.0.0.1"
    else:
        for _, net in sorted((inspect.get("NetworkSettings", {}).get("Networks") or {}).items()):
            if net.get("Gateway") and net.get("IPAddress") and net.get("IPPrefixLen"):
                facts["backend_host"] = str(ipaddress.ip_address(net["Gateway"]))
                facts["client_subnet"] = str(
                    ipaddress.ip_interface(f"{net['IPAddress']}/{net['IPPrefixLen']}").network
                )
                break
    return facts


def process_facts(pid: int, proc: Path = Path("/proc")) -> dict:
    base = proc / str(pid)
    args = (base / "cmdline").read_bytes().decode(errors="replace").split("\0")
    env = (base / "environ").read_bytes().decode(errors="replace").split("\0")

    def read(path):
        p = Path(path)
        return p if p.is_file() else None

    flat, source = load_static(args, env, read)
    facts = static_facts(flat)
    facts.update(source=source, backend_host="127.0.0.1", client_subnet="")
    return facts


# --- Route dynamique --------------------------------------------------------------


def route(domain: str, backend: str, entrypoint: str = "", resolver: str = "") -> str:
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"nom de domaine invalide : {domain!r}")
    if not BACKEND_RE.fullmatch(backend):
        raise ValueError(f"adresse du service invalide : {backend!r}")
    for name in (entrypoint, resolver):
        if name and not NAME_RE.fullmatch(name):
            raise ValueError(f"nom Traefik invalide : {name!r}")
    router = {"rule": f"Host(`{domain}`)", "service": ROUTE_NAME, "tls": {}}
    if entrypoint:
        router["entryPoints"] = [entrypoint]
    if resolver:
        router["tls"] = {"certResolver": resolver}
    config = {
        "http": {
            "routers": {ROUTE_NAME: router},
            "services": {ROUTE_NAME: {"loadBalancer": {"servers": [{"url": backend}]}}},
        }
    }
    header = (
        f"# Route vers infra-mcp, écrite par infra-mcp-admin pour {domain}.\n"
        "# Défense en profondeur possible : n'accepter que certaines IP (middleware ipAllowList).\n"
    )
    return header + yaml.safe_dump(config, sort_keys=False)


# --- Ligne de commande ------------------------------------------------------------


def _print_facts(facts: dict) -> None:
    for key, value in facts.items():
        print(f"{key}={str(value).replace(chr(10), ' ')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infra_mcp.traefik")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("docker")
    proc = sub.add_parser("process")
    proc.add_argument("pid", type=int)
    rt = sub.add_parser("route")
    rt.add_argument("domain")
    rt.add_argument("backend")
    rt.add_argument("--entrypoint", default="")
    rt.add_argument("--resolver", default="")
    chk = sub.add_parser("check-domain")
    chk.add_argument("domain")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "docker":
            inspect = json.load(sys.stdin)
            _print_facts(docker_facts(inspect[0] if isinstance(inspect, list) else inspect))
        elif args.cmd == "process":
            _print_facts(process_facts(args.pid))
        elif args.cmd == "route":
            sys.stdout.write(route(args.domain, args.backend, args.entrypoint, args.resolver))
        elif args.cmd == "check-domain":
            return 0 if DOMAIN_RE.fullmatch(args.domain) else 1
    except (OSError, ValueError, yaml.YAMLError, tomllib.TOMLDecodeError, KeyError) as e:
        print(f"erreur : {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

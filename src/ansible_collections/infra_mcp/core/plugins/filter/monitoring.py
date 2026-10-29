"""Filtres du playbook maintenance_health : modèle de surveillance par stack et
lignes d'erreur des logs. En Python plutôt qu'en Jinja pour rester lisibles et
testables (tests/test_health.py)."""

import re

KEEP = 20
STACK_KEYS = {"enabled", "logs", "error_pattern", "ignore", "services"}
SERVICE_KEYS = {"logs", "error_pattern", "ignore"}


def _ignore(*lists):
    return [p for lst in lists for p in (lst or [])]


def monitoring_stacks(stacks):
    """Entrées de swarm_stacks surveillées (sans monitoring.enabled: false)."""
    return [s for s in stacks or [] if (s.get("monitoring") or {}).get("enabled", True)]


def monitoring_services(stack_results, error_pattern, ignore):
    """Résultats de docker_stack_task_info (bouclé sur les entrées de swarm_stacks)
    -> services dont on lit les logs, avec leur expression d'erreur et leurs lignes
    ignorées : le service surcharge la stack, qui surcharge l'hôte ; les listes
    ignore s'additionnent."""
    targets = []
    for res in stack_results:
        stack = res["item"]
        conf = stack.get("monitoring") or {}
        names = sorted({re.sub(r"\.[^.]+$", "", t.get("Name", "")) for t in res.get("results") or []})
        for name in names:
            short = name[len(stack["name"]) + 1:]
            svc = (conf.get("services") or {}).get(short) or {}
            if not svc.get("logs", conf.get("logs", True)):
                continue
            targets.append({
                "name": name,
                "error_pattern": svc.get("error_pattern") or conf.get("error_pattern") or error_pattern,
                "ignore": _ignore(ignore, conf.get("ignore"), svc.get("ignore")),
            })
    return targets


def error_lines(lines, error_pattern, ignore=(), keep=KEEP):
    """Lignes qui correspondent à error_pattern et à aucune expression de ignore
    (insensible à la casse) -> nombre et dernières lignes."""
    match = re.compile(error_pattern, re.I)
    skip = [re.compile(p, re.I) for p in ignore or []]
    hits = [line for line in lines if match.search(line) and not any(s.search(line) for s in skip)]
    return {"count": len(hits), "last": hits[-keep:]}


def monitoring_findings(results, error_pattern, ignore):
    """Résultats d'une boucle de lecture de logs -> {source: {count, last}}.

    item est un nom d'unité (réglages de l'hôte) ou une cible de monitoring_services."""
    found = {}
    for res in results:
        item = res["item"]
        if isinstance(item, dict):
            name, pattern, skip = item["name"], item["error_pattern"], item["ignore"]
        else:
            name, pattern, skip = item, error_pattern, ignore
        lines = (res.get("stdout_lines") or []) + (res.get("stderr_lines") or [])
        if not lines:
            continue
        # Lignes horodatées : le tri remet stdout et stderr dans l'ordre.
        if re.match(r"^\d{4}-\d\d-\d\d", lines[0]):
            lines = sorted(lines)
        found[name] = error_lines(lines, pattern, skip)
    return found


class FilterModule:
    def filters(self):
        return {
            "monitoring_stacks": monitoring_stacks,
            "monitoring_services": monitoring_services,
            "monitoring_findings": monitoring_findings,
        }

"""Filtres du playbook maintenance_stack_autoupdate : services suivis, images à
tirer et rapport. En Python plutôt qu'en Jinja pour rester lisibles et testables
(tests/test_autoupdate.py)."""

import json

# Label de service : identifiant de l'image déployée par le dernier passage.
LABEL = "infra.autoupdate.image-id"


def autoupdate_services(stacks):
    """Entrées de swarm_stacks -> services Swarm (<stack>_<service>) listés dans
    leur clé auto_update."""
    return [f"{s['name']}_{svc}" for s in stacks or [] for svc in s.get("auto_update") or []]


def autoupdate_targets(inspect_results):
    """Résultats bouclés de docker service inspect --format '{{json .Spec}}' ->
    services existants, avec leur image (tag, sans digest) et l'identifiant
    d'image noté au dernier passage."""
    targets = []
    for r in inspect_results:
        if r.get("rc") != 0:
            continue
        spec = json.loads(r["stdout"])
        image = spec["TaskTemplate"]["ContainerSpec"]["Image"].partition("@")[0]
        targets.append({"service": r["item"], "image": image, "deployed": (spec.get("Labels") or {}).get(LABEL)})
    return targets


def autoupdate_report(inspect_results, targets, pull_results, id_results, update_results):
    """-> services absents, mis à jour, en échec, et image de chaque service.
    pull_results : docker pull bouclé sur les images ; id_results : docker image
    inspect bouclé sur les pulls réussis ; update_results : docker service update
    bouclé sur les cibles."""
    unpulled = {r["item"]: (r.get("stderr") or "image introuvable")[-500:] for r in pull_results if r.get("rc") != 0}
    ids = {r["item"]["item"]: r["stdout"] for r in id_results if r.get("rc") == 0}
    failed = {t["service"]: unpulled[t["image"]] for t in targets if t["image"] in unpulled}
    updated = []
    for r in update_results:
        if r.get("skipped"):
            continue
        name = r["item"]["service"]
        if r.get("rc") == 0:
            updated.append(name)
        else:
            failed[name] = (r.get("stderr") or r.get("stdout") or "")[-500:]
    return {
        "missing": [r["item"] for r in inspect_results if r.get("rc") != 0],
        "updated": updated,
        "failed": failed,
        "images": {t["service"]: f"{t['image']} ({ids.get(t['image'], '?')[:19]})" for t in targets},
    }


class FilterModule:
    def filters(self):
        return {
            "autoupdate_services": autoupdate_services,
            "autoupdate_targets": autoupdate_targets,
            "autoupdate_report": autoupdate_report,
        }

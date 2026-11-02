"""Filtres du playbook maintenance_stack_versions : rapport de ce qui tourne. En
Python plutôt qu'en Jinja pour rester lisibles et testables (tests/test_versions.py)."""

import json

OCI = "org.opencontainers.image."
# Labels OCI rapportés, sous leur nom court.
OCI_LABELS = ("version", "revision", "source", "created")
VERSION_LINES = 20


def _repository(ref):
    """ghcr.io/o/app:main@sha256:… -> ghcr.io/o/app (sans tag ni digest)."""
    name = ref.partition("@")[0]
    head, _, last = name.rpartition("/")
    return f"{head}/{last.partition(':')[0]}" if head else last.partition(":")[0]


def _by_item(results, key=lambda item: item):
    return {key(r["item"]): r for r in results or [] if not r.get("skipped")}


def versions_report(services, ps_results, inspect_results, image_results, exec_results):
    """-> par service Swarm : image, conteneur en cours, sortie de la commande de
    version. services : [{service, argv}] ; ps_results : docker ps bouclé sur les
    services ; inspect_results : docker inspect bouclé sur les conteneurs trouvés ;
    image_results : docker image inspect bouclé sur leurs images ; exec_results :
    commande de version bouclée sur les services qui en ont une."""
    containers = {s: (r.get("stdout_lines") or [None])[0] for s, r in _by_item(ps_results, lambda i: i["service"]).items()}
    inspects = {}
    for cid, r in _by_item(inspect_results).items():
        if r.get("rc") == 0:
            inspects[cid] = json.loads(r["stdout"])[0]
    images = {}
    for iid, r in _by_item(image_results).items():
        if r.get("rc") == 0:
            images[iid] = json.loads(r["stdout"])[0]
    execs = _by_item(exec_results, lambda i: i["service"])
    report = {}
    for svc in services:
        name = svc["service"]
        cid = containers.get(name)
        container = inspects.get(cid) if cid else None
        if container is None:
            report[name] = {"error": "aucun conteneur en cours sur ce nœud"}
            continue
        image = images.get(container.get("Image")) or {}
        labels = (image.get("Config") or {}).get("Labels") or {}
        ref = container["Config"]["Image"]
        digests = [d.partition("@") for d in image.get("RepoDigests") or []]
        entry = {
            "image": ref.partition("@")[0],
            "image_id": container.get("Image", "")[:19],
            "digest": next((d for r, _, d in digests if r == _repository(ref)), digests[0][2] if digests else None),
            "started": container["State"].get("StartedAt"),
            **{k: labels[OCI + k] for k in OCI_LABELS if labels.get(OCI + k)},
        }
        r = execs.get(name)
        if svc.get("argv") is None:
            entry["version"] = None
        elif r is not None:
            lines = (r.get("stdout_lines") or []) + (r.get("stderr_lines") or [])
            entry["version"] = lines[:VERSION_LINES]
            if r.get("rc") == 124:
                entry["version_error"] = "délai dépassé"
            elif r.get("rc") != 0:
                entry["version_error"] = f"code de sortie {r.get('rc')}"
        report[name] = entry
    return report


class FilterModule:
    def filters(self):
        return {"versions_report": versions_report}

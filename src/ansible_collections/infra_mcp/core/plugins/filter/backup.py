"""Filtres des playbooks de sauvegarde (maintenance_backup, maintenance_backup_list,
maintenance_health) : résultats de restic, fichier d'état et alertes. En Python
plutôt qu'en Jinja pour rester lisibles et testables (tests/test_backup.py)."""

import json
from datetime import datetime, timezone


def _summary(stdout):
    """Dernière ligne « summary » de restic backup --json."""
    for line in reversed((stdout or "").splitlines()):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("message_type") == "summary":
            return msg
    return {}


def _error(result, default="échec"):
    text = "\n".join(filter(None, [result.get("stderr"), result.get("msg")])).strip()
    return text[-500:] or default


def backup_expected(stacks):
    """Entrées de swarm_stacks -> stacks qui ont quelque chose à sauvegarder."""
    return sorted(s["name"] for s in stacks or []
                  if isinstance(s, dict) and isinstance(s.get("backup"), dict)
                  and (s["backup"].get("dumps") or s["backup"].get("volumes")))


def backup_items(plan, ps_results, volume_results):
    """Plan de la sauvegarde : un élément par dump ou volume, avec ce qu'il faut à
    restic (conteneur du service, point de montage du volume) ou son erreur.
    plan : {dumps: [{stack, service, argv}], volumes: [{stack, volume}]} ;
    ps_results : docker ps bouclé sur les dumps ; volume_results : docker volume
    inspect bouclé sur les volumes."""
    containers = {(r["item"]["stack"], r["item"]["service"]): (r.get("stdout_lines") or [None])[0]
                  for r in ps_results or [] if not r.get("skipped")}
    mounts = {r["item"]["volume"]: json.loads(r["stdout"])[0]["Mountpoint"]
              for r in volume_results or [] if not r.get("skipped") and r.get("rc") == 0}
    items = []
    for d in plan.get("dumps") or []:
        cid = containers.get((d["stack"], d["service"]))
        item = {"stack": d["stack"], "kind": "dump", "name": d["service"], "argv": d["argv"],
                "filename": f"{d['stack']}/{d['service']}.dump"}
        items.append({**item, "container": cid} if cid else {**item, "error": "aucun conteneur en cours"})
    for v in plan.get("volumes") or []:
        path = mounts.get(v["volume"])
        item = {"stack": v["stack"], "kind": "volume", "name": v["volume"]}
        items.append({**item, "path": path} if path else {**item, "error": "volume introuvable"})
    return items


def backup_results(items, dump_results, volume_results):
    """-> par stack : ok, et pour chaque élément : instantané et taille, ou erreur."""
    done = {}
    for r in list(dump_results or []) + list(volume_results or []):
        if r.get("skipped"):
            continue
        it = r["item"]
        key = (it["stack"], it["kind"], it["name"])
        if r.get("rc") == 0:
            s = _summary(r.get("stdout"))
            done[key] = {"snapshot": s.get("snapshot_id", "")[:8], "bytes": s.get("total_bytes_processed"),
                         "added_bytes": s.get("data_added")}
        else:
            done[key] = {"error": _error(r)}
    report = {}
    for it in items:
        entry = report.setdefault(it["stack"], {"ok": True, "items": {}})
        res = {"error": it["error"]} if "error" in it else done.get((it["stack"], it["kind"], it["name"]), {"error": "non lancé"})
        entry["items"][f"{it['kind']}:{it['name']}"] = res
        if "error" in res:
            entry["ok"] = False
    return report


def backup_status(previous, report, finished, check=None):
    """Fichier d'état mis à jour : pour chaque stack, la dernière tentative et la
    dernière réussite ; le dernier restic check. previous : contenu du fichier
    (texte, éventuellement vide ou illisible)."""
    try:
        status = json.loads(previous) if previous else {}
    except ValueError:
        status = {}
    if not isinstance(status, dict):
        status = {}
    stacks = status.setdefault("stacks", {})
    for name, entry in report.items():
        s = stacks.setdefault(name, {})
        s["last_run"] = finished
        s["ok"] = entry["ok"]
        s["errors"] = {k: v["error"] for k, v in entry["items"].items() if "error" in v}
        if entry["ok"]:
            s["last_success"] = finished
    if check is not None:
        status["check"] = {"at": finished, "ok": check.get("rc") == 0,
                           **({} if check.get("rc") == 0 else {"error": _error(check)})}
    return status


def backup_alerts(configured, expected, status_text, max_age_hours, now=None):
    """Alertes de sauvegarde pour health. status_text : fichier d'état (texte) ou
    vide ; now : instant de référence (ISO 8601), maintenant par défaut."""
    if not expected:
        return []
    if not configured:
        return ["sauvegardes : backup_repository non défini, aucune stack n'est sauvegardée"]
    try:
        status = json.loads(status_text) if status_text else {}
    except ValueError:
        status = {}
    now = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    alerts = []
    for name in expected:
        s = (status.get("stacks") or {}).get(name) or {}
        if not s.get("last_success"):
            alerts.append(f"stack {name} : aucune sauvegarde réussie")
        else:
            age = (now - datetime.fromisoformat(s["last_success"])).total_seconds() / 3600
            if age > float(max_age_hours):
                alerts.append(f"stack {name} : dernière sauvegarde réussie il y a {int(age)} h")
        if s.get("ok") is False:
            alerts.append(f"stack {name} : dernière sauvegarde en échec ({', '.join(sorted(s.get('errors') or {}))})")
    check = status.get("check")
    if check and not check.get("ok"):
        alerts.append(f"sauvegardes : restic check en échec : {check.get('error', '')[:200]}")
    return alerts


def backup_snapshots(stdout):
    """restic snapshots --json -> par stack : dernier instantané de chaque élément
    et nombre d'instantanés."""
    try:
        snapshots = json.loads(stdout or "[]")
    except ValueError:
        return {}
    report = {}
    for snap in sorted(snapshots or [], key=lambda s: s.get("time", "")):
        tags = snap.get("tags") or []
        stack = next((t for t in tags if not t.startswith(("volume:", "dump:"))), None)
        kind = next((t for t in tags if t.startswith(("volume:", "dump:"))), None)
        if not stack or not kind:
            continue
        entry = report.setdefault(stack, {})
        item = entry.setdefault(kind, {"count": 0})
        summary = snap.get("summary") or {}
        item.update(count=item["count"] + 1, latest=snap.get("time"), snapshot=snap.get("short_id"),
                    bytes=summary.get("total_bytes_processed"))
    return report


class FilterModule:
    def filters(self):
        return {
            "backup_expected": backup_expected,
            "backup_items": backup_items,
            "backup_results": backup_results,
            "backup_status": backup_status,
            "backup_alerts": backup_alerts,
            "backup_snapshots": backup_snapshots,
        }

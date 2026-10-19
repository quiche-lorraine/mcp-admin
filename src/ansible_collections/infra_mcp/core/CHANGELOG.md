# Changelog

## 0.4.0

- Playbooks de maintenance `maintenance_*` (status, journal, service_logs,
  stack_status, updates, reboot, restart_service, stack_restart, docker_prune),
  lancés par les outils MCP du même nom (docs/maintenance.md).

## 0.3.2

- Garde-fous du rôle `base` : le compte d'Ansible protégé est celui de
  l'inventaire (`base_ansible_user`, passé par `add-host`), et non celui du
  premier passage. `add-host --bootstrap-user ubuntu` (ou debian…) fonctionne
  sur les images où root ne peut pas se connecter.

## 0.3.1

- `swarm_join` : le plan ne plante plus sur un swarm pas encore initialisé
  (aucun token à lire en check mode), ce qui bloquait le premier `add-host`
  d'une machine du groupe `swarm`.

## 0.3.0

- Réseaux partagés entre stacks : `swarm_networks` liste des réseaux overlay
  créés chiffrés par le playbook stacks, seuls réseaux `external` acceptés
  dans un compose (par le playbook et par `infra-mcp lint`).

## 0.2.0

- Première version de la collection : rôles base et docker, playbooks site,
  swarm_join et stacks, garde-fous. Socle complété par le durcissement SSH,
  la synchronisation de l'heure et la limite de taille des journaux.

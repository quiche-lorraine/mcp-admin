# Changelog

## 0.3.0

- Réseaux partagés entre stacks : `swarm_networks` liste des réseaux overlay
  créés chiffrés par le playbook stacks, seuls réseaux `external` acceptés
  dans un compose (par le playbook et par `infra-mcp lint`).

## 0.2.0

- Première version de la collection : rôles base et docker, playbooks site,
  swarm_join et stacks, garde-fous. Socle complété par le durcissement SSH,
  la synchronisation de l'heure et la limite de taille des journaux.

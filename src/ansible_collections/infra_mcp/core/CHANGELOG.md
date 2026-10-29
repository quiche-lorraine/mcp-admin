# Changelog

## 0.10.0

- `maintenance_stack_autoupdate` : les services listés dans la clé `auto_update`
  d'une entrée de `swarm_stacks` suivent le tag de leur image. Lancé par le
  serveur toutes les `INFRA_MCP_AUTOUPDATE_INTERVAL` secondes (10 minutes par
  défaut) : le tag est tiré, le service relancé seulement si l'image a changé.

## 0.9.0

- `maintenance_app_command` (outils `app_query` et `app_command`) : commandes
  déclarées par stack dans le repo de config (clé `commands` d'une entrée de
  `swarm_stacks`), lancées par `docker exec` sans shell, paramètres validés,
  délai borné, sortie masquée ou jamais renvoyée (`secret_output`).

## 0.8.0

- `maintenance_health` (outil `health`) : passage de surveillance selon un
  modèle par défaut (seuils disque et mémoire, fenêtre et expression des
  lignes d'erreur, unités dont on lit le journal), surchargé par les variables
  `monitoring_*` et par la clé `monitoring` d'une entrée de `swarm_stacks`.

## 0.7.0

- `swarm_registries` : registres privés (ghcr.io…) d'où les stacks tirent leurs
  images. Le manager s'y connecte avant de déployer, avec un token du vault, et
  transmet les identifiants aux nœuds. `plan stacks` teste la connexion sans
  rien écrire. Le token s'ajoute avec `infra-mcp registry set`.

## 0.6.0

- `swarm_secrets_generated` : secrets Docker créés avec une valeur aléatoire au
  premier `apply stacks`, jamais modifiés ensuite. Rien à mettre dans le vault
  pour une clé d'application ou un mot de passe interne à une stack.

## 0.5.1

- `restart_service` sur `infra-mcp-update.service` (contrôleur) est différé
  comme pour `infra-mcp.service` : la mise à jour attend qu'aucun
  `ansible-playbook` ne tourne, elle attendait donc celui qui la relançait,
  pendant 30 minutes, avant d'abandonner.

## 0.5.0

- Rôle `crowdsec`, appliqué par `site` à tous les serveurs (sauf
  `crowdsec_enabled: false`) : détection SSH et Traefik, blocage par le bouncer
  nftables (hooks input et forward), IP jamais bannies (`crowdsec_whitelist_*`
  et l'adresse du contrôleur).
- `maintenance_status` remonte les bannissements CrowdSec en cours.
- Au premier `plan site` après cette mise à jour, l'installation de CrowdSec
  apparaît à faire.

## 0.4.1

- `maintenance_service_logs` : `since` en jours (`1d`) est converti en heures,
  que `docker service logs` accepte ; il échouait.

## 0.4.0

- Playbooks de maintenance `maintenance_*` (status, journal, service_logs,
  stack_status, updates, reboot, restart_service, stack_restart, docker_prune),
  lancés par les outils MCP du même nom (docs/maintenance.md).

## 0.3.3

- `stacks` : les options d'un réseau propre à une stack (`internal`, `driver`,
  `labels`…) sont conservées. `docker stack deploy` remplace la définition d'un
  réseau d'un fichier compose à l'autre : la surcharge qui ajoute le
  chiffrement les effaçait. Elle reprend désormais la définition du compose.
- `stacks` : un réseau déclaré avec des options mais sans `external`, ou sans
  définition (`null`), ne fait plus échouer la lecture des compose.
- `swarm_networks` : noms validés en entier (`fullmatch`).
- Les stacks sont annoncées à redéployer en plan après cette mise à jour
  (l'empreinte inclut la surcharge).

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

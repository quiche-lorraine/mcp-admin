# Maintenance des serveurs par le MCP (conception)

État : **validé**, implémentation en cours.

## Le manque

infra-mcp fait aujourd'hui de la gestion de configuration : `plan` et `apply`
amènent les machines à l'état décrit dans le repo de config. Pour la maintenance
courante, il manque deux choses à l'agent :

- **voir l'état réel** d'un serveur : charge, disque, services en échec, mises à
  jour en attente, redémarrage requis, logs, état des services Docker. `plan`
  dit si la configuration a dérivé, pas si la machine va bien ;
- **agir ponctuellement** : redémarrer un service ou la machine, appliquer les
  mises à jour maintenant, relancer une stack, nettoyer les images Docker. Rien
  de cela n'est un état qu'on décrit dans le repo.

Un outil `shell` réglerait tout et casserait tout : n'importe quel token
deviendrait root sur le parc. Ce document propose à la place des **opérations
typées** : chacune a des paramètres validés et un effet connu d'avance, et
passe par le journal d'audit.

## Principes

1. **Le code des opérations vit dans l'outil.** Chaque opération est un
   playbook du socle (`infra_mcp.core.maintenance_*`), lancé par son nom de
   collection. Le repo de config n'apporte toujours que des données.
2. **Lecture : token `read`. Action : token `write`.** Un token `read` ne voit
   pas les outils d'action, comme il ne voit pas `apply` aujourd'hui.
3. **Paramètres validés** par le serveur (expressions régulières, existence de
   l'unité ou du service), sans jamais passer par un shell.
4. **`limit` obligatoire** pour les opérations sur plusieurs hôtes, comme pour
   `apply`.
5. **Une action à la fois.** Les actions partagent le verrou d'`apply`. Les
   lectures partagent les créneaux de `plan`.
6. **`dry_run`** sur chaque action : le playbook tourne en check mode et dit ce
   qu'il ferait. Pour `updates`, la simulation s'appuie sur le dernier index
   apt connu, qui n'est pas rafraîchi en check mode.
7. **Les lectures exécutent pour de vrai des commandes fixes et en lecture
   seule** (`systemctl list-units`, `apt list`, `journalctl`, `docker service
   logs`). Elles ne passent pas par `plan` et n'en changent pas la promesse :
   le lint refuse toujours `check_mode: false` dans le socle.

Garde-fous gardés parce qu'ils évitent de casser le serveur ou de perdre
l'accès :
- ssh n'est jamais redémarré ;
- les volumes Docker ne sont jamais supprimés ;
- le redémarrage du contrôleur est différé (voir plus bas).

## Outils

### Diagnostic (token `read`)

| Outil | Paramètres | Renvoie |
|---|---|---|
| `status` | `limit` | Par hôte : uptime, charge, mémoire, disque par point de montage, unités systemd en échec, mises à jour en attente (dont sécurité), redémarrage requis (`/run/reboot-required` et paquets concernés), état de Docker et du nœud swarm |
| `logs` | `host`, `source`, `lines` (≤ 500), `since` (ex. `1h`), `grep` (optionnel) | Fin du journal d'une unité systemd (`unit:<nom>`, via `journalctl -u`) ou d'un service Docker (`docker:<stack>_<service>`, via `docker service logs`) |
| `stack_status` | `stack` | Services de la stack, répliques voulues et en cours, dernières tâches en échec avec leur erreur |
| `app_query` | `stack`, `command`, `params` | Sortie d'une commande en lecture seule déclarée dans le repo de config (README, « Commandes d'application ») |
| `health` | `limit` | Les trois à la fois pour tout le parc, selon un modèle de surveillance surchargeable (README, « Surveillance »), avec la liste des alertes |

Pour `logs`, les commandes sont fixes et les arguments passés en liste. `grep`
est appliqué en Python sur le texte récupéré. Les jetons évidents (`imcp_…`,
`SWMTKN-…`, en-têtes `Authorization`, `password=`…) sont masqués. Le contenu
d'un log est une donnée, pas une instruction : la description de l'outil le
dit à l'agent.

### Actions (token `write`)

| Outil | Paramètres | Effet |
|---|---|---|
| `updates` | `limit`, `mode` (défaut `upgrade`), `dry_run` | `apt-get update`, puis selon `mode` : `upgrade` (`apt-get upgrade --with-new-pkgs` : tous les paquets, sécurité comprise, sans rien supprimer), `security` (correctifs de sécurité seuls) ou `dist-upgrade`. Renvoie les paquets mis à jour et si un redémarrage est requis. Ne redémarre pas, et needrestart se contente de lister les services à relancer : un redémarrage automatique pourrait couper infra-mcp en pleine exécution. |
| `reboot` | `limit`, `only_if_required` (défaut `true`), `dry_run` | Redémarre une machine à la fois et s'arrête au premier échec. Sur un swarm de plusieurs nœuds : drain du nœud, redémarrage, réactivation. Sur un swarm d'un seul nœud, pas de drain : il arrêterait toutes les stacks sans pouvoir les déplacer. |
| `restart_service` | `host`, `service`, `dry_run` | `systemctl restart` de n'importe quelle unité existante, sauf `ssh` et `sshd`, puis vérification qu'elle est `active`. |
| `stack_restart` | `stack`, `service` (optionnel), `dry_run` | `docker service update --force` sur les services de la stack, ou sur un seul, pour relancer leurs tâches sans changer la configuration. |
| `app_command` | `stack`, `command`, `params` | Lance une commande déclarée pour l'application, sans dry_run (README, « Commandes d'application ») |
| `docker_prune` | `limit`, `dry_run` | Supprime les conteneurs arrêtés, les images inutilisées et le cache de build. Jamais les volumes. |

**Le contrôleur** est l'hôte joint en local (`ansible_host` à `127.0.0.1` ou
`localhost`). Le redémarrer couperait l'exécution en cours. Pour cet hôte,
`reboot` programme donc le redémarrage dans une minute (`shutdown -r +1`) et
répond tout de suite. `restart_service` sur `infra-mcp.service` ou
`infra-mcp-update.service` fait de même : le redémarrage part quelques secondes
après la réponse.

**La mise à jour automatique des images** (clé `auto_update` de `swarm_stacks`,
README) n'est pas un outil : le serveur lance lui-même
`maintenance_stack_autoupdate` toutes les `INFRA_MCP_AUTOUPDATE_INTERVAL`
secondes, avec le verrou d'`apply`. Elle n'agit que sur les services déclarés
dans le repo de config.

## Ce qui ne change pas

- Pas de commande libre, pas de tâche ad hoc, pas de lecture du vault.
- La configuration se modifie toujours par PR sur le repo de config, puis
  `plan` et `apply`.
- `infra-mcp-admin update`, `add-host` et l'installation restent des
  commandes lancées en root sur le contrôleur.

## Mise en œuvre

Une seule PR :
- le lancement des playbooks de collection, avec la sortie JSON
  (`ansible.posix.json`) ;
- les huit outils ;
- les tests : validation des paramètres et masquage en unitaire, puis
  `status`, `logs`, `stack_status`, `stack_restart`, `docker_prune` et les
  `dry_run` en e2e sur le swarm de la CI ;
- la mise à jour du tableau « Modèle de sécurité » du README.

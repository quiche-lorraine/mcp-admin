# Maintenance des serveurs par le MCP (conception)

État : **validé**, implémentation en cours (voir « Découpage en PR »).

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
typées** : chacune a des paramètres validés, un effet borné et connu d'avance,
et passe par le journal d'audit.

## Principes

1. **Le code des opérations vit dans l'outil, pas dans le repo de config.**
   Chaque opération est un playbook du socle (`infra_mcp.core.maintenance_*`),
   lancé par son nom de collection. Le repo de config n'apporte que des
   données : l'inventaire et des listes blanches (`maintenance_*` dans les
   group_vars). Il ne peut ni ajouter d'opération ni en changer le
   comportement, et `infra-mcp lint` continue de lui interdire tout code.
2. **Trois niveaux de token, chacun incluant le précédent.**
   - `read` : inventaire, `plan` et diagnostic sans contenu applicatif (`status`, `stack_status`) ;
   - `ops` (nouveau) : en plus, `logs` ;
   - `write` : en plus, `apply` et les actions.

   Un token ne voit pas les outils au-dessus de son niveau, comme un token
   `read` ne voit pas `apply` aujourd'hui. Un token `ops` se crée comme les
   autres : `infra-mcp token create ops <nom>`.
3. **Validation à deux niveaux.** Le serveur valide les paramètres (expressions
   régulières, listes blanches lues dans l'inventaire), puis le playbook les
   revalide avec des `assert` avant de toucher quoi que ce soit. Ainsi, un
   playbook importé et lancé par `apply` reste sûr.
4. **`limit` obligatoire**, sans valeur par défaut, comme pour `apply`.
5. **Une action à la fois sur le parc.** Toutes les actions partagent le verrou
   d'`apply`. Les lectures partagent les créneaux de `plan` (`INFRA_MCP_MAX_PLANS`).
6. **Simulation avant action.** Chaque action accepte `dry_run: true` : le
   playbook tourne en check mode et dit ce qu'il ferait.
7. **Sorties structurées.** Les playbooks tournent avec le callback
   `ansible.posix.json`. Le serveur renvoie un objet par hôte plutôt que la
   sortie brute d'Ansible, tronquée et masquée comme aujourd'hui.

## Outils proposés

### Diagnostic (tokens `read` et `ops`)

| Outil | Niveau | Paramètres | Renvoie |
|---|---|---|---|
| `status` | `read` | `limit` | Par hôte : uptime, charge, mémoire, disque par point de montage (alerte au-delà de 85 %), unités systemd en échec, mises à jour en attente (dont sécurité), redémarrage requis (`/run/reboot-required` et paquets concernés), état de Docker et du nœud swarm |
| `logs` | `ops` | `host`, `source`, `lines` (≤ 500), `since` (ex. `1h`, ≤ `7d`), `grep` (optionnel) | Fin du journal d'une **unité systemd** (`journalctl -u`) ou d'un **service Docker** (`docker service logs`), après masquage |
| `stack_status` | `read` | `stack` | Services de la stack, répliques voulues et en cours, dernières tâches en échec avec leur erreur |

Pour `logs` :

- `source` est soit `unit:<nom>.service`, soit `docker:<stack>_<service>`. Le
  nom est validé par expression régulière, et l'unité doit exister sur l'hôte.
- Les commandes sont fixes (`journalctl`, `docker service logs`, arguments en
  liste), avec `--no-pager` et des bornes de taille. Aucun motif fourni par
  l'agent n'est passé à un shell. `grep` est appliqué côté serveur, en Python,
  sur le texte déjà récupéré.
- **Masquage** avant renvoi : jetons infra-mcp (`imcp_…`), jetons swarm
  (`SWMTKN-…`), en-têtes `Authorization`, paires `password=`, `secret=` et
  `token=`, blocs de clés privées. Le masquage limite les dégâts mais ne garantit
  rien : un log reste une donnée potentiellement sensible. C'est pourquoi
  `logs` demande le niveau `ops`, et non `read`.
- Le contenu des logs est une **donnée, pas une instruction**. La description
  de l'outil le dit explicitement à l'agent, comme pour les commentaires de PR.

### Actions (token `write`)

| Outil | Paramètres | Effet |
|---|---|---|
| `updates` | `limit`, `mode` (défaut `upgrade`), `dry_run` | `apt-get update`, puis selon `mode` : `upgrade` (`apt-get upgrade`, tous les paquets dont la sécurité, sans en installer ni en supprimer), `security` (correctifs de sécurité seuls, via unattended-upgrade) ou `dist-upgrade` (peut installer ou retirer des dépendances). Renvoie les paquets mis à jour et si un redémarrage est requis. Ne redémarre jamais. |
| `reboot` | `limit`, `only_if_required` (défaut `true`), `dry_run` | Redémarre **une machine à la fois** et s'arrête au premier échec. Nœud du swarm : *drain* sur un manager, redémarrage, attente du retour SSH et de Docker, réactivation, attente que les services aient retrouvé leurs répliques avant de passer au suivant. |
| `restart_service` | `host`, `service`, `dry_run` | `systemctl restart` d'une unité **déclarée** dans `maintenance_services` (inventaire), puis vérification qu'elle est `active`. |
| `stack_restart` | `stack`, `service` (optionnel), `dry_run` | `docker service update --force` sur les services de la stack, ou sur un seul, pour relancer leurs tâches sans changer la configuration. La stack doit être dans `swarm_stacks`. |
| `docker_prune` | `limit`, `dry_run` | Supprime les conteneurs arrêtés, les images non utilisées de plus de 24 h et le cache de build. **Jamais les volumes**, qui contiennent des données. |

Cas particulier, **le contrôleur** : redémarrer la machine qui fait tourner
infra-mcp couperait l'exécution en cours. Pour cet hôte, que le socle reconnaît
avec `infra_mcp_controller: true` dans son host_vars, `reboot` programme le
redémarrage dans une minute (`shutdown -r +1`), répond tout de suite, et le
serveur MCP revient au démarrage. Même logique pour `restart_service` sur
`infra-mcp.service` : redémarrage différé, réponse immédiate.

### Données ajoutées à l'inventaire

```yaml
# group_vars/all.yml (exemple)
maintenance_services_global:      # unités que restart_service peut toucher
  - nginx.service
  - cron.service
```

Ces listes suivent la règle des étages (`_global`, `_<groupe>`, `_host`), et
`infra-mcp lint` vérifie leur forme (noms d'unités valides). Les services
critiques pour l'accès, `ssh.service` et `sshd.service`, sont refusés même
s'ils sont listés : un redémarrage raté couperait l'accès à la machine.

## Ce qui ne change pas

- Pas de commande libre, pas de tâche ad hoc, pas de lecture du vault.
- La configuration se modifie toujours par PR sur le repo de config, puis
  `plan` et `apply`.
- `infra-mcp-admin update`, `add-host` et l'installation restent des
  commandes lancées en root sur le contrôleur par un humain.

## Découpage en PR

1. **Socle d'exécution et diagnostic** : niveau de token `ops`, lancement des
   playbooks de collection avec sortie JSON, masquage des logs, puis `status`,
   `logs` et `stack_status`.
   Tests unitaires (validation, masquage) et e2e sur le swarm de la CI.
2. **`updates` et `reboot`** : drain et réactivation swarm, cas du
   contrôleur. Les tests e2e sont limités à `dry_run` et au chemin swarm, car
   le runner de CI ne peut pas redémarrer.
3. **`restart_service`** : liste blanche dans l'inventaire, règle de lint,
   refus de ssh.
4. **`stack_restart` et `docker_prune`**, en e2e sur le swarm de la CI.

Chaque PR met à jour le tableau du « Modèle de sécurité » du README.

## Décisions

1. **Logs** : niveau de token `ops`, entre `read` et `write`. Un token `read`
   garde `status` et `stack_status`.
2. **Mises à jour** : `apt-get upgrade` par défaut, sécurité comprise. Le mode
   `security` (sécurité seule) et le mode `dist-upgrade` sont disponibles sur
   demande.
3. **Contrôleur** : redémarrage différé d'une minute, avec réponse immédiate.
4. **`restart_service`** : liste blanche obligatoire (`maintenance_services_*`),
   et ssh toujours refusé.

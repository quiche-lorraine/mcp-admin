# infra-mcp

Serveur MCP (FastMCP, Streamable HTTP) qui pilote un repo Ansible. Claude peut
lire l'état du parc, simuler un changement et, avec un token d'écriture,
appliquer un playbook existant. Il ne peut rien exécuter d'autre.

Le projet livre deux choses, versionnées ensemble :
- **l'outil** (`src/infra_mcp/`) : le serveur MCP et la commande `infra-mcp lint` ;
- **le socle Ansible** (collection `infra_mcp.core`, `src/ansible_collections/`) :
  ce que tout serveur reçoit, avec ses garde-fous.

La configuration du parc (inventaire, variables, stacks, vault, et des
playbooks d'une ligne qui importent ceux du socle) vit dans un repo de config
séparé, ici [quiche-lorraine/infra](https://github.com/quiche-lorraine/infra),
qui fige la version de l'outil qu'il utilise. `examples/infra/` en est un
modèle générique, aussi utilisé par les tests.

## Modèle de sécurité

| Outil            | Niveau | Effet                                                        |
|------------------|--------|--------------------------------------------------------------|
| `list_hosts`     | read   | Arborescence de l'inventaire                                 |
| `host_vars`      | read   | Variables effectives d'un hôte, secrets masqués              |
| `list_playbooks` | read   | Playbooks présents dans le repo                              |
| `repo_status`    | read   | Commit actuellement déployé sur le contrôleur                |
| `plan`           | read   | `--check --diff` : ce qui changerait, sans rien toucher      |
| `apply`          | write  | Exécute un playbook du repo, `limit` obligatoire             |
| `status`         | read   | État des hôtes : charge, disques, unités en échec, mises à jour en attente, redémarrage requis, Docker |
| `logs`           | read   | Fin des logs d'une unité systemd ou d'un service Docker, secrets évidents masqués |
| `stack_status`   | read   | Services d'une stack : répliques voulues et en cours, erreurs |
| `updates`        | write  | Mises à jour des paquets (`upgrade`, `security` ou `dist-upgrade`), sans redémarrage |
| `reboot`         | write  | Redémarrage un hôte à la fois (drain si le swarm a plusieurs nœuds ; contrôleur différé d'une minute) |
| `restart_service`| write  | Redémarre une unité systemd, jamais ssh                      |
| `stack_restart`  | write  | Relance les tâches d'une stack sans changer sa configuration |
| `docker_prune`   | write  | Supprime conteneurs arrêtés, images inutilisées, cache de build ; jamais les volumes |

Les outils de maintenance (de `status` à `docker_prune`) lancent des playbooks
du socle (`infra_mcp.core.maintenance_*`), pas du repo de config, avec leurs
paramètres validés en extra vars. Les actions acceptent `dry_run` (check mode)
et partagent le verrou d'`apply`. Conception : [docs/maintenance.md](docs/maintenance.md).

Volontairement absents : commande shell, tâche ad hoc, création de playbook,
lecture des secrets. Le vault est déchiffré par Ansible au moment d'exécuter,
jamais renvoyé à Claude : `host_vars` masque les variables `vault_*` et les
tâches qui manipulent des secrets sont en `no_log`. Un token `read` ne voit même
pas `apply` dans la liste des outils.

- Les tokens sont stockés sous forme d'empreinte SHA-256 et relus à chaque
  requête : une révocation est immédiate, sans redémarrage.
- Chaque appel (refus compris) est journalisé dans `audit.jsonl` avec le token
  utilisé, les arguments et la durée.
- Un seul `apply` à la fois, et au plus `INFRA_MCP_MAX_PLANS` (2) lectures
  simultanées (`plan`, `status`, `logs`, `stack_status`…) : un token read ne peut
  pas saturer le contrôleur ni le parc. Une lecture de plus attend qu'une place se
  libère, au plus `INFRA_MCP_SLOT_WAIT` (300) secondes, avant d'être refusée.
- Les entrées sont validées : `limit` n'accepte que des noms d'hôtes ou de
  groupes, `playbook` que des fichiers de `playbooks/` du repo de config.

Le point sensible : le contrôleur détient une clé SSH avec sudo sur tout le
parc, et le mot de passe vault. Toute la sécurité repose sur les outils exposés
ci-dessus et sur le fait que les playbooks ne changent que par commit dans le
repo de config.

**Le repo de config vaut root sur le parc.** `plan` exécute ses playbooks sur
le contrôleur : un contenu malveillant mergé pourrait y lire la clé SSH ou le
mot de passe vault et les afficher dans la sortie, accessible avec un simple
token read. `infra-mcp lint` ferme les voies connues (un repo de config
n'apporte que des données) :
- ni code : pas de `.py`, de dossier de plugins ou de modules, de fichier
  exécutable, d'option `ansible.cfg` hors d'une liste sûre ;
- rien sur le contrôleur : pas de `delegate_to`, `local_action`, `connection`,
  `hosts: localhost`, ni de variable de connexion (`ansible_ssh_*_args`,
  interpréteur autre qu'un chemin…) ;
- rien lu sur le contrôleur : lookups limités à une liste sûre (`vars`,
  `template`…), pas de `src` / `vars_files` hors du repo ;
- secrets : fichiers chiffrés nommés `vault.yml`, pas de `!vault` en ligne,
  variables `vault_*` utilisées seulement par `swarm_secrets`.

C'est une défense en profondeur : la relecture des PR, une branche principale
protégée et la surveillance des changements restent la vraie barrière.

## Workflow

1. Claude (ou toi) modifie `group_vars` / `host_vars` et ouvre une PR sur le
   repo de config, dont la CI lance `infra-mcp lint`.
2. Après le merge, le timer `infra-mcp-sync` avance le repo sur le contrôleur
   (si le commit passe le lint). Si `requirements.txt` a changé, l'outil se met
   à jour tout seul (voir « Mise à jour »).
3. `plan site` montre l'effet ; `apply site` l'applique. Pareil pour les
   stacks Swarm avec `plan stacks` / `apply stacks`.

`plan site` sur `all` sert aussi de détection de dérive : tout `changed` est un
écart entre une machine et le repo.

## Socle (infra_mcp.core)

Installée avec l'outil (`pip install`), la collection est trouvée par Ansible
sans configuration. `playbooks/site.yml` du repo de config se résume à
`ansible.builtin.import_playbook: infra_mcp.core.site`.

Le rôle `base`, appliqué à tous les serveurs :
- **garde-fous** d'abord : rien n'est modifié si la configuration est dangereuse ;
- **paquets** : ca-certificates, curl, unattended-upgrades ; telnet retiré ;
- **mises à jour de sécurité** automatiques, redémarrage à `updates_reboot_time` ;
- **heure** : fuseau `base_timezone` (UTC), synchronisation par
  systemd-timesyncd (sauf si chrony est déjà là) ;
- **journaux** : journald limité à `base_journald_max_use` (500M) ;
- **comptes admin** : clés SSH exclusives, sudo exclusif ;
- **SSH** : clé uniquement, jamais root (`sshd_config.d/10-infra-base.conf`,
  validé par `sshd -t`, prioritaire sur cloud-init) ;
- **firewall** : entrée en deny, port SSH toujours ouvert ;
- **comptes retirés** en dernier.

Le rôle `crowdsec`, appliqué à tous les serveurs (sauf `crowdsec_enabled:
false`) :
- **CrowdSec** depuis le dépôt de l'éditeur (paquets `any`, indépendants de la
  version d'Ubuntu) ;
- **détection** : SSH (`auth.log`, ou journald sans rsyslog) et, sur les nœuds
  Docker, les logs d'accès de Traefik (`crowdsec_traefik_containers`) ;
  collections `crowdsecurity/linux`, `sshd` et `traefik` ;
- **blocage** par le bouncer nftables, sur les hooks `input` (la machine) et
  `forward` (ports publiés par Docker, qui échappent aux règles d'ufw) ;
- **jamais bannis** : réseaux privés et boucle locale, l'adresse d'où Ansible
  se connecte (le contrôleur), et `crowdsec_whitelist_*` (IP ou CIDR, listes
  additionnées comme celles de `base`) ;
- les IP qui attaquent sont partagées avec CrowdSec, qui fournit en retour sa
  liste communautaire d'IP malveillantes ;
- l'outil `status` donne les bannissements en cours (décisions locales).

Ses valeurs par défaut (`roles/base/defaults/main.yml`) se surchargent dans
l'inventaire. Ses **verrous** (`roles/base/vars/main.yml`, aujourd'hui le
firewall en deny) ne peuvent pas l'être : le rôle échoue si un étage essaie.
Un repo de config peut ajouter ses propres verrous avec `locked_vars`.

`infra-mcp lint <dossier>` vérifie un repo de config avec les règles de l'outil
(plan reste une simulation, le repo n'apporte que des données, disposition des
variables, politique des stacks) : à lancer dans sa CI, avec la même version de
l'outil.

## Configuration à étages

Les étages suivent l'inventaire : `group_vars/all.yml` → `group_vars/<groupe>.yml`
→ `host_vars/<hôte>.yml`.

- **Valeurs simples** : priorité Ansible classique, l'hôte gagne sur le groupe,
  qui gagne sur le global.
- **Listes** : elles s'additionnent. Chaque étage définit sa propre variable avec
  un suffixe (`packages_present_global`, `packages_present_swarm`,
  `packages_present_host`) et le rôle `base` fusionne tout ce qui commence par
  `packages_present_`, y compris `packages_present_base` du socle. Même principe pour `packages_absent_`, `firewall_allow_`,
  `firewall_remove_`, `admin_users_`, `users_absent_` et `crowdsec_whitelist_`.
- **Retraits** : enlever un élément d'une liste ne défait rien sur les serveurs.
  Pour fermer un port, liste la règle dans `firewall_remove_*` (avec la même
  `src` que la règle d'origine) ; pour supprimer un compte, liste-le dans
  `users_absent_*` (son sudo est retiré, son dossier personnel conservé mais
  réservé à root). Le rôle `base` refuse, avant de toucher quoi que ce soit,
  une entrée mal formée, un élément à la fois ajouté et retiré, le retrait du
  port SSH (22 et `ansible_port`), de `root` ou de `ansible_user`.
- **Sudo** : géré en exclusif. Passer un admin à `sudo: false`, ou le retirer
  de `admin_users_*`, supprime son fichier sudo. Seuls les fichiers créés par
  le rôle (marqués `# ansible-managed: role base`) sont concernés.
- **Valeurs verrouillées** : celles du socle, plus `locked_vars` du repo de
  config. Si un étage tente de les modifier, le rôle `base` échoue avant de
  toucher quoi que ce soit.

## Stacks Swarm

`playbooks/stacks.yml` déploie des stacks depuis le premier manager du swarm
(après `site` et `swarm-join`).

- **Une stack** = `stacks/<nom>/compose.yml` dans le repo de config, déclarée dans
  `swarm_stacks` (`group_vars/swarm_managers/stacks.yml`). La retirer de la
  liste ne la supprime pas : liste-la dans `swarm_stacks_absent`.
- **Secrets** : chiffrés avec ansible-vault dans
  `group_vars/swarm_managers/vault.yml`, sous des noms `vault_*` (le playbook
  refuse les autres : `host_vars` les afficherait en clair). `swarm_secrets`
  les transforme en secrets Docker ; le compose les déclare `external: true` et
  la stack les liste dans `secrets`. Changer une valeur crée une nouvelle
  version (`nom_v2`…) et redéploie les stacks concernées.

  ```bash
  ansible-vault create inventory/group_vars/swarm_managers/vault.yml
  ansible-vault edit   inventory/group_vars/swarm_managers/vault.yml
  ```
- **Secrets générés** : pour une valeur qui doit seulement être secrète et
  stable (clé d'application, mot de passe entre deux conteneurs), il suffit de
  lister son nom dans `swarm_secrets_generated`. Le secret Docker est créé avec
  une valeur aléatoire au premier `apply stacks`, puis n'est plus jamais
  modifié : la valeur ne passe ni par le repo ni par personne. Pour en changer,
  supprimer le secret à la main (`docker secret rm`) une fois les stacks qui
  l'utilisent arrêtées.
- **plan** : `docker_stack` ne sait pas simuler. Chaque service porte l'empreinte
  de son compose (label `infra.stack-hash`) ; `plan stacks` la compare à celle
  qui tourne et annonce « à créer », « à redéployer » ou « à supprimer ».
- **Politique** (vérifiée par `infra-mcp lint`) : image avec
  un tag explicite (jamais `latest`), pas de `privileged`, réseau/pid/ipc de
  l'hôte, `cap_add`, `devices`, montage du socket Docker ou de chemins sensibles
  de l'hôte. Une exception se déclare sur le service, avec sa raison :
  `x-allow: {docker-socket: "Portainer pilote Docker"}`. Elle reste visible en
  revue de PR. Sont aussi refusés : l'interpolation `$VAR` (évaluée avec
  l'environnement root du manager : `${HOME}:/data` monterait `/root`), les
  volumes `driver_opts` qui montent un chemin de l'hôte (traités comme un
  montage direct), et les réseaux `external` autres que les réseaux partagés
  (ci-dessous).
- **Réseaux chiffrés** : le socle force `encrypted` sur le réseau par défaut et
  sur chaque réseau déclaré : le trafic entre nœuds passe en IPsec, ESP étant
  ouvert entre les nœuds par `swarm-join`. Un réseau existant ne change pas
  d'option : pour chiffrer une stack déjà déployée, supprime-la puis
  redéploie-la.
- **Réseaux partagés** : pour que plusieurs stacks se parlent (un reverse proxy
  et les services qu'il expose), déclare le réseau dans `swarm_networks`
  (`group_vars/swarm_managers/stacks.yml`, là où `infra-mcp lint` le lit). Le
  playbook le crée en overlay chiffré avant de déployer les stacks, qui s'y
  branchent sous le nom de sa clé :

  ```yaml
  # group_vars/swarm_managers/stacks.yml
  swarm_networks: [proxy]

  # stacks/<nom>/compose.yml
  services:
    web:
      networks: [default, proxy]
  networks:
    proxy:
      external: true
  ```

  Ce sont les seuls réseaux `external` acceptés, sans `name:`. Retirer un réseau
  de la liste ne le supprime pas. Un réseau du même nom créé sans chiffrement est
  recréé chiffré, ce qui échoue tant qu'un service l'utilise.

## Installation sur le contrôleur

Une seule commande, sur le contrôleur (Debian ou Ubuntu), depuis une session
SSH :

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/main/install.sh)"
```

Le script pose ses questions au fur et à mesure, sans jamais demander de
relancer. On peut aussi y répondre d'avance :
`sudo bash -c "$(curl -fsSL …/install.sh)" -- --repo quiche-lorraine/infra --domain mcp.exemple.fr`.
Il peut être relancé autant de fois que voulu : il ne refait que ce qui manque.

Pas de `curl … | sudo bash` : `sudo` lance alors la commande hors du premier
plan du terminal, et la première saisie ou le premier `apt` la suspend sans
rien afficher. `install.sh` le détecte et donne la bonne commande.

`install.sh` récupère `infra-mcp-admin` sur `main` et lui passe la main. Dès
que le repo de config est cloné, la suite se fait avec `infra-mcp-admin` à la
version que ce repo fige (`requirements.txt`). Pour lire le script avant de
l'exécuter : `curl -fsSLO …/install.sh`, puis `sudo bash install.sh`.

`install` :
1. crée le compte `infra-mcp`, ses dossiers et sa clé SSH vers le parc ;
2. clone le repo de config dans `/opt/infra` (`owner/repo` suffit pour GitHub).
   S'il est privé, une clé dédiée (distincte de celle du parc) est créée : le
   script l'affiche avec le lien de la page GitHub où l'ajouter en *deploy
   key* **en lecture seule**, attend que ce soit fait, puis continue. La clé
   d'hôte de GitHub est vérifiée contre son empreinte publiée ;
3. installe l'outil dans `/opt/infra-mcp/venv`, à la version de
   `requirements.txt` (socle et paquet ansible compris) ;
4. installe les unités systemd et se copie dans `/usr/local/sbin`, depuis le
   paquet installé : tout suit la même version ;
5. demande le mot de passe vault (sans écho), s'il n'est pas déjà en place ;
6. récupère les clés d'hôte du parc et les affiche toutes avec leur empreinte,
   **à confirmer** (à comparer avec `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
   depuis la console de l'hébergeur) ;
7. met le serveur derrière un reverse proxy HTTPS (voir ci-dessous) ;
8. démarre les services, puis vérifie : `infra-mcp lint`,
   `systemd-analyze security`, que le serveur répond 401 sans token, et la
   même chose en HTTPS à travers le proxy, certificat compris ;
9. crée le token de Claude Code (lecture ou écriture, au choix) et affiche la
   commande `claude mcp add` complète, à lancer sur ton poste.

Il termine par ce qui reste à faire : ajouter la clé publique du contrôleur
dans `admin_users_global` si elle n'y est pas encore, puis `add-host` pour
chaque machine.

**Reverse proxy.** Le serveur n'écoute qu'en HTTP local ; `install` cherche un
Traefik déjà en place :
- **dans un conteneur Docker** (par exemple celui de Coolify) : il lit sa
  configuration (options, variables ou fichier monté) pour trouver son point
  d'entrée sur le port 443, son résolveur de certificats et son dossier de
  routes. Il propose alors d'y ajouter la route du serveur MCP, de faire écouter
  infra-mcp sur la passerelle du réseau Docker de Traefik (joignable par les
  conteneurs, pas par Internet) et, si ufw est actif, de laisser passer ce
  réseau ;
- **sur la machine** : même chose, infra-mcp restant en local ;
- **aucun** : si le port 443 est libre, il propose d'installer Traefik
  (binaire de version et d'empreinte figées dans le script, unité systemd
  durcie sous un compte éphémère, `/etc/traefik`). Le certificat Let's
  Encrypt est obtenu sur le port 443 : le port 80 reste fermé. Il faut que le
  domaine pointe vers le contrôleur ; si ufw est actif, le script propose
  d'ouvrir le 443 (à déclarer aussi dans `firewall_allow_host` si le
  contrôleur est dans l'inventaire).

Quand il ne peut pas écrire la route lui-même, il la prépare dans
`/etc/infra-mcp/traefik-infra-mcp.yml`, à ajouter à la configuration
dynamique de Traefik. Le domaine est retenu dans `/etc/infra-mcp/proxy.conf`.
`update` met à jour le Traefik qu'il a installé, et seulement celui-là.

Les réglages propres au contrôleur vont dans `/etc/infra-mcp/infra-mcp.env`
(créé par `install`, jamais écrasé ; seule l'adresse d'écoute peut y être
changée par `install`, après accord).

**Mise à jour** : automatique. Quand une PR qui change `requirements.txt` est
mergée dans le repo de config, le contrôleur récupère le commit (timer
`infra-mcp-sync`, après le lint), et `infra-mcp-update.path` voit le fichier
changer. Il lance `infra-mcp-update.service`, qui exécute en root
`infra-mcp-admin update --auto` :
- rien à faire si l'outil est déjà à la version demandée ;
- sinon, attente de la fin des opérations Ansible en cours, puis mise à jour
  et redémarrage d'infra-mcp.

L'outil MCP `repo_status` donne la version qui tourne. Le résultat est dans
`journalctl -u infra-mcp-update`, et un échec apparaît dans `systemctl
--failed` (outil `status`). Le serveur MCP ne peut pas déclencher de mise à
jour : seul un commit mergé et passé au lint le fait.

À la main, même chose sans condition (et seule possibilité quand le nouveau
commit ne passe le lint qu'avec la nouvelle version de l'outil) :

```bash
sudo infra-mcp-admin update
```

## Ajouter une machine

1. L'ajouter à l'inventaire du repo de config (PR), et attendre la
   synchronisation du contrôleur.
2. La créer chez l'hébergeur, au choix :
   - **avec cloud-init** (recommandé) : `sudo infra-mcp-admin cloud-init <hôte>`
     produit un *user-data* à coller à la création. La machine démarre avec les
     comptes admin, leurs clés et leur sudo, sans root ni mot de passe : elle
     n'est jamais accessible en root ;
   - **sinon**, avec la clé publique du contrôleur pour root, ou pour le compte
     sudo de l'image (`ubuntu`, `debian`…) quand l'hébergeur refuse root ;
3. `sudo infra-mcp-admin add-host <hôte>` :
   - vérifie que la machine est dans l'inventaire, fait confirmer l'empreinte
     de sa clé d'hôte ;
   - se connecte avec le compte admin (cloud-init) ou, à défaut, en root, ou
     avec le compte donné par `--bootstrap-user ubuntu`. Ce compte-là garde ses
     accès (à lister dans `users_absent_*` pour le retirer) ;
   - lance `site` en simulation, montre le résultat, puis l'applique après
     confirmation ;
   - vérifie que le compte admin se connecte et que root est refusé : si le
     compte admin ne se connecte pas, il s'arrête **avant** que tu perdes
     l'accès et dit quoi corriger ;
   - si la machine est dans le groupe `swarm`, lance `swarm-join` sur tout le
     swarm (les autres nœuds doivent lui ouvrir leurs ports), simulation puis
     confirmation.

## Tokens

`install` crée le token « claude-code ». Pour en gérer d'autres, toujours en
tant que `infra-mcp`, pour que le service puisse lire le fichier :

```bash
sudo -u infra-mcp env INFRA_MCP_TOKENS=/etc/infra-mcp/tokens.json \
  /opt/infra-mcp/venv/bin/infra-mcp token create read claude-code-web
sudo -u infra-mcp env INFRA_MCP_TOKENS=/etc/infra-mcp/tokens.json \
  /opt/infra-mcp/venv/bin/infra-mcp token list
sudo -u infra-mcp env INFRA_MCP_TOKENS=/etc/infra-mcp/tokens.json \
  /opt/infra-mcp/venv/bin/infra-mcp token revoke <id>
```

Le token n'est affiché qu'une fois.

## Brancher Claude Code

```bash
claude mcp add --transport http infra https://mcp.exemple.fr/mcp \
  --header "Authorization: Bearer imcp_..."
```

## Développement

```bash
pip install -e '.[dev]'                       # outil, socle, ansible complet
pytest                                        # serveur, tokens, socle, garde-fous
(cd src/ansible_collections/infra_mcp/core && ansible-lint)
(cd ansible && infra-mcp lint . && ansible-lint)
```

La CI (`.github/workflows/ci.yml`) lance tout cela sur chaque PR, plus le test
de bout en bout des stacks sur un vrai swarm. Comme le timer
déploie tout ce qui est mergé, protège la branche principale en exigeant
qu'elle passe.

## Limites connues

- **Premier passage** : sur une machine neuve, `infra-mcp-admin add-host`
  (voir « Ajouter une machine ») ; ensuite, root ne peut plus se connecter en
  SSH.
- **Check mode** : les tâches `command` / `shell` ne sont pas simulées, elles
  sont sautées par `plan`. Préfère les modules qui gèrent le check mode (`stat`,
  `slurp`, `getent`, `package_facts`…). `check_mode: false` et le lookup `pipe`
  sont interdits, et la CI refuse une PR qui en contient : ils feraient exécuter
  quelque chose pour de vrai par `plan`, qui n'exige qu'un token `read`.
- **Docker et ufw** : les ports publiés par Docker contournent ufw. Le firewall
  du rôle `base` protège les services de l'hôte, pas les conteneurs.
- **Docker géré ailleurs** : `docker_engine_managed: false` dans le
  `host_vars` d'une machine où un autre outil gère Docker.
- **Redémarrages** : décale `updates_reboot_time` entre les machines (dans
  l'exemple : managers à 04:00, workers à 04:30, contrôleur à 05:00), pour que
  le parc ne tombe pas d'un coup.
- **Comptes retirés** : la suppression échoue si le compte a encore des
  processus (session ouverte, cron). Termine-les, puis relance `apply`.
- **Stacks** : `plan` voit les changements par service, pas les services
  retirés d'un compose (`apply` les supprime, grâce à `--prune`). Les configs
  `file:` ne sont pas encore prises en charge. Les images ne sont pas
  résolues par le registre (sinon chaque apply redémarrait les services) :
  pousser une nouvelle image sous le même tag n'est pas détecté. Change le tag,
  ou fige l'image par son digest (`image@sha256:…`).
- **Swarm** : seul le join des workers est automatisé. La promotion d'un manager
  reste manuelle, et le token manager n'est jamais manipulé.

# infra-mcp

Serveur MCP (FastMCP, Streamable HTTP) qui pilote un repo Ansible. Claude peut
lire l'état du parc, simuler un changement et, avec un token d'écriture,
appliquer un playbook existant. Il ne peut rien exécuter d'autre.

Le projet livre deux choses, versionnées ensemble :
- **l'outil** (`src/infra_mcp/`) : le serveur MCP et la commande `infra-mcp lint` ;
- **le socle Ansible** (collection `infra_mcp.core`, `src/ansible_collections/`) :
  ce que tout serveur reçoit, avec ses garde-fous.

`ansible/` est un repo de config : inventaire, variables, stacks, vault, et des
playbooks d'une ligne qui importent ceux du socle. Il a vocation à vivre dans
son propre repo.

## Modèle de sécurité

| Outil            | Niveau | Effet                                                        |
|------------------|--------|--------------------------------------------------------------|
| `list_hosts`     | read   | Arborescence de l'inventaire                                 |
| `host_vars`      | read   | Variables effectives d'un hôte, secrets masqués              |
| `list_playbooks` | read   | Playbooks présents dans le repo                              |
| `repo_status`    | read   | Commit actuellement déployé sur le contrôleur                |
| `plan`           | read   | `--check --diff` : ce qui changerait, sans rien toucher      |
| `apply`          | write  | Exécute un playbook du repo, `limit` obligatoire             |

Volontairement absents : commande shell, tâche ad hoc, création de playbook,
lecture des secrets. Le vault est déchiffré par Ansible au moment d'exécuter,
jamais renvoyé à Claude : `host_vars` masque les variables `vault_*` et les
tâches qui manipulent des secrets sont en `no_log`. Un token `read` ne voit même
pas `apply` dans la liste des outils.

- Les tokens sont stockés sous forme d'empreinte SHA-256 et relus à chaque
  requête : une révocation est immédiate, sans redémarrage.
- Chaque appel (refus compris) est journalisé dans `audit.jsonl` avec le token
  utilisé, les arguments et la durée.
- Un seul `apply` à la fois.
- Les entrées sont validées : `limit` n'accepte que des noms d'hôtes ou de
  groupes, `playbook` que des fichiers de `ansible/playbooks/`.

Le point sensible : le contrôleur détient une clé SSH avec sudo sur tout le
parc. Toute la sécurité repose sur les outils exposés ci-dessus et sur le fait
que les playbooks ne changent que par commit dans le repo.

## Workflow

1. Claude (ou toi) modifie `group_vars` / `host_vars` et ouvre une PR.
2. Après le merge, le timer `infra-mcp-sync` fait un `git pull` sur le contrôleur.
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

Ses valeurs par défaut (`roles/base/defaults/main.yml`) se surchargent dans
l'inventaire. Ses **verrous** (`roles/base/vars/main.yml`, aujourd'hui le
firewall en deny) ne peuvent pas l'être : le rôle échoue si un étage essaie.
Un repo de config peut ajouter ses propres verrous avec `locked_vars`.

`infra-mcp lint <dossier>` vérifie un repo de config avec les règles de l'outil
(plan reste une simulation, disposition des variables, politique des stacks) :
à lancer dans sa CI, avec la même version de l'outil.

## Configuration à étages

Les étages suivent l'inventaire : `group_vars/all.yml` → `group_vars/<groupe>.yml`
→ `host_vars/<hôte>.yml`.

- **Valeurs simples** : priorité Ansible classique, l'hôte gagne sur le groupe,
  qui gagne sur le global.
- **Listes** : elles s'additionnent. Chaque étage définit sa propre variable avec
  un suffixe (`packages_present_global`, `packages_present_swarm`,
  `packages_present_host`) et le rôle `base` fusionne tout ce qui commence par
  `packages_present_`, y compris `packages_present_base` du socle. Même principe pour `packages_absent_`, `firewall_allow_`,
  `firewall_remove_`, `admin_users_` et `users_absent_`.
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

- **Une stack** = `ansible/stacks/<nom>/compose.yml`, déclarée dans
  `swarm_stacks` (`group_vars/swarm_managers/stacks.yml`). La retirer de la
  liste ne la supprime pas : liste-la dans `swarm_stacks_absent`.
- **Secrets** : chiffrés avec ansible-vault dans
  `group_vars/swarm_managers/vault.yml`, sous des noms `vault_*` (le playbook
  refuse les autres : `host_vars` les afficherait en clair). `swarm_secrets`
  les transforme en secrets Docker ; le compose les déclare `external: true` et
  la stack les liste dans `secrets`. Changer une valeur crée une nouvelle
  version (`nom_v2`…) et redéploie les stacks concernées.

  ```bash
  ansible-vault create ansible/inventory/group_vars/swarm_managers/vault.yml
  ansible-vault edit   ansible/inventory/group_vars/swarm_managers/vault.yml
  ```
- **plan** : `docker_stack` ne sait pas simuler. Chaque service porte l'empreinte
  de son compose (label `infra.stack-hash`) ; `plan stacks` la compare à celle
  qui tourne et annonce « à créer », « à redéployer » ou « à supprimer ».
- **Politique** (vérifiée par `infra-mcp lint`) : image avec
  un tag explicite (jamais `latest`), pas de `privileged`, réseau/pid/ipc de
  l'hôte, `cap_add`, `devices`, montage du socket Docker ou de chemins sensibles
  de l'hôte. Une exception se déclare sur le service, avec sa raison :
  `x-allow: {docker-socket: "Portainer pilote Docker"}`. Elle reste visible en
  revue de PR.

## Installation sur le contrôleur

```bash
# Utilisateur dédié, avec sa clé SSH vers le parc
sudo useradd --system --create-home --home-dir /var/lib/infra-mcp --shell /usr/sbin/nologin infra-mcp
sudo install -d -m 700 -o infra-mcp -g infra-mcp /var/lib/infra-mcp/.ssh
sudo -u infra-mcp ssh-keygen -t ed25519 -N "" -f /var/lib/infra-mcp/.ssh/id_ed25519
# → ajoute la clé publique dans admin_users_global (group_vars/all.yml)

# Clés d'hôte du parc (host_key_checking est actif : sans elles, Ansible refuse
# de se connecter). Compare les empreintes affichées avec celles de chaque
# serveur (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub, depuis la console
# de l'hébergeur) avant de les accepter.
ssh-keyscan -t ed25519 203.0.113.10 203.0.113.11 203.0.113.12 > /tmp/known_hosts
ssh-keygen -lf /tmp/known_hosts
sudo install -m 644 -o infra-mcp -g infra-mcp /tmp/known_hosts /var/lib/infra-mcp/.ssh/known_hosts
# Pour un hôte ajouté plus tard : même chose, en ajoutant (>>) au fichier existant.

# Code et repo
sudo mkdir -p /opt/infra-mcp /opt/infra /etc/infra-mcp /var/log/infra-mcp
sudo chown infra-mcp: /opt/infra /etc/infra-mcp /var/log/infra-mcp
sudo -u infra-mcp git clone git@github.com:renaud/infra.git /opt/infra
sudo python3 -m venv /opt/infra-mcp/venv
sudo /opt/infra-mcp/venv/bin/pip install /chemin/vers/infra-mcp
# (installe aussi le socle infra_mcp.core et le paquet ansible complet)

# Mot de passe ansible-vault (le même que sur ton poste, à garder dans ton
# gestionnaire de mots de passe). Saisi sans écho ni historique, lisible par
# le service seulement.
read -rsp 'Mot de passe vault : ' P && printf '%s\n' "$P" \
  | sudo install -m 400 -o infra-mcp -g infra-mcp /dev/stdin /etc/infra-mcp/vault-pass; unset P

# Services
sudo cp deploy/infra-mcp*.service deploy/infra-mcp-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now infra-mcp infra-mcp-sync.timer
```

Puis ajoute `deploy/traefik-infra-mcp.yml` dans les configurations dynamiques
du proxy Coolify.

## Tokens

Toujours en tant que `infra-mcp`, pour que le service puisse lire le fichier :

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
claude mcp add --transport http infra https://mcp.example.fr/mcp \
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

- **Premier passage** : le rôle `base` crée l'utilisateur admin. Sur une machine
  neuve, lance-le une fois à la main depuis le contrôleur, avec l'Ansible du
  venv (qui contient le socle) :
  `/opt/infra-mcp/venv/bin/ansible-playbook playbooks/site.yml -l <hôte> -e ansible_user=root`.
  Après ce passage, root ne peut plus se connecter en SSH : vérifie que ta clé
  et celle du contrôleur sont bien dans `admin_users_global`.
- **Check mode** : les tâches `command` / `shell` ne sont pas simulées, elles
  sont sautées par `plan`. Préfère les modules qui gèrent le check mode (`stat`,
  `slurp`, `getent`, `package_facts`…). `check_mode: false` et le lookup `pipe`
  sont interdits, et la CI refuse une PR qui en contient : ils feraient exécuter
  quelque chose pour de vrai par `plan`, qui n'exige qu'un token `read`.
- **Docker et ufw** : les ports publiés par Docker contournent ufw. Le firewall
  du rôle `base` protège les services de l'hôte, pas les conteneurs.
- **Serveur Coolify** : `docker_engine_managed: false` dans son `host_vars`,
  Coolify gère Docker lui-même.
- **Redémarrages** : managers à 04:00, workers du swarm à 04:30, srv-01
  (Coolify, contrôleur) à 05:00. Avec plusieurs workers, décale-les aussi
  entre eux dans leur `host_vars`.
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

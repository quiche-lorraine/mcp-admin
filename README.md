# infra-mcp

Serveur MCP (FastMCP, Streamable HTTP) qui pilote un repo Ansible. Claude peut
lire l'état du parc, simuler un changement et, avec un token d'écriture,
appliquer un playbook existant. Il ne peut rien exécuter d'autre.

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
accès Vault. Un token `read` ne voit même pas `apply` dans la liste des outils.

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
3. `plan site` montre l'effet ; `apply site` l'applique.

`plan site` sur `all` sert aussi de détection de dérive : tout `changed` est un
écart entre une machine et le repo.

## Configuration à étages

Les étages suivent l'inventaire : `group_vars/all.yml` → `group_vars/<groupe>.yml`
→ `host_vars/<hôte>.yml`.

- **Valeurs simples** : priorité Ansible classique, l'hôte gagne sur le groupe,
  qui gagne sur le global.
- **Listes** : elles s'additionnent. Chaque étage définit sa propre variable avec
  un suffixe (`packages_present_global`, `packages_present_swarm`,
  `packages_present_host`) et le rôle `base` fusionne tout ce qui commence par
  `packages_present_`. Même principe pour `packages_absent_`, `firewall_allow_`
  et `admin_users_`.
- **Valeurs verrouillées** : `policy/locked.yml`, chargé par les playbooks avec
  une priorité supérieure à l'inventaire. Si un étage tente de les modifier, le
  rôle `base` échoue avant de toucher quoi que ce soit.

## Installation sur le contrôleur

```bash
# Utilisateur dédié, avec sa clé SSH vers le parc
sudo useradd --system --create-home --home-dir /var/lib/infra-mcp --shell /usr/sbin/nologin infra-mcp
sudo -u infra-mcp ssh-keygen -t ed25519 -N "" -f /var/lib/infra-mcp/.ssh/id_ed25519
# → ajoute la clé publique dans admin_users_global (group_vars/all.yml)

# Code et repo
sudo mkdir -p /opt/infra-mcp /opt/infra /etc/infra-mcp /var/log/infra-mcp
sudo chown infra-mcp: /opt/infra /etc/infra-mcp /var/log/infra-mcp
sudo -u infra-mcp git clone git@github.com:renaud/infra.git /opt/infra
sudo python3 -m venv /opt/infra-mcp/venv
sudo /opt/infra-mcp/venv/bin/pip install /chemin/vers/infra-mcp ansible
# (le paquet « ansible » complet embarque les collections de requirements.yml)

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

## Limites connues

- **Premier passage** : le rôle `base` crée l'utilisateur admin. Sur une machine
  neuve, lance-le une fois à la main depuis le contrôleur avec
  `ansible-playbook playbooks/site.yml -l <hôte> -e ansible_user=root`.
- **Check mode** : les tâches `command` / `shell` ne sont pas simulées. Évite-les
  dans les rôles, ou marque-les `check_mode: false` quand elles sont en lecture.
- **Docker et ufw** : les ports publiés par Docker contournent ufw. Le firewall
  du rôle `base` protège les services de l'hôte, pas les conteneurs.
- **Serveur Coolify** : `docker_engine_managed: false` dans son `host_vars`,
  Coolify gère Docker lui-même.
- **Swarm** : seul le join des workers est automatisé. La promotion d'un manager
  reste manuelle, et le token manager n'est jamais manipulé.

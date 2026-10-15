"""user-data cloud-init pour une nouvelle machine : infra-mcp cloud-init <hôte>.

À coller à la création du serveur chez l'hébergeur. La machine démarre avec les
comptes admin du socle (clés SSH et sudo), sans root ni mot de passe : le premier
passage d'Ansible se fait alors comme les suivants, sans jamais se connecter en root.

Le fichier sudo est écrit au format exact du rôle base (marqueur compris) : le rôle
le reconnaît ensuite comme le sien et le gère en exclusif. Un sudo posé par
cloud-init lui-même (90-cloud-init-users) échapperait à cette gestion.
"""

import yaml

SUDO_MARKER = "# ansible-managed: role base"


class _Dumper(yaml.SafeDumper):
    """Textes sur plusieurs lignes en bloc littéral (|), comme on les écrirait à la main."""


_Dumper.add_representer(
    str,
    lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None),
)


def admin_users(hostvars: dict) -> list[dict]:
    """Les admins de toutes les couches (admin_users_global, _<groupe>, _host…)."""
    users = []
    for key in sorted(hostvars):
        if key.startswith("admin_users_"):
            users += hostvars[key] or []
    return users


def user_data(host: str, hostvars: dict) -> str:
    admins = [u for u in admin_users(hostvars) if u.get("ssh_keys")]
    sudoers = [u for u in admins if u.get("sudo")]
    if not sudoers:
        raise ValueError(f"{host} : aucun admin avec sudo et une clé SSH dans admin_users_*")
    ansible_user = hostvars.get("ansible_user", "root")
    if ansible_user != "root" and ansible_user not in {u["name"] for u in sudoers}:
        raise ValueError(f"{host} : ansible_user ({ansible_user}) doit être un admin avec sudo et une clé")
    config = {
        "disable_root": True,
        "ssh_pwauth": False,
        "users": [
            {"name": u["name"], "shell": "/bin/bash", "lock_passwd": True, "ssh_authorized_keys": list(u["ssh_keys"])}
            for u in admins
        ],
        "write_files": [
            {
                "path": f"/etc/sudoers.d/90-{u['name']}",
                "owner": "root:root",
                "permissions": "0440",
                "content": f"{SUDO_MARKER}\n{u['name']} ALL=(ALL) NOPASSWD:ALL\n",
            }
            for u in sudoers
        ],
    }
    header = (
        "#cloud-config\n"
        f"# infra-mcp : premier démarrage de {host}. Comptes admin du socle avec leurs\n"
        "# clés et leur sudo, sans root ni mot de passe. Ensuite : infra-mcp-admin add-host.\n"
    )
    return header + yaml.dump(config, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)

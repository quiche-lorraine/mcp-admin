"""Commandes d'application (app_query, app_command) : déclaration vérifiée par le
lint, paramètres vérifiés à l'appel, rien d'autre ne s'exécute."""

import shutil

import pytest
import yaml
from fastmcp.exceptions import ToolError

from conftest import ANSIBLE_DIR
from infra_mcp import appcmd, config, lint

SERVICES = {"php", "database"}
CREATE = {"service": "php", "argv": ["php", "bin/console", "app:user:create", "{email}", "--organization={org}"],
          "params": {"email": r"[^@\s]+@[^@\s]+", "org": r"[\w .-]{1,64}"}}


def test_declaration_valide():
    assert lint.command_violations(CREATE, SERVICES) == []
    assert lint.command_violations({"service": "database", "level": "read", "timeout": 30, "secret_output": False,
                                    "argv": ["psql", "-U", "app", "-c", "SELECT 1"]}, SERVICES) == []


@pytest.mark.parametrize("cmd, expected", [
    ({**CREATE, "service": "absent"}, "absent du compose"),
    ({**CREATE, "argv": ["sh", "-c", "id"], "params": {}}, "ne peut pas lancer"),
    ({**CREATE, "argv": ["/bin/bash", "x"], "params": {}}, "ne peut pas lancer"),
    ({**CREATE, "argv": ["{email}"], "params": {"email": "x"}}, "ne peut pas lancer"),
    ({**CREATE, "argv": ["psql", "-c", "SELECT * FROM u WHERE e = '{email}'"], "params": {"email": "x"}},
     "argument entier"),
    ({**CREATE, "argv": ["php", "-r", "{email}"], "params": {"email": "x"}}, "du code à exécuter"),
    ({**CREATE, "argv": ["php", "bin/console", "{autre}"]}, "'autre' absent de params"),
    ({**CREATE, "argv": ["php", "bin/console", "{email}"]}, "'org' n'apparaît pas"),
    ({**CREATE, "params": {"email": "(", "org": "x"}}, "expression invalide"),
    ({**CREATE, "level": "admin"}, "read ou write"),
    ({**CREATE, "timeout": 3600}, "timeout"),
    ({**CREATE, "timeout": True}, "int attendu"),
    ({**CREATE, "argv": []}, "liste non vide"),
    ({**CREATE, "shell": True}, "clé inconnue 'shell'"),
    ("php bin/console", "dictionnaire attendu"),
])
def test_declaration_refusee(cmd, expected):
    problems = lint.command_violations(cmd, SERVICES)
    assert any(expected in p for p in problems), problems


def test_arguments():
    argv = appcmd.build_argv(CREATE, {"email": "a@b.fr", "org": "Quiche Lorraine"})
    assert argv == ["php", "bin/console", "app:user:create", "a@b.fr", "--organization=Quiche Lorraine"]
    with pytest.raises(ToolError, match="valeur refusée"):
        appcmd.build_argv(CREATE, {"email": "a@b.fr; id", "org": "x"})
    with pytest.raises(ToolError, match="ne commence pas par -"):
        appcmd.build_argv(CREATE, {"email": "--help@x", "org": "x"})
    loose = {"service": "php", "argv": ["php", "bin/console", "{x}"], "params": {"x": ".+"}}
    with pytest.raises(ToolError, match="balise"):
        appcmd.build_argv(loose, {"x": "{{ lookup('pipe', 'id') }}"})
    with pytest.raises(ToolError, match="Jinja"):
        appcmd.build_argv({"service": "php", "argv": ["echo", "{{ x }}"]}, {})
    with pytest.raises(ToolError, match="manquants : org"):
        appcmd.build_argv(CREATE, {"email": "a@b.fr"})
    with pytest.raises(ToolError, match="inconnus : x"):
        appcmd.build_argv(CREATE, {"email": "a@b.fr", "org": "o", "x": "1"})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    path = root / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    text = path.read_text().replace("swarm_stacks: []", "swarm_stacks:\n  - name: whoami\n    secrets: [whoami_token]")
    data = yaml.safe_load(text)
    data["swarm_secrets"] = {"whoami_token": "x"}
    data["swarm_stacks"][0]["commands"] = {"version": {"service": "web", "argv": ["/whoami", "-h"], "level": "read"}}
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(config, "ANSIBLE_DIR", root)
    return root


def test_lint_du_repo(repo):
    assert [p for p in lint.stacks_inventory_violations(repo) if "commande" in p] == []
    path = repo / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    data = yaml.safe_load(path.read_text())
    data["swarm_stacks"][0]["commands"] = {"Shell": {"service": "web", "argv": ["sh"]}}
    path.write_text(yaml.safe_dump(data))
    problems = lint.stacks_inventory_violations(repo)
    assert any("commande 'Shell' : nom invalide" in p for p in problems)
    assert any("commande Shell : argv : 'sh' ne peut pas lancer" in p for p in problems)


def test_commande_declaree(repo):
    assert appcmd.find("whoami", "version")["argv"] == ["/whoami", "-h"]
    with pytest.raises(ToolError, match=r"commande inconnue pour whoami : sh \(déclarées : version\)"):
        appcmd.find("whoami", "sh")
    with pytest.raises(ToolError, match="stack inconnue"):
        appcmd.find("absente", "version")

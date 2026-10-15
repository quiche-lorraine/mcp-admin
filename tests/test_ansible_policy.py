"""Garantit que `plan`, accessible avec un token read, reste une simulation.

`plan` lance les playbooks en --check. Deux choses font quand même exécuter
quelque chose pour de vrai :
- `check_mode: false` sur une tâche, un bloc ou un play : elle tourne en réel ;
- le lookup `pipe` : il lance une commande sur le contrôleur, même en check mode.
Les deux sont donc interdits dans le repo Ansible.
"""

import re
from pathlib import Path

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).resolve().parent.parent / "ansible"
YAML_FILES = sorted(p for p in ANSIBLE_DIR.rglob("*.yml") if p.is_file())

_PIPE_RE = re.compile(r"""(lookup|query|q)\(\s*['"](ansible\.builtin\.)?pipe['"]|with_pipe\b""")


class _Loader(yaml.SafeLoader):
    """Accepte les tags Ansible (!vault, !unsafe…) sans les interpréter."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _check_mode_violations(node, where="tâche sans nom"):
    if isinstance(node, dict):
        where = f"« {node['name']} »" if "name" in node else where
        for key, value in node.items():
            if key == "check_mode" and value is not True:
                yield f"{where} : check_mode: {value!r}"
            yield from _check_mode_violations(value, where)
    elif isinstance(node, list):
        for item in node:
            yield from _check_mode_violations(item, where)


def test_fichiers_trouves():
    assert any(p.parent.name == "playbooks" for p in YAML_FILES)


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(ANSIBLE_DIR)))
def test_pas_d_execution_reelle_en_check_mode(path):
    text = path.read_text()
    violations = list(_check_mode_violations(yaml.load(text, Loader=_Loader)))
    violations += [f"ligne {text[:m.start()].count(chr(10)) + 1}: lookup pipe" for m in _PIPE_RE.finditer(text)]
    assert not violations, "interdit, plan doit rester une simulation :\n" + "\n".join(violations)


def test_le_garde_fou_detecte_les_violations():
    playbook = yaml.load(
        """
- hosts: all
  tasks:
    - block:
        - name: lecture
          ansible.builtin.command: cat /etc/hostname
          check_mode: false
""",
        Loader=_Loader,
    )
    assert len(list(_check_mode_violations(playbook))) == 1
    assert _PIPE_RE.search("{{ lookup('pipe', 'id') }}")
    assert _PIPE_RE.search('{{ query("ansible.builtin.pipe", "id") }}')
    assert not _PIPE_RE.search("{{ lookup('ansible.builtin.vars', 'pipeline') }}")


def test_pas_de_fichier_et_dossier_de_meme_nom():
    # group_vars/x.yml et group_vars/x/ ensemble : Ansible ignore le fichier en
    # silence, et toutes ses variables disparaissent.
    for kind in ("group_vars", "host_vars"):
        base = ANSIBLE_DIR / "inventory" / kind
        if not base.is_dir():
            continue
        clashes = [d.name for d in base.iterdir() if d.is_dir() and (base / f"{d.name}.yml").exists()]
        assert not clashes, f"{kind} : {clashes} existent à la fois en fichier .yml et en dossier"

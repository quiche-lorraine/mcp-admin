"""Comparaison de l'outil installé à la version figée par requirements.txt."""

import pytest

from infra_mcp import version

SHA = "a18ea36f10a2b56694d28d084ef5969c1cc04cc9"
GIT = f"infra-mcp @ git+https://github.com/quiche-lorraine/mcp-admin@{SHA}"


@pytest.mark.parametrize(
    "requirement, tool, ok",
    [
        (GIT, {"version": "0.1.0", "commit": SHA}, True),
        (GIT, {"version": "0.1.0", "commit": "b" * 40}, False),
        (GIT, {"version": "0.1.0", "commit": None}, False),
        ("infra-mcp==0.2.0", {"version": "0.2.0", "commit": None}, True),
        ("infra-mcp==0.2.0", {"version": "0.2.1", "commit": None}, False),
        ("infra-mcp==0.2.0", {"version": "0.2.0", "commit": SHA}, False),  # installé depuis Git
        (f"infra-mcp @ git+https://github.com/x/y@main", {"version": "0.1.0", "commit": SHA}, False),
    ],
)
def test_correspondance(requirement, tool, ok):
    assert version.matches(requirement, tool) is ok


def test_fichier(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "installed", lambda: {"version": "0.1.0", "commit": SHA})
    req = tmp_path / "requirements.txt"
    req.write_text(f"# Version de l'outil\n{GIT}\n")
    assert version.main([str(req)]) == 0
    req.write_text(f"{GIT}\ninfra-mcp==0.1.0\n")
    assert version.main([str(req)]) == 1


def test_outil_installe():
    tool = version.installed()
    assert tool["version"] and set(tool) == {"version", "commit"}

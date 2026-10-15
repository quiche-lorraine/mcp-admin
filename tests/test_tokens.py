import stat

from infra_mcp import tokens


async def test_cycle_de_vie(tmp_path):
    path = tmp_path / "tokens.json"
    token = tokens.create(path, "read", "claude-code-web")
    _, token_id, secret = token.split("_", 2)

    assert secret not in path.read_text()  # seule l'empreinte est stockée
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert tokens.listing(path) == {
        token_id: {"level": "read", "label": "claude-code-web", "created": tokens._load(path)[token_id]["created"]}
    }

    verifier = tokens.FileTokenVerifier(path)
    access = await verifier.verify_token(token)
    assert access.scopes == ["read"]
    assert access.client_id == f"{token_id}:claude-code-web"

    assert tokens.revoke(path, token_id)
    assert not tokens.revoke(path, token_id)
    assert await verifier.verify_token(token) is None  # révocation immédiate


async def test_write_inclut_read(tmp_path):
    path = tmp_path / "tokens.json"
    access = await tokens.FileTokenVerifier(path).verify_token(tokens.create(path, "write", "x"))
    assert access.scopes == ["read", "write"]


async def test_tokens_refuses(tmp_path):
    path = tmp_path / "tokens.json"
    token = tokens.create(path, "write", "x")
    _, token_id, secret = token.split("_", 2)
    verifier = tokens.FileTokenVerifier(path)
    for bad in [
        "",
        "Bearer " + token,
        f"imcp_{token_id}_{secret[:-1]}x",  # mauvais secret
        f"imcp_ffffffff_{secret}",  # id inconnu
        f"xxxx_{token_id}_{secret}",
        f"imcp_{token_id}",
    ]:
        assert await verifier.verify_token(bad) is None, bad


def test_niveau_inconnu(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        tokens.create(tmp_path / "tokens.json", "admin", "x")

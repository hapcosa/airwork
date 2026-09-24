import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from airwork_agent.audit import redact
from airwork_agent.auth import AccessVerifier, check_origin
from airwork_agent.config import Settings
from airwork_agent.store import InvalidInput, Store

TOKEN = "sk-ant-oat01-" + "A" * 40


def test_cuenta_requiere_perfil_y_sin_base_url(env, store):
    st = store
    with pytest.raises(InvalidInput):
        st.create_account("noexiste")
    with pytest.raises(InvalidInput):
        st.create_account("mala")
    with pytest.raises(InvalidInput):
        st.create_account("../cuenta1")
    acc = st.create_account("cuenta1", "Principal")
    assert acc["profile"]["logged_in"] and not acc["has_token"]
    assert st.create_account("cuenta2")["profile"]["logged_in"] is False
    with pytest.raises(InvalidInput):
        st.create_account("cuenta1")


def test_token_desactivado_por_defecto(env, store):
    st = store
    with pytest.raises(InvalidInput):
        st.create_account("cuenta1", token=TOKEN)


def test_token_cifrado_y_entorno_del_hijo(env, store):
    env.allow_tokens = True
    st = store
    acc = st.create_account("cuenta1", token=TOKEN, use_headroom=True)
    assert acc["has_token"] and "token" not in json.dumps(acc).replace("has_token", "")
    assert TOKEN.encode() not in env.db_path.read_bytes()
    assert env.key_path.stat().st_mode & 0o777 == 0o600
    assert env.db_path.stat().st_mode & 0o777 == 0o600
    child = st.child_env(acc["id"])
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert child["CLAUDE_CONFIG_DIR"].endswith("/.claude-accounts/cuenta1")
    assert child["ANTHROPIC_BASE_URL"] == env.headroom_url
    st.update_account(acc["id"], use_headroom=False, clear_token=True)
    assert st.child_env(acc["id"]) == {"CLAUDE_CONFIG_DIR": child["CLAUDE_CONFIG_DIR"]}


def test_token_con_formato_invalido(env, store):
    env.allow_tokens = True
    with pytest.raises(InvalidInput):
        store.create_account("cuenta1", token="sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxx")


def test_preferencias_sesion_gana_a_proyecto(env, store):
    st = store
    a1 = st.create_account("cuenta1")["id"]
    a2 = st.create_account("cuenta2")["id"]
    st.set_prefs("project", "demo", account_id=a1, model="sonnet", effort="high")
    st.set_prefs("session", "s1", account_id=a2)
    eff = st.effective_prefs("demo", "s1")
    assert eff == {"account_id": a2, "model": "sonnet", "effort": "high", "permission_mode": None}
    with pytest.raises(InvalidInput):
        st.set_prefs("project", "demo", permission_mode="bypassPermissions")
    with pytest.raises(InvalidInput):
        st.set_prefs("project", "demo", effort="extremo")


def test_redact():
    assert redact(f"falló con {TOKEN} y sk-ant-api03-zzz") == "falló con sk-ant-*** y sk-ant-***"


# --- JWT de Access -------------------------------------------------------------------

def _request(headers: dict, method="GET") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": method, "path": "/api/pc", "headers": raw,
                    "client": ("127.0.0.1", 5000), "query_string": b""})


@pytest.fixture
def access(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = Settings(home=tmp_path, access_team_domain="team.cloudflareaccess.com", access_aud="aud123",
                 allowed_emails=("yo@example.com",))
    v = AccessVerifier(s)

    class _K:
        def __init__(self):
            self.key = key.public_key()

    monkeypatch.setattr(v._jwks, "get_signing_key_from_jwt", lambda t: _K())

    def make(**over):
        now = int(time.time())
        claims = {"email": "yo@example.com", "aud": "aud123", "iss": "https://team.cloudflareaccess.com",
                  "iat": now, "exp": now + 600, **over}
        return jwt.encode(claims, key, algorithm="RS256")

    return v, make


def test_jwt_valido(access):
    v, make = access
    assert v.identify(_request({"Cf-Access-Jwt-Assertion": make()})).email == "yo@example.com"


@pytest.mark.parametrize("over,code", [
    ({"aud": "otra"}, 401), ({"iss": "https://evil.cloudflareaccess.com"}, 401),
    ({"exp": int(time.time()) - 10}, 401), ({"email": "otro@example.com"}, 403),
])
def test_jwt_invalido(access, over, code):
    v, make = access
    with pytest.raises(HTTPException) as e:
        v.identify(_request({"Cf-Access-Jwt-Assertion": make(**over)}))
    assert e.value.status_code == code


def test_sin_jwt_401_y_dev_solo_fuera_de_cloudflare(access, tmp_path):
    v, _ = access
    with pytest.raises(HTTPException) as e:
        v.identify(_request({}))
    assert e.value.status_code == 401
    dev = AccessVerifier(Settings(home=tmp_path, dev=True))
    assert dev.identify(_request({})).email == "dev@local"
    with pytest.raises(HTTPException):
        dev.identify(_request({"Cf-Ray": "abc"}))


def test_check_origin():
    check_origin(_request({"Host": "pc1.x.cl", "Origin": "https://pc1.x.cl", "Content-Type": "application/json",
                           "Content-Length": "2"}, "POST"))
    with pytest.raises(HTTPException) as e:
        check_origin(_request({"Host": "pc1.x.cl", "Origin": "https://evil.cl"}, "POST"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        check_origin(_request({"Host": "pc1.x.cl", "Content-Type": "text/plain", "Content-Length": "5"}, "POST"))
    assert e.value.status_code == 415


def test_settings_se_niega_a_servir_sin_access(tmp_path):
    with pytest.raises(RuntimeError):
        Settings(home=tmp_path).validate_for_serving()
    with pytest.raises(RuntimeError):
        Settings(home=tmp_path, dev=True, host="0.0.0.0").validate_for_serving()
    Settings(home=tmp_path, dev=True).validate_for_serving()

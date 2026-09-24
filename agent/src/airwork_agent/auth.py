"""Validación del JWT de Cloudflare Access (Cf-Access-Jwt-Assertion) y chequeo de origen."""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from .config import Settings


@dataclass
class Identity:
    email: str
    ip: str


class AccessVerifier:
    def __init__(self, s: Settings):
        self.s = s
        self._jwks: jwt.PyJWKClient | None = None
        if s.access_team_domain:
            self._jwks = jwt.PyJWKClient(
                f"https://{s.access_team_domain}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
            )

    def decode(self, token: str) -> dict:
        if self._jwks is None:
            raise jwt.InvalidTokenError("Access no está configurado")
        key = self._jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key, algorithms=["RS256"], audience=self.s.access_aud,
            issuer=f"https://{self.s.access_team_domain}", options={"require": ["exp", "iat", "aud", "iss"]},
        )

    def identify(self, request: Request) -> Identity:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
        token = request.headers.get("cf-access-jwt-assertion")
        # Modo desarrollo: solo sin JWT y solo si la petición no pasó por Cloudflare.
        if self.s.dev and not token and "cf-ray" not in request.headers:
            return Identity("dev@local", ip)
        if not token:
            raise HTTPException(401, "Falta Cf-Access-Jwt-Assertion")
        try:
            claims = self.decode(token)
        except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
            raise HTTPException(401, f"JWT de Access inválido: {type(e).__name__}") from None
        email = str(claims.get("email", "")).lower()
        if not email or email not in self.s.allowed_emails:
            raise HTTPException(403, "Email no autorizado")
        return Identity(email, ip)


def check_origin(request: Request) -> None:
    """Defensa CSRF: toda escritura exige JSON y un Origin igual al propio host."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    ctype = request.headers.get("content-type", "")
    if request.headers.get("content-length", "0") not in ("", "0") and not ctype.startswith("application/json"):
        raise HTTPException(415, "Solo application/json")
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin is None:
        return  # clientes no navegador (curl, tests); Access igual exige el JWT
    if origin.split("://", 1)[-1] != host:
        raise HTTPException(403, "Origin distinto del host")

# Fase 2 — Despliegue de airwork-agent

Un PC = un agente + un túnel + una aplicación de Cloudflare Access. Para sumar otro PC se repite
todo con otro hostname (`<pc>.<dominio>`); la PWA de cada PC enlaza a los demás.

```
celular ──HTTPS──> Cloudflare (Access: login + JWT) ──túnel saliente──> cloudflared (PC)
                                                                          │
                                                          http://127.0.0.1:8790  airwork-agent
```

- El agente escucha solo en `127.0.0.1` y se niega a arrancar si se le pide otra interfaz.
- No se abre ningún puerto en el router: `cloudflared` sale hacia Cloudflare.
- Doble control: Access bloquea en el borde y el agente valida además el JWT
  (`Cf-Access-Jwt-Assertion`: firma, `aud`, `iss`, expiración y email permitido).
- Este túnel es independiente de cualquier otro que ya tengas en `~/.cloudflared`: tiene su
  propia config en `~/.config/airwork/cloudflared.yml` y su propia credencial.

Archivos del repo:

| Archivo | Destino |
|---|---|
| `systemd/airwork-agent.service` | `~/.config/systemd/user/` |
| `systemd/cloudflared-airwork.service` | `~/.config/systemd/user/` |
| `config/agent.env.example` | `~/.config/airwork/agent.env` (600) |
| `config/cloudflared-airwork.example.yml` | `~/.config/airwork/cloudflared.yml` |

## 1. Agente

```sh
cd ~/programacion/airwork/agent
uv sync --frozen --no-dev              # crea .venv/bin/airwork-agent
install -Dm600 ../config/agent.env.example ~/.config/airwork/agent.env
install -Dm644 ../systemd/airwork-agent.service ~/.config/systemd/user/airwork-agent.service
```

Completa `agent.env` después de crear la aplicación de Access (paso 3). Hasta entonces el
agente no arranca: es a propósito.

## 2. Túnel

Se usa un túnel con nombre, gestionado localmente.

```sh
cloudflared tunnel create airwork-<pc>        # imprime el UUID y crea ~/.cloudflared/<UUID>.json
```

- `tunnel create` usa `~/.cloudflared/cert.pem`. Si ese certificado es de la misma cuenta de
  Cloudflare donde está tu dominio, sirve tal cual.
- **No ejecutes `cloudflared tunnel login` si `cert.pem` ya existe**: lo reemplazaría y afectaría
  a los otros túneles que dependen de él. Si el dominio está en otra cuenta, crea el túnel desde
  el panel (Zero Trust → Networks → Tunnels) y usa esa credencial.
- El `.json` del túnel es un secreto: queda en `~/.cloudflared`, fuera del repo.

DNS: un CNAME `<pc>` → `<UUID>.cfargotunnel.com` con proxy activado. Se crea desde el panel
de DNS o con `cloudflared tunnel route dns airwork-<pc> <pc>.<dominio>`. Revisa en el panel que
haya quedado en la zona correcta.

```sh
install -Dm644 ../config/cloudflared-airwork.example.yml ~/.config/airwork/cloudflared.yml
$EDITOR ~/.config/airwork/cloudflared.yml     # UUID, <pc>.<dominio>
cloudflared tunnel --config ~/.config/airwork/cloudflared.yml ingress validate
install -Dm644 ../systemd/cloudflared-airwork.service ~/.config/systemd/user/
```

No agregues `httpHostHeader` al ingress. El agente compara `Origin` con `Host` para frenar
CSRF y necesita el hostname público tal cual.

## 3. Cloudflare Access: la política

**Crea la aplicación Access antes de levantar el túnel.** Si no, el hostname queda expuesto
sin login durante ese intervalo. El agente igual rechaza sin JWT, pero la PWA estática sí se
serviría.

Zero Trust → Access → Applications → Add → **Self-hosted**:

| Campo | Valor |
|---|---|
| Application name | `airwork-<pc>` |
| Domain | `<pc>.<dominio>`, sin path: cubre la PWA y `/api/` |
| Session duration | `24h` (sugerido; con menos, la PWA pide login más seguido) |
| Identity providers | One-time PIN por email, o el IdP que uses. Si el IdP ofrece passkeys, mejor. |
| Instant Auth | activado si hay un solo IdP |

Política única:

| Campo | Valor |
|---|---|
| Name | `solo-yo` |
| Action | **Allow** |
| Include | **Emails** → tu email (el mismo de `AIRWORK_ALLOWED_EMAILS`) |

No agregues reglas **Bypass** ni **Service Auth**, ni siquiera para `manifest.webmanifest`,
`sw.js` o los íconos. La PWA pide el manifest con credenciales
(`crossorigin="use-credentials"`), así que funciona detrás del login.

Después, en la aplicación ya creada:

- **Overview → Application Audience (AUD) Tag**: va en `AIRWORK_ACCESS_AUD`.
- **Settings → Custom Pages → Team domain** (`<team>.cloudflareaccess.com`): va en
  `AIRWORK_ACCESS_TEAM_DOMAIN`, sin `https://`.

Cada PC tiene su propia aplicación y su propio AUD: un JWT de un PC no sirve en otro.

## 4. Arranque

```sh
systemctl --user daemon-reload
systemctl --user enable --now airwork-agent.service
systemctl --user enable --now cloudflared-airwork.service
journalctl --user -u airwork-agent -f
```

### Linger (decisión tuya)

Con `Linger=no`, que es el estado actual de este PC, los servicios `--user` corren solo
mientras tengas una sesión abierta. Tras un reinicio no hay agente hasta que inicies sesión.
Para que corran sin sesión:

```sh
loginctl enable-linger "$USER"
```

- A favor: el PC queda accesible desde el celular después de un corte de luz o de un reinicio.
- En contra: un proceso capaz de ejecutar comandos como tu usuario queda activo sin que nadie
  haya iniciado sesión. Solo lo protegen Access y la validación del JWT.

## 5. Verificación

Local, sin pasar por Cloudflare:

```sh
curl -s -w ' %{http_code}\n' 127.0.0.1:8790/api/pc
# {"detail":"Falta Cf-Access-Jwt-Assertion"} 401
ss -ltnp | grep 8790          # debe decir 127.0.0.1:8790, nunca 0.0.0.0 ni *
```

Desde fuera (datos móviles, sin sesión de Access):

```sh
curl -sI https://<pc>.<dominio>/ | head -3
# HTTP 302 hacia <team>.cloudflareaccess.com: Access intercepta antes del túnel
```

En el celular: abre `https://<pc>.<dominio>`, entra con el PIN o el IdP y usa "Agregar a
pantalla de inicio". En `~/.local/state/airwork/audit.log` debe aparecer tu email en cada
acción.

## 6. Cortar el acceso

De menor a mayor alcance:

1. **Desde la PWA:** el botón "Cerrar todos los procesos" (`POST /api/kill`), en la pantalla del PC, cierra todas las sesiones de
   claude en curso. El agente sigue arriba.
2. **Desde el PC:** `systemctl --user stop cloudflared-airwork` corta el acceso remoto y deja el
   agente solo en loopback.
3. **Desde el panel de Cloudflare**, si perdiste el celular:
   - Zero Trust → My Team → Users → tu usuario → **Revoke session**;
   - o cambia la política a Block;
   - o borra la aplicación y el túnel.

   Esto funciona aunque no tengas acceso al PC.

## Qué no cubre este despliegue

- El endurecimiento de systemd es mínimo a propósito: `NoNewPrivileges`, sin `sudo` dentro de
  las sesiones. `ProtectSystem`, `PrivateTmp` y `UMask` cambiarían cómo claude trabaja en tus
  proyectos respecto de una terminal normal. La frontera de seguridad real es Access + JWT.
- Sin hub multi-PC: cada PC es independiente (Fase 3).

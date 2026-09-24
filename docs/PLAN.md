# airwork — Plan: trabajar con Claude Code desde el celular

Fecha: 2026-09-23 · Claude Code local: v2.1.280 · `cloudflared` instalado, `tailscale` no.

## 1. Objetivo

Desde el celular, fuera de casa:

- Enviar prompts a Claude Code corriendo en uno de varios PCs (el PC tiene que estar encendido).
- Elegir proyecto de `~/programacion` (signalsTrading, kryptolab, RayaditoEcomerce, etc.).
- Cambiar modelo y effort, compactar, limpiar o cambiar de sesión, y revisar conversaciones antiguas.
- Usar varias cuentas Claude (Pro/Max).
- Traspasos entre sesiones: un agente escribe `docs/handoff/<fecha>-<tema>.md` y el siguiente lo lee.
- Seguridad fuerte, sin puertos abiertos en casa.

## 2. Qué existe hoy (investigación)

| Opción | Qué resuelve | Qué no resuelve | Datos pasan por |
|---|---|---|---|
| **Remote Control oficial** (`claude remote-control`) | Chat desde la app oficial de Claude (Android) o claude.ai/code. Desde el móvil funcionan `/model <x>`, `/effort <x>`, `/compact`, `/clear`, `/context`, `/usage`, `/rename` y `/config k=v`. Tiene modo servidor con hasta 32 sesiones y `--spawn worktree`, push notifications y aprobación de permisos. Solo abre conexiones salientes HTTPS, sin puertos entrantes. | `/resume` es solo local, así que no puedes abrir conversaciones viejas desde el móvil. Cada servidor queda atado a un directorio y a una cuenta. No sirve con `setup-token` ni con API key: requiere `claude auth login` completo. En modo servidor, si se corta la red más de unos 10 min, el proceso termina. | Servidores de Anthropic (el transcript se guarda allí) |
| **Happy** (slopus/happy, fork *happier*) | App Android/iOS open source con cifrado de extremo a extremo (E2E). Maneja multi-máquina, abre sesiones en cualquier directorio e incluye voz. El relay es autohospedable (Docker + Postgres + Redis). | Es un wrapper de terceros sobre el CLI: depende de que siga siendo compatible con cada versión de Claude Code. | Tu relay (cifrado E2E) |
| **CloudCLI / claudecodeui** (siteboon) | Web UI con todos los proyectos y **todas las sesiones históricas** (lee `~/.claude/projects`), explorador de archivos, git y terminal. | Expone una shell por HTTP: es inaceptable sin una capa de acceso delante. Es otra superficie de ataque más que mantener. | Tu PC (si lo pones detrás de Cloudflare Access) |
| **Cloudflare Tunnel + Access** | Publica servicios del PC sin abrir puertos, con login por SSO/OTP/passkey, políticas por email/dispositivo/país y *service tokens*. Gratis hasta 50 usuarios. Con el cliente WARP ("Cloudflare One Client") en el celular también tienes red privada (VPN). | No es una UI: es solo el transporte y la autenticación. | Edge de Cloudflare (TLS termina allí) |
| **Tailscale / WireGuard** | VPN P2P, sencilla. | Otro proveedor más (Headscale si lo quieres autohospedar). Ya tienes Cloudflare, así que no aporta mucho. | P2P (coordinación en Tailscale) |

Hechos relevantes sobre cuentas:

- **Varias cuentas en un PC**: usa `CLAUDE_CONFIG_DIR` distinto por cuenta. En Linux las credenciales viven dentro de ese directorio, así que el aislamiento es completo.
- **Facturación de `claude -p` y del Agent SDK**: el cambio que los movía a un crédito separado (anunciado para el 15-jun-2026) se **pausó**. Hoy siguen consumiendo la suscripción, pero Anthropic avisó que lo revisará. No diseñes algo que dependa de `claude -p` masivo.
- **Términos de uso**: las suscripciones Pro/Max son de uso personal. Usar *tus* cuentas desde *tus* PCs está bien. Rotar cuentas de forma automática para esquivar los límites de uso sí puede chocar con los términos, así que revísalos antes de automatizar el cambio de cuenta.

## 3. Arquitectura recomendada

**No construyas un cliente de chat propio.** El chat, el cambio de modelo y effort, la compactación, los permisos y las notificaciones ya los da Remote Control oficial, con mejor calidad y soporte que cualquier APK casera. Lo que falta es un **plano de control** pequeño: arrancar y detener sesiones por proyecto y cuenta, ver el historial y los handoffs, y retomar sesiones viejas.

```
 Celular
  ├─ App Claude (oficial) ──HTTPS──► Anthropic ◄──saliente── claude remote-control (PC-n, por proyecto/cuenta)
  └─ PWA "airwork" ──HTTPS──► Cloudflare Access (passkey/OTP) ──Tunnel──► airwork-agent (PC-n, 127.0.0.1)
                                   hub.tudominio.cl  → lista de PCs
                                   pc1.tudominio.cl  → agente del PC1
                                   pc2.tudominio.cl  → agente del PC2
```

Componentes:

1. **Motor (en cada PC)**: `claude remote-control` corriendo como servicio `systemd --user` dentro de `tmux`, **una instancia por (proyecto, cuenta)**, con `--name "<pc>/<proyecto>/<cuenta>"`, `--spawn worktree` y `--permission-mode` conservador.
2. **airwork-agent (en cada PC)**: un servicio pequeño (Python/FastAPI o Go) que escucha solo en `127.0.0.1` y sale a internet por `cloudflared`. API:
   - `GET /projects`: carpetas de `~/programacion` que son repos git.
   - `GET /accounts`: perfiles `CLAUDE_CONFIG_DIR` disponibles, más el uso (`/usage`) si se puede leer.
   - `POST /sessions {project, account, mode}`: arranca `claude remote-control` (servicio nuevo o `tmux new-session`).
   - `GET /sessions` y `DELETE /sessions/:id`: lista y detiene sesiones (el kill switch).
   - `GET /history?project=`: lista y lee los `.jsonl` de `<CLAUDE_CONFIG_DIR>/projects/<proyecto>/`, en solo lectura.
   - `POST /resume {sessionId}`: `claude --resume <id> --remote-control "<nombre>"` en tmux. Esto cubre el hueco de `/resume` desde el móvil.
   - `GET/POST /handoff`: lista y lee `docs/handoff/*.md` de cada proyecto.
3. **hub (Cloudflare Worker + Access)**: una página con la lista de PCs y su estado (el heartbeat de cada agente se guarda en Workers KV). Todo va detrás de una sola política de Access.
4. **PWA**, no APK: se instala desde Chrome en Android, sin firmar ni publicar en la Play Store, y se actualiza sola. Un botón "Abrir en Claude" abre la URL de la sesión o la app oficial. Solo haz una APK (Capacitor/TWA) si después necesitas algo nativo.

Alternativa de menor esfuerzo: **Happy con el relay autohospedado** en lugar de las piezas 1 a 4. Te da una app única con multi-PC y cifrado E2E, pero dependes de un tercero que tiene que seguir el ritmo de Claude Code. Es razonable probarlo en la Fase 0 como comparación.

## 4. Seguridad

Modelo de amenaza: quien controla la sesión **ejecuta código en tu PC**. En signalsTrading y kryptolab eso significa llaves de exchange y dinero real.

1. **Cero puertos entrantes.** Remote Control y `cloudflared` solo abren conexiones salientes. No uses port-forward en el router.
2. **Cloudflare Access en todo hostname** del plano de control:
   - Política *Allow* con solo tu email.
   - Método de login: passkey o Google con 2FA. El OTP por email queda solo como respaldo.
   - Sesión corta (8 h) y, opcionalmente, restricción por país.
   - El agente **valida el JWT `Cf-Access-Jwt-Assertion`** (firma y `aud`). No basta con que Access esté delante.
3. **El agente escucha solo en 127.0.0.1.** No ofrece un endpoint de shell arbitraria, solo acciones con una lista blanca: proyectos dentro de `~/programacion` y cuentas registradas. Rechaza rutas con `..` y symlinks que salgan de ahí.
4. **Permisos de Claude Code** en las sesiones remotas:
   - Nada de `bypassPermissions`. Usa `default` o `acceptEdits`, y aprueba el resto desde el móvil.
   - `permissions.deny` para `Read(**/.env*)`, `Read(**/*secret*)`, `Read(~/.ssh/**)` y `Bash(curl:*)` hacia fuera cuando haga falta.
   - Evalúa `--sandbox` (aislamiento de filesystem y red).
5. **Secretos de trading**: llaves con permisos solo de lectura o paper trading en las máquinas donde corre el agente remoto. Nunca llaves con retiro habilitado.
6. **Auditoría**: el agente registra cada acción (quién, qué, cuándo) en un archivo con rotación. El transcript de Remote Control queda además en Anthropic.
7. **Kill switch**: `DELETE /sessions/*` más una regla de Access que se desactiva con un clic. También existe el setting `disableRemoteControl` en el PC.
8. **Actualizaciones**: fija la versión de Claude Code por PC y actualiza de forma deliberada. Remote Control cambia seguido: varias funciones exigen v2.1.2xx o superior.

## 5. Traspaso entre sesiones (handoff)

Una convención común para todos los repos:

- La skill global `/handoff` (en `~/.claude/skills/handoff/SKILL.md`) escribe `docs/handoff/AAAA-MM-DD-HHMM-<tema>.md` con: objetivo, estado, archivos tocados, decisiones, próximos pasos y el **prompt listo para pegar**. También hace commit en la rama actual.
- La skill `/retomar <ruta>` lee el archivo, verifica la rama y el estado de git, y continúa.
- Desde el móvil: "ejecuta /handoff", luego `/clear` o una sesión nueva, y después `/retomar docs/handoff/…`. La PWA lista los handoffs para copiar la ruta.

## 6. Plan por fases

### Fase 0: validar con lo oficial (1 tarde, sin código)

**Estado 2026-09-23.** Primer hallazgo: `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` (el proxy headroom) está fijado en `~/.claude/settings.json` y en `signalsTrading/.claude/settings.local.json`, y eso **bloquea Remote Control**. Solución aplicada:

- Se creó el perfil `~/.claude-accounts/remote/`: tiene el `settings.json` global sin la variable y enlaces simbólicos a `CLAUDE.md`, `RTK.md`, `skills`, `agents`, `plugins` y `projects`.
- Se creó el script `airwork/bin/rc-up`.
- Se empieza por kryptolab, que no tiene settings locales.
- Falta tmux.

- [x] Diagnóstico y perfil `remote`, más `bin/rc-up`.
- [x] `sudo pacman -S tmux`.
- [x] `CLAUDE_CONFIG_DIR=~/.claude-accounts/remote claude auth login` (cuenta cyb3rsignals, Pro).
- [x] Trust de kryptolab con el perfil nuevo, y después `bin/rc-up kryptolab` + `y`. 2026-09-24: **Connected**, `arch-trader/kryptolab/remote`, capacidad 1/32, spawn en worktree.
- [ ] Pruebas desde el móvil: prompt, `/model`, `/effort`, `/compact`, permisos y push.

- [ ] En cada PC: `claude auth login` (login completo, **no** `setup-token`) y `claude` una vez en cada proyecto para aceptar el workspace trust.
- [ ] `tmux new -s st 'cd ~/programacion/signalsTrading && claude remote-control --name pc1/signals --spawn worktree'`.
- [ ] Desde la app Claude en Android, en Code: enviar un prompt y probar `/model sonnet`, `/effort high`, `/compact` y aprobar un permiso.
- [ ] Activar las push notifications (`/config`).
- [ ] Probar un corte de red y la reconexión, y dejarlo 24 h para ver la estabilidad.
- [ ] (Opcional) Probar Happy para comparar.
- **Criterio de salida**: sabes con certeza qué te falta. Lo esperado es retomar conversaciones viejas, arrancar un proyecto nuevo sin tocar el PC y cambiar de cuenta.

**Herramientas creadas (2026-09-24)** en `airwork/bin`:

| Comando | Qué hace |
|---|---|
| `rc-all up\|down\|status\|check\|restart <p>` | Gestiona todos los proyectos de `config/projects.conf` (proyecto, perfil, modo). |
| `rc-up <p> [perfil] [modo]` | Levanta un servidor en tmux `rc-<p>-<perfil>`. |
| `rc-history <p> [n]` / `--show <id>` | Lista las conversaciones del proyecto (incluye worktrees) o muestra sus prompts. |
| `rc-resume <p> <id>` | Retoma una conversación antigua con `--remote-control`. Validado: aparece en la app. |
| `rc-profile <alias>` | Crea un perfil de cuenta nuevo (sin headroom, con lo compartido enlazado). |

Además, `systemd/airwork-rc.service` ejecuta `rc-all up` al arrancar.

Desde el celular, estos comandos se piden a Claude dentro de cualquier sesión remota ya conectada, por ejemplo "ejecuta rc-history signalsTrading". Eso sirve de puente hasta la Fase 2.

### Fase 1: servicios persistentes y multi-cuenta (1 día)
- [ ] Perfiles de cuenta: `~/.claude-accounts/<alias>/` con `CLAUDE_CONFIG_DIR`, y un login por perfil. Enlaza con symlinks `skills/`, `CLAUDE.md` y `settings.json` compartidos desde `~/.claude`.
- [ ] Plantilla `systemd --user`: `airwork-rc@<proyecto>--<cuenta>.service`, que lanza tmux con `claude remote-control`, `Restart=on-failure` y `loginctl enable-linger`.
- [ ] Script `airwork` (CLI) para `up`, `down`, `ls`, `resume <id>` y `accounts`.
- [ ] Skills `/handoff` y `/retomar`.
- [ ] Endurecer los permisos de la sección 4.4 en los settings compartidos.

### Fase 2: plano de control remoto (2–4 días)
- [ ] `airwork-agent` (FastAPI) con la API de la sección 3.2, validación del JWT de Access y log de auditoría.
- [ ] `cloudflared` como servicio: túnel `pc1` hacia `pc1.<dominio>` → `http://127.0.0.1:<puerto>`.
- [ ] Cloudflare Access: aplicación self-hosted con la política de solo tu email y passkey.
- [ ] Una PWA mínima: PCs → proyectos → [Nueva sesión | Historial | Handoffs] → abrir en Claude.
- [ ] Visor de historial: parsea los `.jsonl`, lista las sesiones por fecha y resumen, y ofrece el botón "Retomar".

### Fase 3: multi-PC y hub (1–2 días)
- [ ] Instalación del agente en cada PC como paquete o script de instalación, más el registro de su túnel.
- [ ] Worker `hub.<dominio>` con heartbeat en KV y estado online/offline de cada PC.
- [ ] (Opcional) Wake-on-LAN desde un dispositivo que siempre esté encendido (router o Raspberry Pi) detrás del mismo Access.

### Fase 4: pulido (opcional)
- [ ] Ver el uso por cuenta y sugerir otra cuenta cuando una esté cerca del límite (manual, sin rotación automática).
- [ ] Notificaciones propias (ntfy o Telegram) para eventos del agente.
- [ ] Empaquetar como APK (TWA) si la PWA se queda corta.

## 7. Riesgos y preguntas abiertas

- **Cambio de cuenta en la app oficial**: cada cuenta ve solo sus propias sesiones de Remote Control. Hay que verificar si la app Android permite tener varias cuentas a la vez. Si no, la solución es usar claude.ai/code en perfiles distintos del navegador.
- **`/resume` remoto**: el workaround de la Fase 2 (lanzar `claude --resume <id> --remote-control`) hay que validarlo en la práctica: el nombre de la sesión, la reconexión y que no queden dos procesos con la misma conversación.
- **Dependencia de Anthropic**: Remote Control es una función reciente y todavía cambia. Mantén Happy como plan B.
- **El PC tiene que estar encendido**: nada de esto sirve con el PC suspendido. Configura la suspensión, o Wake-on-LAN.
- **Workspace trust**: se acepta localmente una vez por proyecto. Los proyectos nuevos necesitan un primer `claude` en el PC, o el agente puede prepararlo.

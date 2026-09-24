# airwork-agent — Diseño (Fase 2)

Fecha: 2026-09-24 · Claude Code v2.1.281 · Python 3.14 · rama `fase2`

Servicio en cada PC más una PWA para el celular. Permite elegir PC → proyecto → conversación,
leer y retomar las sesiones locales, crear sesiones nuevas, elegir la cuenta a mano y aprobar
permisos a distancia. Convive con los scripts `rc-*`: no los toca ni los reemplaza.

## 0. Verificaciones previas (2026-09-24)

| Pregunta | Resultado |
|---|---|
| `claude -p --resume <id>` sigue la sesión y escribe en el mismo `.jsonl` | **Sí.** Mismo `session_id` y mismo archivo (pasó de 25 a 33 líneas). |
| Streaming con `--output-format stream-json --verbose` | **Sí.** Eventos `system/init`, `assistant`, `rate_limit_event`, `system/status`, `system/compact_boundary` y `result`. |
| `-p --resume` sobre una sesión creada en modo **interactivo** | **Sin verificar.** Mis 75 sesiones mezclan `entrypoint: cli` y `sdk-cli` con el mismo formato, así que se espera que funcione. Se prueba en la etapa 2 con una sesión desechable. |
| Modelo | `--model`, o `set_model()` en el SDK. |
| Effort | Existe el flag `--effort low\|medium\|high\|xhigh\|max`. Con haiku, `per_turn_effort_active` salió `False`; falta confirmarlo con sonnet u opus. |
| Compactar | `/compact` como prompt en `-p` **funciona**: `compact_boundary` con `pre_tokens: 24569` y `post_tokens: 2780`. |
| Uso de contexto | `/context` en `-p` devuelve una tabla markdown (`Tokens: 24.4k / 200k (12%)`) sin gastar tokens del modelo. La cuota viene en `rate_limit_event.unifiedWindows` (`five_hour`, `seven_day`). |
| Permisos remotos | Se elige `can_use_tool` del Agent SDK en Python (ver §4). |
| Detectar un `claude` activo con el mismo id | Pendiente. El CLI ya detecta este caso con `--bg --resume` ("starts a copy and says so"); ver §4.3. |

## 1. Decisiones y desviaciones

1. **Una cuenta es un perfil `CLAUDE_CONFIG_DIR`** (`~/.claude-accounts/<alias>`, creado con
   `rc-profile` y con login oficial). El token `setup-token` guardado por el agente queda como
   **modo opcional y desactivado** (`AIRWORK_ALLOW_TOKENS=0`).
   - **Por qué:** los términos vigentes dicen que quien desarrolla con el Agent SDK no debe
     "collect, store, or intermediate Claude.ai credentials or session tokens". Es ambiguo para
     uso personal, pero el perfil no guarda ningún secreto propio y cumple igual la selección
     manual de cuenta.
   - **Si activas el modo token:** va cifrado con Fernet, la clave está en
     `~/.config/airwork/key` (600) y se pasa en `CLAUDE_CODE_OAUTH_TOKEN`. El perfil igual hace
     falta por los settings y el trust.
2. **Historial compartido:** todos los perfiles enlazan `projects/` → `~/.claude/projects`,
   así que una misma conversación se puede seguir con otra cuenta.
3. **Headroom apagado por defecto.** El agente siempre quita `ANTHROPIC_BASE_URL` y
   `ANTHROPIC_API_KEY` del entorno del hijo. Solo los vuelve a poner (`http://127.0.0.1:8787`)
   si la cuenta tiene `use_headroom = true`. Hay una trampa: `~/.claude/settings.json` fija esa
   variable en `env`, y settings le gana al entorno. Por eso **nunca se usa `~/.claude` como
   perfil**: se usan perfiles creados por `rc-profile`, que ya la quitan.
4. **Motor:** `claude-agent-sdk` (Python) con `cli_path` apuntando al `claude` del sistema, para
   no usar el binario que trae el paquete. No hay llamadas directas a la API de mensajes.
5. **Streaming por SSE**, no WebSocket. SSE funciona sin configuración extra a través de
   Cloudflare, reconecta solo con `Last-Event-ID` y no complica la PWA. Las órdenes van por
   `POST`.
6. **Puerto `127.0.0.1:8790`**, porque 8787 lo usa headroom.
7. **Multi-PC:** cada PC tiene su agente y su hostname (`<pc>.<dominio>`). La PWA guarda la
   lista de PCs. El hub con heartbeat queda para la Fase 3.

## 2. Modelo de datos

SQLite en `~/.local/share/airwork/agent.db`, con permisos 600. El estado de cada conversación no
se duplica: la fuente de verdad son los `.jsonl`.

```
accounts        id, alias UNIQUE, config_dir, use_headroom BOOL, token_enc BLOB NULL,
                label, created_at, last_error, last_error_at
prefs           scope ('project'|'session'), key (nombre del proyecto | session_id),
                account_id, model, effort, permission_mode, updated_at
                -- la preferencia de la sesión gana a la del proyecto
pcs             id, name, base_url              -- solo para la PWA; hoy: arch-trader
audit (archivo) ~/.local/state/airwork/audit.log, JSON por línea, con rotación:
                ts, email (del JWT), ip, acción, proyecto, sesión, cuenta, resultado
```

Proyectos y sesiones se calculan al vuelo:

- **Proyecto:** un directorio de primer nivel en `~/programacion` (con `realpath` dentro de esa
  raíz) que sea un repo git o tenga historial.
- **Sesiones del proyecto:** `~/.claude/projects/<enc>/*.jsonl` y
  `<enc>--claude-worktrees-*/*.jsonl`, donde `enc` reemplaza con `-` todo lo que no sea
  alfanumérico. Si hay líneas `relocated` o `continued-in`, se sigue el enlace.
- **Resumen de una sesión** (en caché por `mtime` y tamaño): título, primer y último prompt,
  número de prompts, rama, `cwd` y si es un worktree.
  - Título: `custom-title`, luego `ai-title`, luego `last-prompt`.
  - Rama y `cwd`: la última línea `user` que los tenga.
- **Lectura de una sesión:** paginada. Se transforma en una lista de eventos:
  - `prompt`;
  - `text`;
  - `thinking` (plegado);
  - `tool_use` (nombre y entrada resumida) y `tool_result` (recortado a 4 KB, con opción de ver
    más);
  - `compact` (en `compact_boundary`);
  - `meta` (cambios de modo, de permisos o de modelo).

  Se omiten `isMeta`, `file-history-*` y `attachment`. Las líneas con `isSidechain` son de
  subagentes y se muestran solo si se piden.

## 3. Endpoints

Todos van bajo `/api` y exigen un JWT de Access válido (§5). Las respuestas son JSON y nunca
incluyen tokens.

| Método y ruta | Qué hace |
|---|---|
| `GET /api/pc` | Hostname, versión de Claude Code y del agente, cuentas activas, headroom arriba o no. |
| `GET /api/projects` | Proyectos con la fecha de su última sesión. |
| `GET /api/projects/{p}/sessions?limit=&before=` | Resúmenes de sesiones. `active` indica si está abierta en el agente o fuera de él. |
| `GET /api/sessions/{id}?cursor=&sidechain=0` | Eventos del historial, paginados. |
| `GET /api/sessions/{id}/context` | Lanza `/context` (sin costo de modelo) y devuelve la tabla. |
| `POST /api/runs` | Arranca o reutiliza un proceso. Cuerpo: `{project, session_id?, fork?, prompt, account?, model?, effort?, permission_mode?}`. Devuelve `{run_id, session_id}`. |
| `POST /api/runs/{rid}/messages` | Envía otro prompt al mismo proceso; también sirve para `/compact`. |
| `POST /api/runs/{rid}/interrupt` | Cancela el turno en curso. |
| `DELETE /api/runs/{rid}` | Cierra el proceso (kill switch por sesión). |
| `PATCH /api/runs/{rid}` | Cambia `model` o `permission_mode` en caliente. El effort aplica al siguiente arranque. |
| `GET /api/runs/{rid}/events` | Stream SSE: `init`, `text`, `thinking`, `tool_use`, `tool_result`, `permission_request`, `permission_resolved`, `status`, `compact`, `rate_limit`, `result`, `error`, `closed`. |
| `POST /api/permissions/{req_id}` | `{decision: allow\|deny, message?, scope: once\|session}` |
| `GET /api/accounts` / `POST` / `PATCH /{id}` / `DELETE /{id}` | CRUD de cuentas. `POST` valida que `config_dir` esté en `~/.claude-accounts` y tenga `settings.json` sin `ANTHROPIC_BASE_URL`. |
| `GET/PUT /api/prefs/{scope}/{key}` | Cuenta, modelo, effort y modo por proyecto o por sesión. |
| `GET /api/projects/{p}/handoffs` | Lista `docs/handoff/*.md`. Con `?name=` devuelve el contenido. |
| `POST /api/kill` | Cierra todos los procesos del agente. |

`permission_mode` acepta `default`, `acceptEdits` y `plan`. El agente rechaza `bypassPermissions`
y `auto`.

## 4. Flujo de streaming y permisos

```
PWA ──POST /api/runs──► RunManager ──ClaudeSDKClient(cwd, env, options)──► claude (CLI)
 ▲                          │   ▲                                            │
 └──SSE /events◄── buffer ◄─┘   └── can_use_tool(tool, input) ◄──────────────┘
                                     │ crea PermissionRequest(req_id, Future)
                                     │ emite SSE permission_request
 POST /api/permissions/{req_id} ─────┘ resuelve el Future → Allow/Deny
```

### 4.1 Procesos

- Hay un `ClaudeSDKClient` por sesión activa. Se cierra después de 15 minutos inactivo o con
  `DELETE`.
- Cada run tiene un buffer circular de eventos (unos 2.000, con id incremental). Una PWA que
  reconecta con `Last-Event-ID` recibe lo que se perdió.
- Arrancar un proceso:
  1. Toma la cuenta de la petición, o de la preferencia de la sesión, o de la del proyecto. Si
     no hay ninguna, error 400. **Nunca elige una sola.**
  2. Arma el entorno: `CLAUDE_CONFIG_DIR`. Si la cuenta tiene token, `CLAUDE_CODE_OAUTH_TOKEN`.
     Si tiene headroom, `ANTHROPIC_BASE_URL`. Quita `ANTHROPIC_API_KEY`.
  3. Fija `cwd`: el `cwd` de la sesión, leído del `.jsonl`, o la raíz del proyecto si es nueva.
     Debe existir y estar dentro de `~/programacion` (incluidos sus worktrees).
  4. Pasa las opciones: `resume=id`, `fork_session`, `model`, `effort` (con `extra_args` si el
     SDK no lo expone), `permission_mode` y `can_use_tool`.

### 4.2 Permisos

- `can_use_tool` crea una petición, la emite por SSE y espera. Si nadie responde en 10 minutos,
  **deniega**.
- La respuesta `scope: session` agrega la regla solo a ese run, en memoria; no se escribe en los
  settings.
- Para evitar que el agente se autoapruebe, ningún endpoint aprueba en bloque, y las respuestas
  quedan en el log de auditoría con el email del JWT.
- **Por qué `can_use_tool` y no `--permission-prompt-tool`:** el callback vive en el mismo
  proceso async que sirve el SSE, así que no hace falta un servidor MCP ni un canal propio. El
  mismo cliente además ofrece `interrupt()` y `set_model()` sin relanzar el proceso.
- **Costo:** se depende del paquete `claude-agent-sdk`. Queda fijada la versión en
  `pyproject.toml`.

### 4.3 Conversación abierta en dos lugares

- **Dentro del agente:** un lock por `session_id` hace imposible abrirla dos veces.
- **Fuera del agente** (terminal, `rc-resume` o Remote Control): se busca un proceso `claude`
  cuyo `argv` contenga el id, y además se revisa si el `.jsonl` cambió en los últimos 90
  segundos. Si alguna de las dos señales es positiva, se responde **409** y se ofrece
  `fork: true`, que usa `--fork-session` y crea un id nuevo desde esa historia. Es una
  heurística: una sesión interactiva abierta con `/resume` desde el selector no lleva el id en
  `argv`, y por eso existe la segunda señal.

### 4.4 Cuota y errores

- `rate_limit_event` se reenvía a la PWA, que muestra el porcentaje de 5 h y de 7 días.
- Si un `result` trae un error de cuota o de autenticación, se guarda `last_error` en la cuenta,
  el error se muestra tal cual y **no se reintenta**. Tú eliges otra cuenta y reenvías; la
  historia es local, así que la conversación sigue.

## 5. Seguridad: amenazas y mitigaciones

Quien controla el agente ejecuta código en el PC, y ahí hay llaves de exchange.

| Amenaza | Mitigación |
|---|---|
| Acceso directo al puerto | Bind solo en `127.0.0.1`. Sin port-forward. `cloudflared` solo abre conexiones salientes. |
| Access mal configurado o saltado (por ejemplo, otro servicio local que proxee al puerto) | El backend valida `Cf-Access-Jwt-Assertion` con las claves de `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (en caché, rotación incluida). Comprueba firma RS256, `aud` = AUD de la aplicación, `iss` = dominio del team, `exp` y que el `email` esté en `AIRWORK_ALLOWED_EMAILS`. Sin JWT válido, 401. |
| Modo desarrollo olvidado en producción | `AIRWORK_DEV=1` solo omite la validación si la petición **no** trae `Cf-Ray`, es decir, si no pasó por Cloudflare. La unidad de systemd no lo define. |
| CSRF desde otra web con la cookie de Access | Solo JSON (`Content-Type: application/json`). Se verifica que `Origin` sea el propio host. Sin CORS. |
| Path traversal para leer o ejecutar fuera de `~/programacion` | Los nombres de proyecto pasan por una lista blanca (lista del directorio). Se comprueba `realpath` dentro de la raíz. `session_id` debe ser un UUID válido. `config_dir` debe estar dentro de `~/.claude-accounts`. |
| Fuga de tokens | Van cifrados en reposo, la clave es un archivo 600 fuera del repo, la base de datos es 600, y `.gitignore` cubre `*.db`, `*.key` y `.env*`. Hay un filtro de logging que enmascara `sk-ant-[A-Za-z0-9_-]+`. La API nunca devuelve el token, solo `has_token: true`. El entorno del hijo no se registra. |
| El agente aprueba permisos solo | Ningún modo sin permisos. La denegación es el default y el timeout también deniega. Cada respuesta queda auditada. |
| Lectura de secretos en el historial | La PWA muestra los `tool_result` tal cual (pueden traer `.env` que Claude leyó). Mitigación parcial: el `permissions.deny` de PLAN §4.4 en los perfiles. Queda como **riesgo aceptado**, detrás de Access. |
| Robo del celular | Sesión corta de Access (8 h) con passkey, y un kill switch (`POST /api/kill` y desactivar la aplicación en Access). |
| Abuso de cuota o bucles | Un run activo por sesión, como máximo 4 runs simultáneos, y timeout de inactividad. |
| Dependencia de terceros | Se fijan `claude-agent-sdk` y la versión de Claude Code. Un test de humo al arrancar verifica que `claude --version` coincide con la esperada y, si no, avisa. |

## 6. PWA

Es HTML, JS y CSS sin build, servidos por el propio agente en `/`. Tiene manifest y service
worker que solo cachea el shell, nunca la API.

Pantallas: PCs → proyectos → sesiones (buscador y "nueva") → chat.

En el chat:
- cabecera con la cuenta, el modelo, el effort, el modo de permisos y el uso de contexto;
- botones para compactar e interrumpir;
- tarjetas de permiso con **Permitir**, **Denegar** y **Permitir en esta sesión**.

## 7. Etapas

1. Este documento.
2. `agent/`: FastAPI + SDK con tests (`pytest`). La lectura de `.jsonl` se prueba con fixtures
   sintéticas y el `RunManager` con un cliente falso. Hay un test de integración opcional
   (`-m live`) que usa haiku en un directorio temporal.
3. PWA.
4. `systemd --user`, `cloudflared` (ejemplo) y `docs/cloudflare-access.md`.
5. Skills `/handoff` y `/retomar`, que se instalan en `~/.claude/skills`. **Se toca fuera de
   `airwork`, así que pediré confirmación.**

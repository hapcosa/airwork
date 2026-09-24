# Prompt: sistema propio airwork (Fase 2)

> Pega esto en una sesión nueva de Claude Code abierta en `~/programacion/airwork`
> o dile: "lee `docs/prompts/fase2-sistema-propio.md` y ejecútalo".

---

## Contexto

Trabajo en varios proyectos de `~/programacion` (kryptolab, signalsTrading, RayaditoEcomerce,
Chilo-FloraApiDevops, bitacora-barcazas) y quiero manejarlos desde el celular cuando no estoy
en casa. El PC queda encendido.

Hoy uso **Claude Code Remote Control** con los scripts de `airwork/bin`:
`rc-all`, `rc-up`, `rc-history`, `rc-resume` y `rc-profile`. Tiene dos límites:
- una cuenta fija por proyecto, porque exige el login completo de claude.ai;
- la app oficial no muestra las conversaciones antiguas del PC.

Lee primero `docs/PLAN.md` completo: ahí están la arquitectura, la seguridad y las
lecciones de la Fase 0.

## Objetivo

Construir **airwork-agent**: un servicio en el PC con una PWA para el celular que me permita:

1. **Elegir PC → proyecto → conversación.** Hoy hay un solo PC (`arch-trader`), pero el diseño
   debe admitir varios.
2. **Ver y retomar las conversaciones que ya tengo en este PC.** Es el requisito principal.
   Son las sesiones de `~/.claude/projects/<ruta-codificada>/*.jsonl`, incluidos los worktrees
   `<base>--claude-worktrees-*`. Debo poder abrir una, leer el historial completo (prompts,
   respuestas y herramientas usadas) y seguir escribiendo en ella desde el celular.
3. **Crear conversaciones nuevas** en cualquier proyecto.
4. **Elegir la cuenta Claude a mano** en cada conversación o proyecto. Una cuenta es un token
   de `claude setup-token` (`sk-ant-oat01-…`) pasado en `CLAUDE_CODE_OAUTH_TOKEN`.
   **Sin rotación automática.** Si una cuenta agota su cuota, se muestra el error y yo elijo
   otra. El historial es local, así que la misma conversación sigue con otra cuenta.
5. **Cambiar modelo y effort, compactar** y ver el uso de contexto.
6. **Aprobar permisos desde el celular**, es decir, que no todo corra con permisos abiertos.
7. **Handoff:** los skills `/handoff` y `/retomar` que describe el plan. Un prompt para la
   próxima sesión se guarda en `docs/` del proyecto y la sesión nueva lo lee.

## Restricciones

- **Motor:** Claude Code oficial, ya sea `claude -p` o el Agent SDK, que usa el CLI por debajo,
  autenticado con `CLAUDE_CODE_OAUTH_TOKEN`. Es lo mismo que ya hace
  `signalsTrading/AI_trader/ai_clients/claude_code_client.py`. **Nunca** llames a la API de
  mensajes directamente con el token OAuth.
- **Tokens:** van cifrados en reposo (por ejemplo, SQLite más una clave en el keyring o en un
  archivo con permisos 600). Nunca aparecen en logs, en respuestas HTTP ni en git.
- **Red:**
  - el servicio escucha solo en `127.0.0.1`;
  - se expone con Cloudflare Tunnel y Cloudflare Access (uso mis dominios de Cloudflare);
  - el backend además valida el JWT de Access (`Cf-Access-Jwt-Assertion`);
  - no se abren puertos en el router.
- **Retomar una sesión** se hace en su `cwd` original, que se lee del `.jsonl`. No se abre la
  misma sesión desde dos procesos a la vez: detecta si hay un `claude` activo con ese id y
  ofrece `--fork-session`.
- **Headroom:** el proxy `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` es opcional. Por defecto el
  proceso hijo se lanza sin esa variable. Deja un interruptor por cuenta.
- **Idioma:** español neutro en la UI, los comentarios y los commits. Sin voseo.
- **Git:** `airwork` todavía no es un repo git. Inicialízalo y trabaja en una rama.
- **Coexistencia:** no rompas los scripts `rc-*`; Remote Control sigue funcionando en paralelo.

## Antes de escribir código, verifica y reporta (con output)

1. Continuación de sesiones con `claude -p`:
   - ¿`claude -p --resume <id>` continúa una sesión creada en modo interactivo?
   - ¿Se escribe en el mismo `.jsonl`?
   - ¿Funciona con `--output-format stream-json --verbose` para hacer streaming?
2. Qué se puede controlar sin modo interactivo:
   - modelo (`--model`);
   - effort: ¿hay flag o solo settings?;
   - compactar: ¿`/compact` funciona como prompt en `-p`?;
   - uso de contexto.
3. Permisos remotos: compara `--permission-prompt-tool` (una herramienta MCP propia que
   reenvía la aprobación al celular) contra el callback `can_use_tool` del Agent SDK en Python.
   Elige uno y explica por qué.
4. ¿El Agent SDK acepta `CLAUDE_CODE_OAUTH_TOKEN`, o solo `claude -p` lo acepta? Revisa la
   documentación vigente y los términos de uso para uso personal con suscripción. Si hay
   ambigüedad, dímelo antes de seguir.
5. El formato real de los `.jsonl`: tipos de entrada, títulos (`custom-title`, `ai-title`),
   mensajes del asistente y bloques de herramientas. Usa `bin/rc-history` como punto de partida.

## Entregables por etapas (confírmame cada etapa antes de pasar a la siguiente)

1. **Diseño corto** en `docs/fase2-diseno.md`:
   - endpoints;
   - modelo de datos (pcs, proyectos, cuentas, sesiones);
   - flujo de streaming y permisos;
   - amenazas y mitigaciones.
2. **Backend FastAPI** (`agent/`):
   - listar proyectos y sesiones;
   - leer una sesión;
   - enviar un prompt (nuevo o `--resume`) con streaming por SSE o WebSocket;
   - cancelar;
   - CRUD de cuentas con tokens cifrados;
   - selección manual de cuenta.
   - Con tests.
3. **PWA mínima**, usable en el celular:
   - lista de proyectos → sesiones → chat;
   - selector de cuenta, modelo y effort;
   - botones de compactar y de nueva sesión;
   - aprobación de permisos.
4. **Despliegue:**
   - unidad `systemd --user`;
   - `cloudflared` con una configuración de ejemplo;
   - la política de Access documentada en `docs/`.
5. **Skills** `/handoff` y `/retomar`.

## Cómo quiero que trabajes

- Dime qué falló, con el output; no lo escondas.
- Lista explícitamente cualquier default o desviación que elijas.
- No pidas confirmación para lo trivial. Sí para decisiones de seguridad y para cualquier cosa
  que toque mis proyectos fuera de `airwork`.

---
name: handoff
description: Traspaso entre sesiones — escribe docs/handoff/AAAA-MM-DD-HHMM-<tema>.md con objetivo, estado, archivos, decisiones, pendientes y un prompt listo para la próxima sesión, y lo commitea solo en la rama actual. Usar solo cuando el usuario lo pida con /handoff.
argument-hint: "[tema]"
disable-model-invocation: true
allowed-tools: Bash(git rev-parse:*), Bash(git branch --show-current), Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(date:*)
---

# /handoff — dejar la sesión lista para otra

Escribes un traspaso para que **otra sesión, sin este contexto**, pueda continuar el trabajo.
La skill `/retomar` lo lee. La PWA de airwork los lista desde `docs/handoff/`.

Tema pedido: `$ARGUMENTS` (si viene vacío, elige uno corto a partir de la tarea).

## 1. Dónde estás

```bash
git rev-parse --show-toplevel      # raíz del repo o del worktree actual
git branch --show-current
git status --porcelain
git log --oneline -5
date +%Y-%m-%d-%H%M
```

- El archivo va en `<raíz>/docs/handoff/`, dentro del worktree donde trabajas, no en el checkout
  principal.
- Si no es un repo git, usa `docs/handoff/` bajo el directorio actual y omite el commit.

## 2. Nombre

`docs/handoff/<AAAA-MM-DD-HHMM>-<tema>.md`:

- `<tema>` en minúsculas, con palabras separadas por `-`, de 40 caracteres como máximo.
- Solo se aceptan `a-z0-9-`: nada de espacios, tildes ni `/`. La PWA rechaza otros nombres.
- Si el nombre ya existe, agrega `-2`.

## 3. Contenido

Escribe en español neutro, con hechos verificables y sin relleno. Usa esta plantilla:

```markdown
# Handoff: <tema>

- Fecha: <AAAA-MM-DD HH:MM>
- Repo: <raíz>  ·  Rama: <rama>  ·  Último commit: <hash corto> <asunto>
- Sesión anterior: ${CLAUDE_SESSION_ID}

## Objetivo
Qué se pidió, con las palabras del usuario cuando importen. Incluye las restricciones que
puso.

## Estado
Qué quedó hecho y cómo se verificó: tests, output, comandos. Qué quedó a medias.

## Archivos tocados
- `ruta` — qué cambió y por qué (incluye los que no están commiteados).

## Decisiones
Qué se decidió y por qué, sobre todo lo que el usuario aprobó o rechazó. Así no se vuelve a
discutir.

## Sin verificar / riesgos
Qué no se probó, qué falló y con qué output, qué supuestos quedan abiertos.

## Próximos pasos
1. Pasos concretos y ordenados. Marca los que requieren confirmación del usuario.

## Prompt para la próxima sesión
~~~
Texto listo para pegar: contexto mínimo, la tarea siguiente, las restricciones vigentes y
qué confirmar antes de actuar.
~~~
```

Reglas:

- **Nunca incluyas secretos**: tokens, llaves, contraseñas, contenido de `.env`, credenciales ni
  JWT. Si hacen falta, indica dónde están, no su valor.
- Nombra los archivos por su ruta relativa al repo.
- Si `git status` muestra cambios sin commitear que no son tuyos, anótalos en "Estado" y no los
  toques.

## 4. Commit (solo el archivo del handoff)

- Si la rama es `main`, `master` o la rama por defecto del repo: **no hagas commit**. Deja el
  archivo sin commitear y avísalo.
- En cualquier otra rama, commitea **solo ese archivo**, aunque haya otras cosas en el stage:

```bash
git add docs/handoff/<archivo>.md
git commit -m "handoff: <tema>" -- docs/handoff/<archivo>.md
```

  Termina el mensaje con la línea de co-autoría que use la sesión.
- No hagas push.

## 5. Respuesta

Tres líneas como máximo:

- la ruta del archivo;
- si se commiteó, en qué rama y con qué hash (o por qué no);
- cómo retomar: `/retomar docs/handoff/<archivo>.md`.

---
name: retomar
description: Retoma el trabajo desde un handoff (docs/handoff/*.md escrito por /handoff) — lo lee, verifica rama, worktree y estado de git contra lo que dice, reporta diferencias y sigue con los próximos pasos. Usar solo cuando el usuario lo pida con /retomar.
argument-hint: "[docs/handoff/archivo.md]"
disable-model-invocation: true
allowed-tools: Bash(git rev-parse:*), Bash(git branch --show-current), Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git worktree list:*), Bash(ls:*)
---

# /retomar — continuar desde un handoff

Archivo pedido: `$ARGUMENTS`

## 1. Encontrar el handoff

- Si se indicó una ruta, léela. Si es relativa, se resuelve desde la raíz del repo
  (`git rev-parse --show-toplevel`).
- Si no se indicó ninguna, usa el más reciente de `docs/handoff/` (el nombre empieza con la
  fecha, así que basta con el orden alfabético) y dilo.
- Si no existe ninguno, dilo y detente. No inventes el contexto.

Lee el archivo completo.

## 2. Verificar antes de tocar nada

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --porcelain
git log --oneline <commit-del-handoff>..HEAD    # qué pasó desde entonces
git worktree list                               # si la rama vive en otro worktree
```

Compara con lo que dice el handoff:

- **La rama o el repo o worktree son otros:** dilo y pregunta. No hagas `checkout` ni cambies
  de worktree por tu cuenta, porque hay trabajo local que se puede perder. Si la rama está en
  otro worktree, indica la ruta.
- **Hay commits nuevos desde el handoff:** resúmelos en una línea cada uno. Algún paso
  pendiente puede estar ya hecho.
- **Hay cambios sin commitear que el handoff no menciona:** dilo y no los toques.
- **Los archivos de "Archivos tocados" no existen o cambiaron de forma incompatible:** dilo.

## 3. Reportar y seguir

Responde primero con un resumen de 5 líneas como máximo:

- el objetivo;
- dónde quedó el trabajo;
- las diferencias encontradas en el paso 2 (o "sin diferencias");
- el próximo paso que vas a hacer.

Después:

- Respeta las **decisiones** y **restricciones** del handoff como si el usuario te las hubiera
  dicho ahora. No reabras lo ya decidido salvo que haya evidencia nueva, y en ese caso muéstrala.
- Sigue los **próximos pasos** en orden. Los marcados como "requiere confirmación" se
  preguntan antes de hacerlos.
- Lo que el handoff lista como "sin verificar" se verifica antes de construir encima.
- Si el handoff trae un "Prompt para la próxima sesión", tómalo como la instrucción de trabajo.

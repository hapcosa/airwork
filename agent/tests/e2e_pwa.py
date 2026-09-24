"""E2E de la PWA: backend real con FakeClient + Chromium headless, viewport de teléfono.

No lo recoge pytest. Ejecutar desde agent/:
    uv run --with playwright python tests/e2e_pwa.py [directorio_para_capturas]
Usa datos sintéticos en un directorio temporal; no toca ~/.claude.
"""
import asyncio, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import conftest, uvicorn
from airwork_agent.app import create_app
from playwright.async_api import async_playwright, expect

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="airwork-e2e-"))
s = conftest.make_env(Path(tempfile.mkdtemp()))
s.permission_timeout_s = 60
app = create_app(s, client_factory=conftest.FakeClient)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8799, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)
BASE = "http://127.0.0.1:8799"
# Si la versión de playwright no coincide con el navegador instalado, usa el headless shell que haya en caché.
_shells = sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
CHROME = str(_shells[-1]) if _shells else None

async def main():
    problems = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=CHROME)
        for scheme in ("light", "dark"):
            ctx = await b.new_context(viewport={"width": 390, "height": 800}, color_scheme=scheme, device_scale_factor=2)
            page = await ctx.new_page()
            page.on("console", lambda m: m.type in ("error", "warning") and problems.append(f"console {m.type}: {m.text}"))
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            await page.goto(BASE + "/#/")
            await expect(page.get_by_text("procesos activos")).to_be_visible()
            if scheme == "light":
                # cuentas: registrar cuenta1
                await page.goto(BASE + "/#/accounts")
                await page.get_by_role("button", name="Agregar cuenta").click()
                await expect(page.locator(".card-title", has_text="cuenta1")).to_be_visible()
                await page.screenshot(path=OUT / "accounts.png", full_page=True)
            await page.goto(BASE + "/#/")
            await page.screenshot(path=OUT / f"pcs-{scheme}.png")
            await page.get_by_text("procesos activos").click()
            await page.get_by_text("demo").click()
            await expect(page.get_by_text("Título propio")).to_be_visible()
            await page.screenshot(path=OUT / f"sessions-{scheme}.png")
            if scheme == "dark":
                break
            # retomar sesión existente (worktree)
            await page.get_by_text("Título propio").click()
            await expect(page.get_by_text("hola, revisa el README")).to_be_visible()
            await page.locator(".controls summary").click()
            await page.locator("select[aria-label=Cuenta]").select_option(label="cuenta1", timeout=3000)
            await page.locator("textarea").fill("sigue por favor")
            await page.get_by_role("button", name="Enviar").click()
            await expect(page.get_by_text("eco:sigue por favor")).to_be_visible()
            # permiso
            await page.locator("textarea").fill("PERMISO para ls")
            await page.get_by_role("button", name="Enviar").click()
            await expect(page.locator(".perm")).to_be_visible()
            await page.screenshot(path=OUT / "perm.png")
            await page.get_by_role("button", name="Permitir", exact=True).click()
            await expect(page.get_by_text("permiso:allow")).to_be_visible()
            await expect(page.locator(".perm.done")).to_be_visible()
            # recargar: el run sigue activo; no se duplican mensajes y aparece de nuevo el stream
            await page.reload()
            await expect(page.get_by_text("proceso activo")).to_be_visible()
            n = await page.get_by_text("eco:sigue por favor").count()
            await page.locator("textarea").fill("tras recargar")
            await page.get_by_role("button", name="Enviar").click()
            await expect(page.get_by_text("eco:tras recargar")).to_be_visible()
            print("eco duplicados tras recargar:", n)
            await page.locator(".controls summary").click()
            await page.get_by_role("button", name="contexto").click()
            await expect(page.get_by_text("Contexto: 1k de 200k")).to_be_visible()
            await page.screenshot(path=OUT / "chat.png")
            await page.get_by_role("button", name="Cerrar proceso").click()
            await expect(page.get_by_text("proceso cerrado")).to_be_visible()
            # nueva conversación
            await page.goto(BASE + "/#/s/demo/new")
            await page.locator("textarea").fill("hola nueva")
            await page.get_by_role("button", name="Enviar").click()
            await expect(page.get_by_text("Elige una cuenta")).to_be_visible()
            await page.locator("select[aria-label=Cuenta]").select_option(label="cuenta1")
            await page.get_by_role("button", name="Enviar").click()
            await expect(page.get_by_text("eco:hola nueva")).to_be_visible()
            assert "/#/s/demo/" in page.url and "new" not in page.url, page.url
            print("url tras nueva:", page.url)
            await page.screenshot(path=OUT / "new.png")
            # handoff
            await page.goto(BASE + "/#/p/demo")
            await page.get_by_text("Handoffs (1)").click()
            await page.get_by_text("2026-09-24-1200-demo.md").click()
            await expect(page.get_by_text("prompt listo")).to_be_visible()
            await page.get_by_role("button", name="Retomar en una conversación nueva").click()
            await expect(page.locator("textarea")).to_have_value("/retomar docs/handoff/2026-09-24-1200-demo.md")
            await ctx.close()
        await b.close()
    print("capturas en", OUT)
    print("PROBLEMAS:", problems or "ninguno")
    assert not problems
    server.should_exit = True

asyncio.run(main())

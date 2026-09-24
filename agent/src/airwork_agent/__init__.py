"""airwork-agent: controla Claude Code de este PC desde el celular."""

__version__ = "0.1.0"


def main() -> None:
    import uvicorn

    from .app import create_app
    from .config import Settings

    s = Settings.from_env()
    s.validate_for_serving()
    uvicorn.run(create_app(s), host=s.host, port=s.port, proxy_headers=False, access_log=False)

"""Optional-integration plugin registry — load server-specific add-ons by name.

Server-specific integrations (an inter-agent mail bus, external gateways, …)
live as self-contained ``ccbot.<name>`` subpackages and are activated by
listing their names in ``CCBOT_PLUGINS`` (comma-separated), e.g.
``CCBOT_PLUGINS=name1,name2``. A plain deployment leaves it empty and ships
none of them — the core carries no reference to any specific plugin, so a
build with the plugin packages removed is fully self-consistent.

A plugin is any ``ccbot.<name>`` module that optionally exposes:
  - ``STRINGS: dict``               i18n catalog entries (merged at startup)
  - ``bot_commands() -> list``      BotCommand menu contributions
  - ``register_handlers(app)``      add PTB handlers
  - ``async on_startup(app)``       start servers / background tasks
  - ``async on_shutdown()``         cleanup

Everything is optional (looked up with getattr), so a plugin implements only
the hooks it needs. Absence is tolerated: a configured name whose package is
missing is logged and skipped — a public build simply ships without the plugin
packages and leaves ``CCBOT_PLUGINS`` empty.
"""

import importlib
import logging
import os
from types import ModuleType

logger = logging.getLogger(__name__)

_loaded: list[ModuleType] | None = None


def _plugin_names() -> list[str]:
    return [n.strip() for n in os.getenv("CCBOT_PLUGINS", "").split(",") if n.strip()]


def preload_configs() -> None:
    """Import each plugin's ``.config`` submodule at process start, BEFORE
    core ``Config()`` runs its scrub. Each plugin's config module reads its
    own token(s) from ``os.environ`` and pops them at module top — plugin-
    owned end-to-end. This ordering ensures the plugin *captures* its token
    before core scrubs anything, and the token is still gone from
    ``os.environ`` by the time the tmux server spawns. Plugins without a
    ``.config`` (e.g. the mail plugin has nothing to scrub) are silently
    skipped, as are absent plugin packages.

    Called from core ``config.py`` at module scope, so it runs before any
    importer of ``ccbot.config`` sees the module-level ``config`` singleton.
    """
    for name in _plugin_names():
        try:
            importlib.import_module(f".{name}.config", __package__)
        except ModuleNotFoundError:
            # plugin package absent, or plugin has no .config submodule
            pass
        except Exception:
            logger.exception("plugin %r config preload failed", name)


def loaded() -> list[ModuleType]:
    """Import and cache the configured plugin modules; skip absent/broken ones.

    Cached after the first call — the set of plugins is fixed for a process.
    """
    global _loaded
    if _loaded is not None:
        return _loaded
    mods: list[ModuleType] = []
    for name in _plugin_names():
        try:
            mods.append(importlib.import_module(f".{name}", __package__))
        except ModuleNotFoundError as e:
            logger.warning("plugin %r configured but not installed: %s", name, e)
        except Exception:
            logger.exception("plugin %r failed to import", name)
    if mods:
        logger.info("plugins loaded: %s", ", ".join(m.__name__ for m in mods))
    _loaded = mods
    return mods


def register_i18n() -> None:
    """Merge every loaded plugin's ``STRINGS`` into the i18n catalog.

    Called once at startup, before the command menu is built (plugin command
    descriptions resolve through the catalog).
    """
    from . import i18n

    for mod in loaded():
        strings = getattr(mod, "STRINGS", None)
        if strings:
            i18n.register(strings)


def bot_commands() -> list:
    """Concatenate every loaded plugin's ``bot_commands()`` contributions."""
    out: list = []
    for mod in loaded():
        fn = getattr(mod, "bot_commands", None)
        if fn is not None:
            out.extend(fn())
    return out


def register_handlers(app) -> None:
    """Let every loaded plugin register its PTB handlers."""
    for mod in loaded():
        fn = getattr(mod, "register_handlers", None)
        if fn is not None:
            fn(app)


async def on_startup(app) -> None:
    """Run every loaded plugin's ``on_startup`` — one failing plugin never
    takes down the bot or its siblings."""
    for mod in loaded():
        fn = getattr(mod, "on_startup", None)
        if fn is not None:
            try:
                await fn(app)
            except Exception:
                logger.exception("plugin %s on_startup failed", mod.__name__)


async def on_shutdown() -> None:
    """Run every loaded plugin's ``on_shutdown`` (best-effort)."""
    for mod in loaded():
        fn = getattr(mod, "on_shutdown", None)
        if fn is not None:
            try:
                await fn()
            except Exception:
                logger.exception("plugin %s on_shutdown failed", mod.__name__)

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
  - ``status_sections() -> tuple[list[str], list[str]]``
                                    (/status section blocks, warning labels).
                                    Called from the /status builder's WORKER
                                    THREAD — must be sync and do its own
                                    bounded probing (timeouts on subprocess /
                                    filesystem), mirroring the core sections.
  - ``status_buttons() -> list``    InlineKeyboardButton row extras for /status
  - ``callback_dispatch() -> list[tuple[str, handler]]``
                                    (callback-data prefix, async handler) pairs
                                    appended to the core dispatcher; handler
                                    signature matches core:
                                    ``async fn(query, data, update, context, user)``

Everything is optional (looked up with getattr), so a plugin implements only
the hooks it needs. Absence is tolerated: a configured name whose package is
missing is logged and skipped — a public build simply ships without the plugin
packages and leaves ``CCBOT_PLUGINS`` empty.
"""

import importlib
import logging
import os
from types import ModuleType
from typing import Any

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


def status_sections() -> tuple[list[str], list[str]]:
    """Collect every plugin's /status contributions: (sections, warnings).

    Runs inside the /status builder's ``asyncio.to_thread`` worker — plugin
    implementations are synchronous and responsible for bounding their own
    probes (a hung filesystem or subprocess must time out, not wedge /status).
    A failing plugin contributes nothing; the core sections still render.
    """
    sections: list[str] = []
    warnings: list[str] = []
    for mod in loaded():
        fn = getattr(mod, "status_sections", None)
        if fn is None:
            continue
        try:
            s, w = fn()
            sections.extend(s)
            warnings.extend(w)
        except Exception:
            logger.exception("plugin %s status_sections failed", mod.__name__)
    return sections, warnings


def status_buttons() -> list:
    """Concatenate every plugin's /status keyboard button contributions."""
    out: list = []
    for mod in loaded():
        fn = getattr(mod, "status_buttons", None)
        if fn is None:
            continue
        try:
            out.extend(fn())
        except Exception:
            logger.exception("plugin %s status_buttons failed", mod.__name__)
    return out


_callback_dispatch: list[tuple[str, Any]] | None = None


def callback_dispatch() -> list[tuple[str, Any]]:
    """(prefix, handler) pairs from every plugin, cached after first call.

    Consulted by the core callback dispatcher after its own tables miss, so
    plugin buttons get real handlers instead of the "Unknown callback data"
    log line. Prefixes are plugin-owned; keep them namespaced (e.g. ``dr:``)
    to avoid colliding with core ``CB_*`` values.
    """
    global _callback_dispatch
    if _callback_dispatch is not None:
        return _callback_dispatch
    pairs: list[tuple[str, Any]] = []
    for mod in loaded():
        fn = getattr(mod, "callback_dispatch", None)
        if fn is None:
            continue
        try:
            pairs.extend(fn())
        except Exception:
            logger.exception("plugin %s callback_dispatch failed", mod.__name__)
    _callback_dispatch = pairs
    return pairs

"""The /inject endpoint — local fire-and-forget task injection into agents.

A unix-socket HTTP listener that types a task into a docker agent's pane
*as a prompt* (never as a shell command). A hook for local automation
(a shortcut or script that hands a task to a running agent). Two modules:

- ``core``   — pure, unit-tested: ``sanitize_inject_text`` (the leading-``!``
  RCE shield + control-byte stripping). The security perimeter.
- ``server`` — aiohttp plumbing: unix socket, token auth, allowlist gate,
  reject-if-busy, push via ``session_manager.send_to_window``.

Feature-flagged on ``CCBOT_INJECT_TOKEN`` (see ``config.InjectConfig``):
empty token → server never starts, zero impact when unused.
"""

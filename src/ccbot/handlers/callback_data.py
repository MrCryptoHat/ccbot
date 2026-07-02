"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI nav keys (sent when Claude shows an interactive prompt)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"  # rs:sel:<index>
CB_SESSION_NEW = "rs:new"  # start a new session
CB_SESSION_CANCEL = "rs:cancel"  # cancel
CB_SESSION_BROWSE = "rs:browse"  # open directory browser (wrong auto-bound folder)

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>

# /status inline buttons
CB_STATUS_REFRESH = "st:ref"

# Agent panel inline keyboard (cm: prefix). The panel has two tabs —
# "nav" (raw key presses: arrows, Space, Tab, Esc, ^C, Enter, "/") and
# "act" (session-level actions: Compact / Clear / Model / Mode / Restart
# / Kill). Tab switch + refresh both carry the tab in the payload so a
# tap doesn't lose context. Payload shapes:
#   cm:<action>:<window_id>            — per-window action button
#   cm:tab:<tab>:<window_id>           — switch keyboard to tab nav/act
#   cm:ref:<tab>:<window_id>           — refresh photo, keep tab
#   cm:cfm:<action>:<window_id>        — confirm destructive action
#   cm:can:<window_id>                 — cancel destructive confirmation
CB_CMD_PREFIX = "cm:"
CB_CMD_CLEAR = "cm:clear:"
CB_CMD_COMPACT = "cm:compact:"
CB_CMD_MODEL = "cm:model:"
CB_CMD_MCP = "cm:mcp:"
CB_CMD_RESUME = "cm:resume:"
CB_CMD_CONTEXT = "cm:ctx:"
CB_CMD_MODE_CYCLE = "cm:mcyc:"  # Shift+Tab — cycles normal/auto-accept/plan
CB_CMD_EFFORT = "cm:effort:"
CB_CMD_WIPE_INPUT = "cm:wipe:"  # Ctrl+U ×N — стереть набранный в инпуте текст
CB_CMD_RESTART = "cm:restart:"
CB_CMD_FRESH = (
    "cm:fresh:"  # restart Claude with a brand-new session_id (old one stays in /resume)
)
CB_CMD_KILL = "cm:kill:"
CB_CMD_REFRESH = "cm:ref:"  # cm:ref:<tab>:<window_id>
CB_CMD_TAB = "cm:tab:"  # cm:tab:<tab>:<window_id>
CB_CMD_CONFIRM = "cm:cfm:"  # cm:cfm:<action>:<window_id>
CB_CMD_CANCEL = "cm:can:"  # cm:can:<window_id>

# Worktree agents (parallel agents on one project; see handlers/worktrees.py)
CB_WT_NEW = "wt:new:"  # wt:new:<window_id> — ➕ новый агент в проекте
CB_WT_DROP = "wt:drop:"  # wt:drop:<thread_id> — 🧨 force-delete on close guard
CB_WT_KEEP = "wt:keep:"  # wt:keep:<thread_id> — ↩ вернуть (reopen) топик
CB_WT_DEL = "wt:del:"  # wt:del:<window_id> — 🗑 удалить агента (panel button)
CB_WT_DELOK = "wt:delok:"  # wt:delok:<thread_id> — confirm 🗑 delete
CB_WT_DELNO = "wt:delno:"  # wt:delno:<window_id> — cancel 🗑 delete

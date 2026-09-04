"""A topic that exists but isn't wired up yet — hold its first message.

``create_forum_topic`` returns before the agent behind that topic exists:
provisioning a docker sibling still has to start Claude in the container and
watch the hook (seconds), and a worktree still has to `git worktree add` and
seed the checkout. The topic is already visible in Telegram all that while, so
a user who types into it immediately lands in ``text_handler`` with **no
binding and no directory memory** — and the unbound-topic fallback then does
the only thing it can: offers the HOST directory browser, in a topic whose
agent was about to come up inside a container (observed 2026-09-04).

The fix is a claim, not a lock: the provisioning coroutine marks the topic for
the duration, and the inbound paths wait on that mark before deciding the
topic is unbound. After the wait they re-read the state and route normally —
so a docker sibling gets the message delivered, and a worktree/tmux sibling
(deliberately left unbound, with only its directory remembered) gets the
session picker it was always meant to show.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Upper bound on how long an inbound message waits for a provision. Generous
# on purpose: the docker path alone can spend ~12 s probing the container's
# hook. On timeout we fall through to the normal unbound handling rather than
# hang the message — a stuck provision must not eat the user's text.
PROVISION_WAIT_TIMEOUT_SEC = 45.0

# (user_id, thread_id) → event set when that topic's provisioning ends.
_claims: dict[tuple[int, int], asyncio.Event] = {}


@contextlib.contextmanager
def claim_topic(user_id: int, thread_id: int) -> Iterator[None]:
    """Mark a freshly created topic as still being provisioned.

    Entered right after ``create_forum_topic``; the claim is released on the
    way out, success or failure (a rolled-back topic releases waiters too —
    they then see an unbound topic, which by then is the truth).
    """
    key = (user_id, thread_id)
    event = asyncio.Event()
    _claims[key] = event
    try:
        yield
    finally:
        _claims.pop(key, None)
        event.set()


async def wait_for_topic(user_id: int, thread_id: int | None) -> bool:
    """Wait out an in-flight provision of this topic. True iff we waited.

    A True return means the caller's earlier binding/state lookups are stale
    and must be re-read; False means nothing was in flight and nothing was
    waited for.
    """
    if thread_id is None:
        return False
    event = _claims.get((user_id, thread_id))
    if event is None:
        return False
    logger.info(
        "Message arrived mid-provision — holding it (user=%d, thread=%d)",
        user_id,
        thread_id,
    )
    try:
        await asyncio.wait_for(event.wait(), timeout=PROVISION_WAIT_TIMEOUT_SEC)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "Provision of thread %d didn't finish within %.0fs — routing the "
            "message as usual",
            thread_id,
            PROVISION_WAIT_TIMEOUT_SEC,
        )
    return True

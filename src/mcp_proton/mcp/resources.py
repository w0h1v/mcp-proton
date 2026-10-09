"""Resources: capability reports, mailbox metadata, operation status, message content.

URIs carry opaque identifiers only (account name, operation id, message
handle); subjects, addresses and mailbox names never appear in them. Every
read resolves the same caller identity and runs the same service functions
(and so the same policy checks) as the equivalent tool.
"""

import json
from typing import Any

from fastmcp.server.providers import LocalProvider

from ..services import accounts, mailboxes, messages
from .tools.base import UNTRUSTED, Gateway

JSON = "application/json"


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @p.resource("proton://capabilities/{account}", name="capabilities", mime_type=JSON,
                description="Capability and Bridge inventory report for one account.")
    async def capabilities(account: str) -> str:
        return _text(await gw.aread(lambda c: accounts.capabilities(app, c, account),
                                    resource=True))

    @p.resource("proton://mailboxes/{account}", name="mailboxes", mime_type=JSON,
                description="Mailboxes of one account that the caller may read.")
    async def mailbox_list(account: str) -> str:
        return _text(await gw.aread(
            lambda c: {"items": mailboxes.list_mailboxes(app, c, account)}, resource=True))

    @p.resource("proton://operations/{operation_id}", name="operation", mime_type=JSON,
                description="Status of one of the caller's own operations.")
    async def operation(operation_id: str) -> str:
        return _text(await gw.aread(lambda c: app.status(c, operation_id), resource=True))

    @p.resource("proton://messages/{handle}", name="message", mime_type=JSON,
                description="One message by opaque handle. " + UNTRUSTED)
    async def message(handle: str) -> str:
        def run(c: Any) -> dict[str, Any]:
            return {"message": messages.get_message(app, c, handle), "notice": UNTRUSTED}

        return _text(await gw.aread(run, resource=True))

    return p

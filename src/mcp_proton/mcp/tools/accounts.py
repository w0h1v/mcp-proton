"""accounts_*: configured accounts, connection health and capability reports."""

from typing import Any

from fastmcp.server.providers import LocalProvider

from ...services import accounts
from .base import AccountParam, Gateway


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "accounts_list",
             "List the accounts you may use, with sender identities and paused state.",
             title="List accounts")
    def accounts_list() -> dict[str, Any]:
        return gw.read(lambda c: {"items": accounts.list_accounts(app, c)})

    @gw.tool(p, "accounts_health",
             "Check the connection to Proton Mail Bridge for one account (never raises for "
             "connection problems; read ok in the result).", title="Account health")
    def accounts_health(account: AccountParam) -> dict[str, Any]:
        return gw.read(lambda c: accounts.health(app, c, account))

    @gw.tool(p, "accounts_capabilities",
             "Report which mail operations are available, unavailable or unverified for the "
             "connected Bridge version, plus protocol capabilities.", title="Capabilities")
    def accounts_capabilities(account: AccountParam) -> dict[str, Any]:
        return gw.read(lambda c: accounts.capabilities(app, c, account))

    return p

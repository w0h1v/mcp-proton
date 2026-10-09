"""operations_*: caller-scoped status, resume and cancel for journaled writes."""

from typing import Any

from fastmcp import Context
from fastmcp.server.providers import LocalProvider

from ...domain.models import OperationStatus
from ...services import messages
from .base import Access, Gateway, OperationIdParam, WriteResult, render_outcome


def build(gw: Gateway) -> LocalProvider:
    app, p = gw.app, gw.provider()

    @gw.tool(p, "operations_status",
             "Status of one of your own operations: pending, approved, denied, expired, "
             "executing, succeeded, partially_succeeded, failed, cancelled or delivery_unknown, "
             "with per-item results and any error. Other clients' operations are not visible.",
             title="Operation status")
    def operations_status(operation_id: OperationIdParam) -> dict[str, Any]:
        return gw.read(lambda c: app.status(c, operation_id))

    @gw.tool(p, "operations_resume",
             "Execute an operation the owner has approved. It runs the stored request exactly "
             "as approved (no new arguments), at most once; policy is rechecked now. While the "
             "owner has not decided it returns approval_pending again.",
             access=Access.WRITE, idempotent=True, title="Resume approved operation")
    async def operations_resume(ctx: Context, operation_id: OperationIdParam) -> WriteResult:
        return await gw.write(ctx, lambda c: app.resume(c, operation_id))

    @gw.tool(p, "operations_cancel",
             "Cancel a pending or approved operation, or ask a running bulk operation to stop "
             "before its next chunk (work already applied is not rolled back).",
             access=Access.WRITE, idempotent=True, approval=False, title="Cancel operation")
    def operations_cancel(operation_id: OperationIdParam) -> dict[str, Any]:
        def run(c: Any) -> dict[str, Any]:
            current = app.status(c, operation_id)  # caller-scoped: NOT_FOUND for others
            if current.status is OperationStatus.EXECUTING:
                messages.request_cancel(operation_id)
                return {**render_outcome(current), "cancel_requested": True}
            return render_outcome(app.cancel(c, operation_id))

        return gw.read(run)

    return p

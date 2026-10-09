from datetime import UTC, datetime, timedelta

from mcp_proton.domain.models import OperationStatus
from mcp_proton.services.retention import apply_retention

from .test_kernel import HERMES, make_app, send_req


def test_retention_keeps_pending_and_recent_and_removes_old():
    app = make_app()  # assistant: send is Ask -> pending
    pending = app.run(HERMES, send_req())
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    app.db.query("INSERT INTO events(account, mailbox, type, data_json, created_at) "
                 "VALUES ('me','INBOX','message_added','{}',?)", (old,))
    counts = apply_retention(app, 30)
    assert counts["events"] == 1
    assert app.status(HERMES, pending.operation_id).status is OperationStatus.PENDING

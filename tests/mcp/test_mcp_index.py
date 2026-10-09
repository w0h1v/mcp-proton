"""Index tools are registered only for non-live storage and go through policy."""

import pytest

from conftest import ACCOUNT, call, client_for  # type: ignore[import-not-found]
from mcp_proton.config import StorageMode


async def test_live_mode_hides_local_search_but_keeps_saved_searches(app):
    async with client_for(app) as c:
        names = {t.name for t in await c.list_tools()}
    assert "search_local" not in names and "stats_get" not in names
    assert {"saved_searches_list", "saved_searches_save", "saved_searches_run"} <= names


async def test_saved_structured_search_round_trip(app, seed):
    seed("INBOX", subject="Invoice 42")
    async with client_for(app) as c:
        saved = await call(c, "saved_searches_save",
                           {"account": ACCOUNT, "name": "inv", "query": {"subject": "Invoice"}})
        assert "error" not in saved
        res = await call(c, "saved_searches_run", {"account": ACCOUNT, "name": "inv"})
        assert res["total"] == 1
        listed = await call(c, "saved_searches_list", {"account": ACCOUNT})
        assert [s["name"] for s in listed["items"]] == ["inv"]
        assert (await call(c, "saved_searches_delete", {"account": ACCOUNT, "name": "inv"}))[
            "deleted"] is True


@pytest.mark.parametrize("mode", [StorageMode.INDEX])
async def test_index_mode_local_search(app, seed, mode):
    from mcp_proton.index import Indexer

    app.config.storage_mode = mode
    seed("INBOX", subject="Quarterly report", body="pineapple budget")
    Indexer(app).sync(ACCOUNT, "INBOX")
    async with client_for(app) as c:
        res = await call(c, "search_local", {"account": ACCOUNT, "text": "pineapple"})
        stats = await call(c, "stats_get", {"account": ACCOUNT})
    assert len(res["items"]) == 1 and res["source"] == "local_index"
    assert "error" not in stats

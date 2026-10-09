"""Optional local metadata cache and full-text index (storage modes ``metadata`` / ``index``).

``live`` mode (the default) retains nothing from this package. See ``store.py`` for the
retention, purge, export and encryption notes.
"""

from . import migrations  # noqa: F401  (registers the schema)
from .extract import extract_text, supported
from .indexer import Indexer, SyncReport
from .search import (
    LocalHit,
    LocalSearchResult,
    SavedSearch,
    delete_saved_search,
    get_saved_search,
    list_saved_searches,
    local_search,
    message_stats,
    run_saved_search,
    save_search,
)
from .store import IndexStore, export_index, purge, storage_report

__all__ = [
    "Indexer", "IndexStore", "LocalHit", "LocalSearchResult", "SavedSearch", "SyncReport",
    "delete_saved_search", "export_index", "extract_text", "get_saved_search",
    "list_saved_searches", "local_search", "message_stats", "purge", "run_saved_search",
    "save_search", "storage_report", "supported",
]

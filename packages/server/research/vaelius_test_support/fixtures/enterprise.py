"""Canonical PostgreSQL store factory for retained synthetic scenarios."""
from agenthub.cloud_runtime import CloudStore
from .postgres import database

def EnterpriseStore(home):
    store=CloudStore(home,database(home),'orchard')
    # These retained fixtures explicitly exercise the optional enrichment path.
    store.curation_enabled=True
    return store

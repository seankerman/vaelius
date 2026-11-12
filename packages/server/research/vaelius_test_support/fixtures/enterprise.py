"""Canonical PostgreSQL store factory for retained synthetic scenarios."""
from agenthub.cloud_runtime import CloudStore
from .postgres import database

def EnterpriseStore(home):
    return CloudStore(home,database(home),'orchard')

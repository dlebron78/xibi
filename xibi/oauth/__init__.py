"""OAuth credential layer for multi-account, DB-backed credential storage.

Public surface is intentionally NOT re-exported from this package — callers
import the submodule they need (xibi.oauth.store, xibi.oauth.google,
xibi.oauth.server) so that test-time imports do not trigger SQLite path
resolution at module load.
"""

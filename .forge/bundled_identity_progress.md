# Bundled identity progress
- [x] model column, migration plugin_bundled_identity (backfill by location, hash only as tie-break)
- [x] loader package_origin/root_clash_reason; runtime.load_installed; lifecycle install/upgrade/enable/views
- [x] leftover refusal on bundled install, purge_data + POST /{id}/purge-data
- [x] tests/plugin_host/test_bundled_identity.py (14 pass), evidence in .forge/evidence/bundled_identity_{before,after}.txt
- [x] full suite 5890 passed 150 skipped; examples 36; contract 375; alembic one head; MariaDB fresh + backfill ok

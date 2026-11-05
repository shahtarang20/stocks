# Generate the ccpp-fanout-v1 vectors

Run from the repo root with the API dependencies installed (writes `fanout_v1` in `docs/specs/ccpp-v1-vectors.json`, then compares the script with `apps/api/app/services/fanout_privacy.py`):

    python scripts/gen_fanout_vectors.py --write && python scripts/gen_fanout_vectors.py --check-app

Idempotent: a second `--write` leaves the file untouched. Then run `apps/api/tests/test_fanout_vectors.py` and the guardian/web vector tests, and review the git diff before committing.

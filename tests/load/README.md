# Load tests (B4)

Locust (pinned in `requirements.txt`). Local stack only (`docker compose up`, Postgres + Redis);
the locustfile refuses non-local hosts unless `LOAD_ALLOW_REMOTE=1` is set with owner approval.

```
python -m venv .venv-load && .venv-load/bin/pip install -r tests/load/requirements.txt
export LOAD_USER_EMAIL=... LOAD_USER_PASSWORD=... LOAD_MACHINE_ID=... LOAD_MACHINE_SECRET=msk_... LOAD_MODEL=...
.venv-load/bin/locust -f tests/load/locustfile.py --host http://localhost:8000 \
  --headless -u 50 -r 5 -t 5m --csv out/load
python tests/load/check_thresholds.py out/load
```

Thresholds in `thresholds.json` are null until a real baseline is recorded in
`docs/operations/load-test-baseline.md`; null never fails.

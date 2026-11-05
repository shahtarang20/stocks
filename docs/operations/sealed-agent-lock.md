# Sealed image: hash-pinned agent dependency lock

`os/sealed/scripts/build.sh` installs the agent with `--require-hashes`, so it needs
`os/sealed/agent-requirements.lock` (every transitive dependency, version and sha256).

- No lock is committed yet: hashes cannot be computed offline and are never written by hand.
- `os/sealed/scripts/make-lock.sh` generates it with `pip-compile --generate-hashes` from
  `provider-agent/pyproject.toml`; the pip-tools version is pinned in `os/sealed/requirements-build.txt`.
- CI (`.github/workflows/sealed-image.yml`) runs the script once when the file is absent, before
  build 1, so both reproducibility builds use the same lock. It is uploaded with the build artifacts.
- For a release: run the script on a build host, review the diff, commit the lock. A committed lock
  makes the CI step a no-op and the build offline-reproducible. `build.sh` also accepts
  `GENERATE_LOCK_IF_ABSENT=1`.
- Unverified: the pinned pip-tools version against this pyproject (TODO(verify-docs)).

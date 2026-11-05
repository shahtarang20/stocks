# syntax=docker/dockerfile:1
# EXAMPLE image for ComputeCoin BYOC (U8). Copy this directory, change the app, build and push to a registry on the
# platform's allowlist, then submit the image by DIGEST (never a tag).
#   docker build -t registry.example.com/acme/tool:1 -f Dockerfile .
#   docker push registry.example.com/acme/tool:1      # note the sha256 digest the registry prints
# Pin the base image by digest. The build fails when BASE_IMAGE is missing, 'PENDING' or a tag.
ARG BASE_IMAGE=PENDING

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac

# Everything the job needs is baked in: the container runs with --network none unless the spec declares egress and the
# platform allows it, so nothing can be downloaded at run time. Pin and hash-check what you install.
WORKDIR /app
COPY app/ /app/
RUN chmod -R a+rX,a-w /app

# Non-root. The host may override the user; do not rely on being uid 10001, and never write outside /out and /tmp.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload
USER 10001

# Contract at run time (see README.md):
#   rootfs read-only, /tmp is a tmpfs (noexec), /out is the only persistent writable path, /in is read-only inputs.
#   No privileged mode, devices, host mounts or capabilities exist. Use exec-form (a JSON array), never a shell string.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/run.py"]

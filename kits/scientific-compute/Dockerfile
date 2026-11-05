# syntax=docker/dockerfile:1
# ComputeCoin Kit: scientific-compute (untrusted customer Python script on numpy / scipy / pandas). Build context is kits/:
#   docker build -f kits/scientific-compute/Dockerfile kits
# BASE_IMAGE: a python image pinned by digest (name@sha256:...). The build fails on any unpinned or missing value.
# The image holds NO model weights; the only downloads are pip packages from requirements.txt (freeze with hashes in CI).
# The customer script is untrusted code: the platform runs this image with --network none, read-only rootfs, non-root,
# dropped capabilities, tmpfs /tmp noexec, writable /out only. Nothing in this image grants the script anything more.
ARG BASE_IMAGE

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY scientific-compute/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY _common/kitrt.py /app/kitrt.py
COPY scientific-compute/entrypoint.py /app/entrypoint.py
COPY scientific-compute/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /app
ENV PARAMS_SCHEMA=/app/params.schema.json PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 HOME=/tmp MPLCONFIGDIR=/tmp
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]

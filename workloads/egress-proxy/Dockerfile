# syntax=docker/dockerfile:1
# ComputeCoin egress proxy (B5): CONNECT-only allowlist proxy, standard library only.
# BASE_IMAGE must be a python image pinned by digest (name@sha256:...); the build FAILS on a tag (R7).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY proxy.py /app/proxy.py
RUN chmod 0444 /app/proxy.py
ENV COMPUTECOIN_PROXY_PORT=3128 COMPUTECOIN_EGRESS_ALLOW="" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 3128
ENTRYPOINT ["python", "/app/proxy.py"]

# syntax=docker/dockerfile:1
# ComputeCoin Kit: notebooks (U10 interactive session, JupyterLab). Build context is the kits/ directory:
#   docker build -f kits/notebooks/Dockerfile kits
# BASE_IMAGE: CUDA image with PyTorch (python3 on PATH) pinned by digest; 'PENDING' or an unpinned value fails the build.
# No weights, no downloads at run time: the session container has no network.
ARG BASE_IMAGE

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY notebooks/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY _common/kitrt.py /app/kitrt.py
COPY notebooks/entrypoint.py /app/entrypoint.py
COPY notebooks/params.schema.json /app/params.schema.json
# `nc` for the session agent's tcp attach (docker exec -i <ctr> nc 127.0.0.1 8888); a loopback-only shim, no package install.
COPY notebooks/nc_shim.py /usr/local/bin/nc
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod 0555 /usr/local/bin/nc \
 && chmod -R a+rX,a-w /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 HOME=/tmp HF_HUB_OFFLINE=1
# Jupyter listens on 127.0.0.1:8888 only (manifest session.tcp_port); the container publishes no port.
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]

# syntax=docker/dockerfile:1
# ComputeCoin rental template: pytorch-jupyter. Build context is the rental/templates directory:
#   docker build -f rental/templates/pytorch-jupyter/Dockerfile rental/templates
# The base image is pinned by digest (official PyTorch CUDA 12.4 runtime image, PyTorch 2.5.1). An unpinned value fails the build.
ARG BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY pytorch-jupyter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY _common/entrypoint.py /app/entrypoint.py
# `nc` for the session agent's tcp attach (docker exec -i <ctr> nc 127.0.0.1 8888); a loopback-only shim, no package install.
COPY _common/nc_shim.py /usr/local/bin/nc
RUN chmod 0555 /usr/local/bin/nc && chmod -R a+rX,a-w /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 HOME=/workspace
# Jupyter listens on 127.0.0.1:8888 only; the container publishes no port. The agent mounts the customer's workspace at /workspace
# and runs the image as an unprivileged user with a read-only root file system.
ENTRYPOINT ["python3", "/app/entrypoint.py"]

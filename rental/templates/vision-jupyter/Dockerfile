# syntax=docker/dockerfile:1
# ComputeCoin rental template: vision-jupyter (computer vision and face-recognition work). Build context is rental/templates:
#   docker build -f rental/templates/vision-jupyter/Dockerfile rental/templates
# The base image is pinned by digest (official PyTorch CUDA 12.4 runtime image, PyTorch 2.5.1). An unpinned value fails the build.
ARG BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY vision-jupyter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 # facenet-pytorch's package metadata pins an older torch; its code works with the torch in the base image, so it is installed without its
 # dependency list (requests and Pillow are already present as dependencies of the libraries above).
 && pip install --no-cache-dir --no-deps facenet-pytorch==2.6.0 \
 && rm /tmp/requirements.txt
COPY _common/entrypoint.py /app/entrypoint.py
COPY _common/nc_shim.py /usr/local/bin/nc
RUN chmod 0555 /usr/local/bin/nc && chmod -R a+rX,a-w /app
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 HOME=/workspace
ENTRYPOINT ["python3", "/app/entrypoint.py"]

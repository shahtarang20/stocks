# syntax=docker/dockerfile:1
# ComputeCoin Kit: ai-image (FLUX.1-schnell text-to-image). Build context is the kits/ directory:
#   docker build -f kits/ai-image/Dockerfile kits
# BASE_IMAGE must be a CUDA PyTorch image pinned by digest (name@sha256:...), FETCH_IMAGE a python image pinned
# by digest used only to download weights. The build fails on any unpinned or missing value.
ARG BASE_IMAGE
ARG FETCH_IMAGE

FROM ${FETCH_IMAGE} AS fetch
ARG MODEL_REPO=black-forest-labs/FLUX.1-schnell
# Exact commit of the model repo, and the SHA-256 of the sorted per-file SHA-256 listing. Both are 'PENDING'
# placeholders recorded by the owner after reviewing the model licence; the build fails until they are set.
ARG MODEL_REVISION=PENDING
ARG EXPECTED_MANIFEST_SHA256=PENDING
RUN test "${MODEL_REVISION}" != "PENDING" && test "${EXPECTED_MANIFEST_SHA256}" != "PENDING" \
 && pip install --no-cache-dir "huggingface_hub==0.27.1" \
 && python -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_REPO}', revision='${MODEL_REVISION}', local_dir='/model', allow_patterns=['*.json','*.safetensors','*.txt','*.model','LICENSE*'], ignore_patterns=['flux1-schnell.safetensors','ae.safetensors'])" \
 && cd /model && find . -type f ! -path './.cache/*' | sort | xargs sha256sum | sed 's# \./# #' > /model.sha256 \
 && rm -rf /model/.cache \
 && echo "${EXPECTED_MANIFEST_SHA256}  /model.sha256" | sha256sum -c -

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "${BASE_IMAGE}" in *@sha256:*) ;; *) echo "BASE_IMAGE must be pinned by digest" >&2; exit 1;; esac
COPY ai-image/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=fetch /model /opt/model
COPY --from=fetch /model.sha256 /opt/model.sha256
COPY _common/kitrt.py /app/kitrt.py
COPY ai-image/entrypoint.py /app/entrypoint.py
COPY ai-image/params.schema.json /app/params.schema.json
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin workload \
 && chmod -R a-w /opt/model /app
# No network at run time: the executor runs with --network none; offline flags make a stray fetch fail loudly.
ENV MODEL_DIR=/opt/model MODEL_MANIFEST=/opt/model.sha256 PARAMS_SCHEMA=/app/params.schema.json \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HOME=/tmp/hf PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
    CUBLAS_WORKSPACE_CONFIG=:4096:8
USER 10001
ENTRYPOINT ["python3", "/app/entrypoint.py"]

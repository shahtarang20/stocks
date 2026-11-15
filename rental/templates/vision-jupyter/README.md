# Template: vision-jupyter

JupyterLab on a GPU with PyTorch and computer-vision libraries, for image, face-detection and face-recognition work. Used by container rentals
(`docs/architecture/RENTAL-container-rental-design.md`). Everything in the `pytorch-jupyter` README about the workspace, `/workspace/results`,
inputs and network applies here too.

## What is inside, beyond pytorch-jupyter

| Piece | Version | Licence |
|---|---|---|
| opencv-python-headless | 4.10.0.84 | Apache-2.0 (bundles FFmpeg, LGPL) |
| albumentations | 1.4.21 | MIT |
| onnx, onnxruntime-gpu | 1.17.0 / 1.20.0 | Apache-2.0 / MIT |
| facenet-pytorch (installed without its dependency list; see the Dockerfile) | 2.6.0 | MIT |
| numpy, Pillow (pinned for facenet-pytorch) | 1.26.4 / 10.4.0 | BSD-style / HPND |

Not included: **insightface** (it builds C++ code when installed, and the image has no compiler), and any pretrained weights. facenet-pytorch
downloads its pretrained face models from GitHub the first time you ask for them, so ask for `github.com:443` and
`objects.githubusercontent.com:443` when you rent if you want them (both are on the platform's allowed list).
Model licences are your responsibility: check each pretrained model's terms before use.

## Build

`docker build -f rental/templates/vision-jupyter/Dockerfile rental/templates`. `onnxruntime-gpu` ships only an x86-64 wheel, so this template is
built for `linux/amd64` only.

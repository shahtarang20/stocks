# Template: pytorch-jupyter

JupyterLab on a GPU with PyTorch, for training or fine-tuning your own models. Used by container rentals
(`docs/architecture/RENTAL-container-rental-design.md`).

## What is inside

| Piece | Version | Licence |
|---|---|---|
| PyTorch, torchvision (from the base image `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`, pinned by digest) | 2.5.1 / 0.20.1 | BSD-style |
| JupyterLab, ipywidgets | 4.3.4 / 8.1.5 | BSD-3-Clause |
| pandas, scikit-learn, scipy, matplotlib, seaborn, tqdm | see `requirements.txt` | BSD-style |
| tensorboard, timm | 2.18.0 / 1.0.11 | Apache-2.0 |

No model weights are included.

## How it runs

* The rental's workspace is mounted at `/workspace` (your `HOME`). Notebooks, datasets and anything you install with `pip install --user` live
  there while the rental runs.
* Put what you want to keep in **`/workspace/results`**. Use "Save results now" on the session page, or stop the rental: the host saves that
  folder to your platform storage. **Everything else is deleted when the rental ends.**
* Your input files (chosen when you rent) are copied into `/workspace/<path>` before the notebook opens.
* There is no network unless you asked for allowed hosts when renting (for example `pypi.org:443` for `pip install`).
* The container is read-only except `/workspace` and a small temporary area, runs as an unprivileged user, and has no extra rights.
  It is **not** a private environment: the host can see your files. Use non-sensitive data only.

## Build

`docker build -f rental/templates/pytorch-jupyter/Dockerfile rental/templates` (or the manual workflow `.github/workflows/rental-templates.yml`).

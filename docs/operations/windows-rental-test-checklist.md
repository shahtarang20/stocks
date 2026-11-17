# Windows container rental: first real test (checklist)

_EXPERIMENTAL. Design: [W14](../architecture/W14-windows-container-rental.md). Needs a **Windows 11** PC with a GeForce or RTX card in normal (WDDM) mode, the Windows
agent already installed and connected, and a test customer account with test credits._

Stop at the first failure and send the report and a screenshot.

## A. On the Windows PC (administrator PowerShell, in the project folder)

1. `.\scripts\windows\rental-test-kit.ps1 -SkipRentalCheck` and read the report. Fix every FAIL (the line under it says what to do).
   The two that matter most: **Windows drive hidden** must PASS, and the Linux system must see the card.
2. `computecoin-agent rental check` must pass (a warning is allowed, a FAIL is not).
3. `computecoin-agent rental on`, then `Restart-Service ComputeCoinAgent`, then `computecoin-agent rental status` (both switches `yes`).

### The graphics card (do this before B)

3b. In the kit report, **NVIDIA card (inside)** must PASS: the Linux system sees the card through the Windows driver. If it says the card
    is not found, `nvidia-smi` may simply not be on the path there: `rental check` also tries `/usr/lib/wsl/lib/nvidia-smi` by its full path.
3c. `computecoin-agent rental check` shows an **NVIDIA driver** line (535.86 or newer) and picks its test image from it. Write down the driver
    version and which image ran (CUDA 12.4 for driver 550 and up, CUDA 12.2 for 535 to 549).
3d. While a rental with 1 card runs, in its terminal run `nvidia-smi`: it must list the card. Then `python3 -c "import torch;print(torch.cuda.is_available())"`
    in the PyTorch template: it must print `True`. If it does not, send the exact message (a "driver version is insufficient" means update the driver).

## B. On the server (the owner, in Railway variables)

4. `CONTAINER_RENTAL_ENABLED=true`, and for this one PC put its machine id in **`WINDOWS_RENTAL_MACHINE_IDS`** (the pilot list; nobody else gets Windows rentals). Do NOT turn on `WINDOWS_RENTAL_ENABLED` yet. Redeploy.

## C. On the website

5. My computers: set a price and Online. After a minute the Marketplace shows the PC with **Container rental** and **Windows PC (experimental)**.
6. As the test customer, Rent a GPU: pick that PC (a warning appears). Own image `python:3.11-slim` by digest, terminal, `/bin/bash`, 0 graphics
   cards (or 1 with a CUDA image), 30 minutes. Start.
7. In the terminal run `python3 -c "print(1+1)"`, write a file in `/workspace/results`, press Save results now, then Stop.

## D. Prove it stayed contained

8. On the PC, while a rental runs: `wsl -d computecoin --user root --exec ls -A /mnt/c` prints nothing. In the rental's terminal `ls /mnt` shows no `c`.
9. After the rental: no `ccoin-sess-*` container (`wsl -d computecoin --exec docker ps -a`) and the workspace folder is gone.
10. A Kit session never reaches this PC: create one as the customer and confirm it stays pending for a Windows-only host.
11. Put the PC to sleep during a rental on purpose and confirm the customer sees it end (and nothing is charged after it stopped).

Send back: the report file, the output of `computecoin-agent rental check`, and a screenshot of the finished session page.

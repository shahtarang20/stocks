# Windows tester guide (container rental, EXPERIMENTAL)

For the person who lends a Windows PC to test **container rental**: a customer runs their own image on your computer. This is not the Kit version.
Nothing here has been run on a real Windows PC yet, so you are the first. If a step fails, that is a result: send us the report (step 6).

**Status when this was written:** the Windows installer is not published yet. Do not start until the ComputeCoin team sends you the installer address.
Until then the website shows "The Windows installer has not been published yet" and offers no command.

## 1. What your PC needs

| Needs | How to check |
|---|---|
| Windows **11** (build 22000 or newer). Windows 10 does not work. | Settings, System, About |
| Virtualization **on** in the BIOS (Intel VT-x or AMD-V, often "SVM Mode") | Task Manager, Performance, CPU: "Virtualization: Enabled" |
| An NVIDIA **GeForce or RTX** card, with the current NVIDIA driver for Windows (535.86 or newer) | open PowerShell and run `nvidia-smi` |
| Plenty of free disk space (50 GB or more) on the drive that will hold the Linux system; the checker in step 5 reports the exact free space | File Explorer |
| Administrator rights | you must be able to open PowerShell with "Run as administrator" |

AMD and Intel cards, data-centre cards and cards in TCC mode do not work. If you have two cards, a customer is given **both together** (Docker inside Windows's Linux layer cannot pick one), so the PC is rented as two cards, one customer at a time.

## 2. How much of your PC the test can use (read this)

The customer's container runs inside a Linux system that Windows runs for us (WSL2). Microsoft's defaults for it:

* **Processor:** all cores. Nothing to change.
* **Memory:** only **50% of your RAM**. For a full-power test, raise it. We never change this for you, because the same file also configures any other Linux you use.
* **Graphics card:** the full power of your card (or cards), through your Windows NVIDIA driver. NVIDIA says some older multi-process CUDA features are not available in WSL, so multi-card training may behave differently; tell us if it does.

To raise the memory, create or edit the file `%UserProfile%\.wslconfig` (for example `C:\Users\<you>\.wslconfig`) and add:

```
[wsl2]
memory=24GB
```

Choose a number that leaves Windows at least 8 GB, and no more than your RAM. Then, when nothing important is running in Linux, run `wsl --shutdown`
in PowerShell. **That stops every Linux system you have, not only ours.** The new memory applies the next time it starts.

## 3. Install

1. On the website open **Provider, Add my computer**. Choose **Windows 11 (experimental)**.
2. Read what the installer does and tick "I have read this". Tick container rental if you are testing rental.
3. Press **Create token and copy the install command**.
4. Open PowerShell with **Run as administrator**, paste, and press Enter.
5. The installer is **not signed yet**, so Windows may warn that the publisher is unknown. If you got the address from the ComputeCoin team, continue.
6. The first start sets up the Linux system and can take several minutes. Then **My computers** shows your PC.

It is designed to install only into ComputeCoin's own folders and its own Linux system, and to leave your other Linux systems alone. That has not been proven on a real PC, so tell us if you see otherwise.

## 4. Set your price

In **My computers**, open the menu on your PC, choose **Edit price and availability**, set a price per hour and choose **Online**.
A Windows PC takes container rentals only, never Kits or ordinary jobs.

## 5. Check it

In an administrator PowerShell, from the project folder:

```
.\scripts\windows\rental-test-kit.ps1
```

It only reads: it changes nothing and prints no password or token. It checks Windows 11, virtualization, WSL, the Linux system, your card (and its mode), Docker,
that your **C: drive is hidden from customers' containers**, and runs one locked-down test container with the card.

## 6. Send us the result

The script writes `computecoin-rental-report-<date>.txt` in the current folder. Read it (it has no secrets), then send it. Also send:

* a note of anything that failed or looked odd, with the step number;
* what `nvidia-smi` prints on Windows;
* the memory you set in step 2.

## 7. Stop and remove

* To stop sharing at once, set the computer to **Offline** in My computers.
* A rental that is already running is allowed to finish.
* To turn off container rental only: `computecoin-agent rental off`, then restart the agent.

The full test list for the team is in `windows-rental-test-checklist.md`.

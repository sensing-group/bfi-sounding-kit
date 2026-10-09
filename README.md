# bfi-sounding-kit

Passive recording of Wi-Fi channel sounding with an ESP32-C5 board.

When a Wi-Fi 5 or Wi-Fi 6 router beamforms towards a device, it first sends a
**sounding request**, and the device replies with a **beamforming report**
describing the radio channel between them. Neither is encrypted. This kit
records both from a board that only receives, never transmits and never joins
a network.

## Contents

| folder | what it is |
|---|---|
| `kit/` | the commands the exercise uses |
| `kit/lib/` | the tools underneath them |
| `kit/home-sheet.md` | the form submitted with the recordings |
| `kit/second-receiver.md` | optional monitor-mode recipe for a laptop |
| `firmware/bin/` | the image to flash, ready for offset 0x0 |
| `firmware/src/` | the firmware source (ESP-IDF v6.1) |
| `logger/` | long-duration CSV logger, not used by the exercise |
| `analysis/` | merges logger CSV files, not used by the exercise |
| `docs/file-format.md` | the logger's CSV columns |

## What you need

- An ESP32-C5 board and a USB cable.
- Python 3.10 or newer. No administrator rights.
- A Wi-Fi network you are allowed to measure. 5 GHz works much better than
  2.4 GHz.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r logger/requirements.txt
.venv/bin/python -m esptool --chip esp32c5 write-flash 0x0 \
    firmware/bin/2026-10-08_bfi-probe_esp32c5.bin
```

Flashing takes about seven seconds. The board then appears as `/dev/ttyACM0`
on Linux, `/dev/cu.usbmodem…` on macOS and `COM3` or similar on Windows; pass
`--port` if it is not `/dev/ttyACM0`. On Linux your user must be in the
`dialout` group.

> **Ubuntu/Debian:** if `python3 -m venv` fails with "ensurepip is not
> available", either install `python3-venv` (needs admin rights) or, without
> admin rights:
> ```bash
> python3 -m venv --without-pip .venv
> curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
> ```

## The exercise

The exercise sheet is handed out in the course. It uses these commands:

```bash
.venv/bin/python kit/scan.py                        # networks in range
.venv/bin/python kit/scan.py --profile "MyWiFi"     # what the router says about itself
.venv/bin/python kit/record.py --network "MyWiFi" --task check --id <ID>
.venv/bin/python kit/summary.py data/<ID>_check.txt
.venv/bin/python kit/blocks.py --id <ID>            # the stopwatch for the experiment
.venv/bin/python kit/motion.py data/<ID>_experiment.txt --blocks data/<ID>_blocks.csv \
                               --device <address>
.venv/bin/python kit/anonymise.py data/* --labels labels.csv
.venv/bin/python kit/check.py --id <ID>             # before submitting
```

`--task` is one of `check`, `baseline`, `experiment`, `outside1`, `outside2`,
and `experiment2` for an optional repeat. It sets the recording length and the
file name, so every submission in the course has the same structure. For the
experiments, `--block-minutes 8` lengthens the recording to fit eight-minute
blocks.

Once the router's address is known, only that network is recorded: sounding
frames of neighbouring networks on the same channel are left out, by the
firmware and again by `kit/lib/probe.py`. Every recording carries clock marks
(`T <computer time> <board time>`), which line the stopwatch up with the
frames.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| no beacons, other traffic present | wrong part of the channel | rerun `scan.py`, use the channel it reports |
| nothing at all | too far away, or wrong band | move the board closer, use the 5 GHz entry |
| reports but no requests | router sends requests in a format the board cannot decode | normal, continue, state it in your report |
| requests but no reports | devices reply in a format the board cannot decode | normal, continue, state it |
| `record.py` or `check.py` says the board crashed and restarted | the computer power-cycled the USB port | unplug and replug the board, record again; turn off USB power saving (Windows: "USB selective suspend"; macOS: keep the laptop awake) |
| `scan.py --profile` finds no announcement of your network | neighbouring networks used up the capture budget | run it again |
| values change for no apparent reason | something else in the room changed | note it in the home sheet and repeat the block |
| `motion.py` reports too few pairs | your router sounds slowly | use `--lag 30`, or record for longer |
| a device's format says "cut short" | its reports are larger than the board can store | normal, note it, and watch another device in Task 3 if there is one |
| `record.py` says the file already exists | you recorded this task before | keep the first recording; a repeat of Task 3 goes in `--task experiment2` |

If a measurement fails, ask before repeating it.

## The logger

`logger/bfi-log.py` records for hours into hourly CSV files, with addresses
hashed. The exercise does not use it, apart from its network scan. Its columns
are documented in [docs/file-format.md](docs/file-format.md), and
`analysis/consolidate.py` merges its output.

## What the board does

It listens on one 20 MHz channel and writes one line per sounding frame, plus
per-second activity counters per device. It never transmits. It cannot decode
the contents of data frames, and nothing in this kit attempts to. It stores up
to 2048 bytes of each frame, enough for the beamforming reports of 2-stream
devices behind 4-antenna routers at 80 MHz.

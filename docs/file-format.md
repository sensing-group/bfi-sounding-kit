# File format

`bfi-log.py` writes one gzip-compressed CSV file per hour. The first line is a
comment with the capture settings, the second line is the column header:

```
# schema=3 home=H07 channel=100 start=2026-10-12T14:00:03+02:00 angle_seconds=60 angles_all=0
t,rec,sta,bss,aid,rssi,len,fmt,nr,nc,bw,ng,cb,legacy,tsf,extra
```

Each row is one event. `rec` says which kind:

| `rec` | Event |
|---|---|
| `N` | a sounding request from the router, one row per device named in it |
| `R` | a beamforming report from a device |
| `H` | a heartbeat, once a minute, with running counters |

## Columns

| Column | In rows | Meaning |
|---|---|---|
| `t` | all | time on the laptop, Unix seconds |
| `rec` | all | `N`, `R` or `H` |
| `sta` | N, R | the device, as vendor prefix plus keyed hash (`a46b40-9f2c1a`), or `rand-…` for a randomized address. Empty for requests that name several devices at once |
| `bss` | N | the router, hashed the same way |
| `aid` | N | association ID: the router's short number for the device |
| `rssi` | N, R | signal strength at the board, dBm |
| `len` | N, R | frame length in bytes |
| `fmt` | N, R | `VHT` (Wi-Fi 5) or `HE` (Wi-Fi 6) sounding |
| `nr` | R | antennas at the router side of the report |
| `nc` | N, R | columns (streams) requested (N) or reported (R) |
| `bw` | R | bandwidth the report covers, MHz |
| `ng` | R | subcarrier grouping (1 = every subcarrier, 4 = every fourth, ...) |
| `cb` | R | codebook bit: how finely each angle is quantised |
| `legacy` | R | 1 if the report arrived in the old 20 MHz frame format |
| `tsf` | N, R | the board's own microsecond clock |
| `extra` | R, H | R: the report body as hex, only inside angle windows. H: counters (`req=… rep=… ang=… other=…`) |

## The report body in `extra`

The hex starts at the category byte, after the frame header (which holds the
addresses and is removed):

| Bytes | Content |
|---|---|
| 1 | category: 21 = VHT, 30 = HE |
| 1 | action |
| 3 (VHT) or 5 (HE) | MIMO control: Nc, Nr, bandwidth, grouping, codebook |
| Nc | average SNR per stream |
| rest | the angles, packed least significant bit first, subcarrier by subcarrier |
| 4 | frame check sequence |

For VHT with codebook 1, each angle pair is 6 bits (phi) and 4 bits (psi).

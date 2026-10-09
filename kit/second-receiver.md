# Optional: a second receiver

Your board hears only the old, narrow format. If your router asks its
questions in the modern wide format, the board hears nothing at all, and from
the board alone there is no way to tell that apart from a router that is not
asking. A laptop Wi-Fi card can usually hear the wide format, so ten minutes
with both receivers side by side turns "we do not know" into a measurement.

This needs administrator rights on your own laptop. It is optional.

## What to do

1. Start a normal ten minute recording with the board, as in Part A.
2. At the same time, put your laptop's Wi-Fi card into listening mode on the
   same channel and record.
3. Hand in both files, and say in your report whether the laptop saw questions
   that the board did not.

Your laptop will lose its own Wi-Fi connection while it is listening. Plug in
a cable first if you need the internet, and put the card back afterwards.

## Linux

```bash
sudo nmcli dev set wlan0 managed no
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq <centre frequency> 80 <block centre>
sudo tcpdump -i wlan0 -s 200 -Z root -w laptop.pcap
```

For channel 36 that is `set freq 5180 80 5210`; for 100, `set freq 5500 80
5530`. `kit/scan.py` prints your channel, and the frequency is
5000 + 5 x channel.

Put it back when you are done:

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type managed
sudo ip link set wlan0 up
sudo nmcli dev set wlan0 managed yes
```

Check that the file is not empty before you trust it. A capture with a handful
of frames in ten minutes means the card was not really listening, usually
because something else grabbed the interface back.

## macOS

```bash
sudo /System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport \
     en0 sniff <channel>
```

The file lands in `/tmp/airportSniffXXXXXX.pcap`. Stop it with ctrl-c and the
card returns to normal by itself. Newer macOS versions have removed the
`airport` tool; if it is missing, use Wireless Diagnostics (hold alt, click
the Wi-Fi menu) and start a sniff from the Window menu.

## Windows

The built-in drivers do not support this. Skip it, or use a Linux live USB.

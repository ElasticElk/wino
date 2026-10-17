# Fridge scanner client

Thin client that POSTs `{barcode, mode, api_key}` to the scanner server on the
NAS. Telegram, Grok, and the cellar database stay there.

There is no Pi or USB scanner yet. Until there is, the backend is exercised
with `--ping` / `--once` (or the mock stdin backend). Hillside's
registered device is **Kitchen Pi** (storage: Big Fridge) — copy its API key
from Django Admin → Scanners, or pass `SCANNER_API_KEY`.

## Without hardware

```sh
# from scanner-service/, against the NAS server
SCANNER_URL=http://127.0.0.1:8080 SCANNER_API_KEY=<kitchen-pi-key> \
  python client/scanner_client.py --config client/config.yaml --ping

SCANNER_URL=http://127.0.0.1:8080 SCANNER_API_KEY=<kitchen-pi-key> \
  python client/scanner_client.py --config client/config.yaml --once 4006381333931 --mode in
```

Mock backend types barcodes on stdin (`m` toggles IN/OUT).

## Hardware

When a device exists:

| Item | Notes |
|---|---|
| Raspberry Pi (any with USB + GPIO) | Zero 2 W is enough |
| USB barcode scanner (Netum / Tera) | HID keyboard, no drivers |
| SPDT latching toggle | IN when open (pull-up HIGH), OUT when closed |
| Piezo buzzer (active, 3.3V) | Optional audio |
| Green + red LED + 330Ω | Optional mode lights |

```
GPIO17 ──── Toggle Switch ──── GND
GPIO27 ──── 330Ω ──── Green LED(+) ──── GND     (IN)
GPIO22 ──── 330Ω ──── Red LED(+) ──── GND       (OUT)
GPIO23 ──── Piezo Buzzer(+) ──── GND
USB      ──── Barcode Scanner
```

Find the scanner device after plugging it in:

```sh
ls -l /dev/input/by-id/
# then: scanner_device: "/dev/input/eventN"
```

## Install on the Pi

```sh
sudo apt install python3-pip python3-venv python3-evdev
sudo mkdir -p /opt/wine-scanner
sudo rsync -a scanner-service/ /opt/wine-scanner/
cd /opt/wine-scanner
python3 -m venv venv
source venv/bin/activate
pip install -r client/requirements.txt
cp client/config.yaml.sample client/config.yaml
# edit url + api_key + scanner_device
python client/scanner_client.py --ping
sudo cp client/wine-scanner.service /etc/systemd/system/
sudo systemctl enable --now wine-scanner
```

The `pi` user needs the `input` and `gpio` groups.

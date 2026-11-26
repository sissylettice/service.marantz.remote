# Marantz AVR Remote

A Kodi service add-on that turns any phone or browser on your network into a remote for a Marantz (or Denon) AV receiver.

The add-on keeps a network connection to the receiver and serves a small web page from the Kodi box. Changes made on the receiver itself, its own remote or another app show up on the page straight away.

Written for and tested with a **Marantz AV7705** on **Kodi 21 (Omega)** / LibreELEC. Other Marantz and Denon receivers from about 2015 onwards use the same control protocol and will probably work, but haven't been tested.

## What you can control

- Power (main zone) and mute
- Master volume: slider and −/+ buttons, with a configurable maximum
- Channel levels: centre, surround L/R, subwoofer and LFE
- Input, with the names you gave inputs on the receiver; unused inputs are hidden
- Sound mode. The button for the current mode lights up, and the last Movie/Music/Game/Pure group used is outlined.
- Dynamic Compression, MultEQ, Dynamic Volume, Dynamic EQ, Loudness Management
- Front display on/off

## Requirements

- Kodi 19 or later (Python 3)
- The receiver on the same network as the Kodi box, with network control enabled. To turn it on from standby, set the receiver's **Network Standby** (sometimes called Network Control) to **On**.
- Ports 9000 (web page) and 12254 (WebSocket) free on the Kodi box. Both can be changed in the settings.

## Install

1. Copy the `service.marantz.remote` folder into Kodi's add-ons folder (`/storage/.kodi/addons/` on LibreELEC), or install a zip of it with *Add-ons → Install from zip file*.
2. Restart Kodi.
3. Under *Settings → Add-ons → My add-ons → Services*, open **Marantz AVR Remote**. Enable it if it's disabled, then choose **Configure** and set the receiver's IP address.
4. On your phone, open `http://<kodi-address>:9000/`. On an iPhone, *Share → Add to Home Screen* gives it a full-screen icon.

## Settings

| Setting | Default | |
|---|---|---|
| AVR IP address | `10.0.10.22` | Can also be changed from the card at the bottom of the web page. |
| Maximum volume | 80 (0 dB) | Absolute scale. The remote never sets the volume above this. |
| Volume display | Absolute (0–98) | Or relative dB (−80 to +18), as the receiver's menu calls them. |
| Web page port | 9000 | Takes effect after restarting Kodi. |
| WebSocket port | 12254 | Takes effect after restarting Kodi. |

## Notes and limits

- **No password.** Anyone on your local network who can open the page can control the receiver. Don't expose these ports to the internet.
- **Subwoofer and LFE levels** only respond in sound modes that use them, such as Dolby, DTS or Multi Ch In. In Stereo or Pure Direct the receiver ignores them.
- **The porthole light** (front illumination) can't be controlled. The receiver doesn't offer it over the network.
- **Only one control connection.** Most of these receivers accept a single network-control (telnet) connection at a time. Other apps that use telnet control may push the add-on off, and it reconnects automatically.

## Troubleshooting

- **The status dot is red** ("Kodi remote service offline"): the page can't reach the add-on. Check that it's enabled and that the page port and WebSocket port aren't used by something else.
- **The status dot is amber** ("AVR not connected"): the add-on can't reach the receiver. Check the IP address and that network control and Network Standby are on.
- **The Kodi log** (`/storage/.kodi/temp/kodi.log` on LibreELEC) has every message to and from the receiver, tagged `[service.marantz.remote]`.

## Licence

GPL-2.0-only; see [LICENSE.txt](LICENSE.txt). Bundled third-party code:

- [python-websocket-server](https://github.com/Pithikos/python-websocket-server) (MIT), in `resources/lib/websocket_server/`
- [noUiSlider](https://refreshless.com/nouislider/) 12.1.0 (MIT), in `html/noUiSlider-12.1.0/`

Not affiliated with or endorsed by Marantz, Denon or Sound United.

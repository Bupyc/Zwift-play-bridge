# Zwift Play Bridge

Use one or both Zwift Play controllers to steer and shift gears in Zwift. The bridge reads the physical controllers over Bluetooth and presents a single virtual Zwift Ride over Wahoo Direct Connect on your local network. You can use the factory bindings for both controllers or set your own in a TOML file.

> **Independent compatibility utility.** This project helps riders regain controls when they can only use one half of their Zwift Play pair. Zwift is a trademark of Zwift, Inc. The names identify compatible hardware and software. [Zwift Terms of Service](https://support.zwift.com/terms-of-service-HJt7VBYyH)

## Requirements

- macOS with Bluetooth and Bonjour, and [Python 3.11 or newer](https://www.python.org/downloads/macos/). Developed with Python 3.13.
- One or two Zwift Play controllers. Input decoding was checked against a right controller on firmware 1.3.1 and a left controller on firmware 2.0.1.
- For virtual shifting, a trainer that [supports Zwift virtual shifting](https://support.zwift.com/zwift-ready-and-zwift-cog-compatible-trainers-r1x1sNas0), such as the Wahoo KICKR v6.

## Set up on macOS

Clone or download this repository, then open Terminal in its folder. Wake the controller and disconnect it from Zwift and Companion so that the bridge can connect over Bluetooth.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python zwift_play_bridge.py --scan
```

Allow Bluetooth access if macOS prompts you. The scan shows each supported controller's Bluetooth name, side, and four-character suffix. Use the name or suffix it prints. For example, if it shows `Zwift Play (right, 12AB)`, either command below selects it.

```sh
.venv/bin/python zwift_play_bridge.py --device 12AB
.venv/bin/python zwift_play_bridge.py --device "Zwift Play"
```

Use the suffix if two controllers show the same name. Add `--name "My Play Bridge"` to change the name Zwift displays. Keep the bridge running while you ride.

### Use both controllers

Wake both controllers and use `--scan` to find their suffixes. This example uses the factory bindings.

```sh
.venv/bin/python zwift_play_bridge.py --both --left 12AB --right 34CD --passthrough
```

The left D-pad navigates, the right `A/B/Y/Z` buttons perform their usual actions, and the left and right side buttons shift down and up respectively. Each paddle steers outward and brakes inward. The bridge keeps both button states so you can press both shifters together in SRAM-Style. [Zwift Play controls](https://support.zwift.com/de/using-your-zwift-play-rkO3EZB3)

Omit `--passthrough` to use the TOML bindings for both controllers. Add `--monitor` to see each controller's input in the terminal.

### Pair in Zwift

1. In **Paired Devices → Power Source**, temporarily select **Zwift Play Bridge** and wait for it to connect.
2. Open **Controls** and select **Zwift Play Bridge** there.
3. Switch **Power Source** and **Resistance** back to your trainer. Keep the bridge selected under **Controls**.

The temporary Power Source connection starts the handshake that makes the bridge available in Controls. Repeat the sequence after restarting Zwift if the bridge is missing from Controls. [Direct Connect pairing notes](https://github.com/VoltageSolutions/GameControllerForZwift/blob/v0.5.0/docs/use/pair-with-zwift.md)

## Default mapping

| Action | Right Play | Left Play |
| --- | --- | --- |
| Steer left / right | Paddle left / right | Paddle left / right |
| Shift up / down | `Y` or side shift / `B` | D-pad up / down |
| Toggle the front chainring in SRAM-Style | `Z` | Side shift |
| Navigate left / right | | D-pad left / right |
| Select | `A` | |

For front shifting, choose **Hardware → Shift Style → SRAM-Style** and a **2×** range under **Gear Range** in Zwift. [Shift styles and gear ranges](https://support.zwift.com/en_us/shift-style-and-gear-range-rksYezzIC)

Edit `[buttons]` for the right controller or `[left_buttons]` for the left in [zwift-play-bridge.toml](zwift-play-bridge.toml). Restart the bridge to apply changes. Use `--config path/to/mapping.toml` for a separate mapping. Run the command below to see every available action, including braking, menu navigation, PowerUps, and Ride Ons.

```sh
.venv/bin/python zwift_play_bridge.py --list-actions
```

### Check the paddle or buttons

Stop the bridge with <kbd>Ctrl</kbd>+<kbd>C</kbd>, then start monitor mode with your own controller suffix.

```sh
.venv/bin/python zwift_play_bridge.py --device 12AB --monitor
```

Monitor mode shows the physical buttons, both paddle axes, and the mapped output. If steering is reversed, set `invert = true` under `[paddle]`. If the paddle moves only the `UD` axis, set `axis = "ud"`. Adjust `deadzone` for drift around the centre and `full_scale` to match the largest reading at full travel.

## Development

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The tests exercise controller selection, button and paddle mappings, and the Direct Connect handshake over a local TCP connection. Zwift and a physical controller are optional for the tests.

## License and credits

Released under the [GNU General Public License v3.0](LICENSE). The bridge uses the protocol documentation from [Game Controller for Zwift](https://github.com/VoltageSolutions/GameControllerForZwift/tree/v0.5.0/docs/how-it-works) and [Makinolo's Zwift Play input notes](https://github.com/ajchellew/zwiftplay/issues/5).

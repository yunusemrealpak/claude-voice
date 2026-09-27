"""PortAudio device lookup by name substring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sounddevice as sd

Kind = Literal["input", "output"]


class DeviceError(RuntimeError):
    """Raised when a configured device cannot be found."""


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    def channels(self, kind: Kind) -> int:
        return self.max_input_channels if kind == "input" else self.max_output_channels


def list_devices() -> list[Device]:
    return [
        Device(
            index=index,
            name=info["name"],
            max_input_channels=int(info["max_input_channels"]),
            max_output_channels=int(info["max_output_channels"]),
            default_samplerate=float(info["default_samplerate"]),
        )
        for index, info in enumerate(sd.query_devices())
    ]


def find_device(name: str, kind: Kind) -> Device:
    """The first device whose name contains `name`, or the system default when empty.

    Matched by name, never by index: PortAudio renumbers devices whenever one
    is plugged in or removed.
    """
    devices = list_devices()
    if not name.strip():
        try:
            index = int(sd.query_devices(kind=kind)["index"])
        except (sd.PortAudioError, KeyError, ValueError) as exc:
            raise DeviceError(f"there is no default {kind} device") from exc
        return devices[index]

    needle = name.casefold()
    for device in devices:
        if needle in device.name.casefold() and device.channels(kind) > 0:
            return device
    available = ", ".join(d.name for d in devices if d.channels(kind) > 0) or "none"
    raise DeviceError(f"no {kind} device matches {name!r}; available: {available}")

"""Shared in-memory device state read by every component.

Single source of truth. Components (scanner/sim, server, recorder) never
talk to each other directly -- they only read/write this object.
"""

from __future__ import annotations

import dataclasses

from .decoder import DecodedPayload


@dataclasses.dataclass
class DeviceState:
    """The latest decoded H5055 readings plus the BT/ET channel mapping.

    ``channels`` maps sensor number (1..6) to the temperature in the user's
    preferred unit (raw value already divided by ``temp_divisor``). A sensor
    that reports disconnected (0xFFFF) is removed from the dict.
    """

    bt_channel: int | None = None
    et_channel: int | None = None
    temp_divisor: float = 1.0

    def __post_init__(self) -> None:
        self.channels: dict[int, float] = {}
        self.battery: int | None = None
        self.rssi: int | None = None
        self.last_seen: float | None = None

    def update(self, payload: DecodedPayload, rssi: int | None, now: float) -> None:
        """Merge one decoded advertisement into the shared state.

        An H5055 advertisement carries only the two slots selected by its
        ``payload_index``; keep feeding all three variants so every channel
        gets populated. A slot whose value is ``None`` (0xFFFF) means the
        probe is unplugged and the channel is dropped.
        """
        for channel, reading in (
            (payload.base_channel, payload.channel_a),
            (payload.base_channel + 1, payload.channel_b),
        ):
            if reading.value is None:
                self.channels.pop(channel, None)
            else:
                self.channels[channel] = reading.value / self.temp_divisor
        self.battery = payload.battery
        self.rssi = rssi
        self.last_seen = now

    @property
    def bt(self) -> float | None:
        """Bean temperature, from the configured BT channel."""
        if self.bt_channel is None:
            return None
        return self.channels.get(self.bt_channel)

    @property
    def et(self) -> float | None:
        """Exhaust/environment temperature, from the configured ET channel."""
        if self.et_channel is None:
            return None
        return self.channels.get(self.et_channel)

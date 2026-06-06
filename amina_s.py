"""ZHA custom quirk for the Amina S EV charger.

Implements the manufacturer-specific 0xFEE7 "Amina S Control" cluster and
exposes useful Home Assistant entities derived from it (EV status bits,
alarm bits, offline configuration, session energy counters).

Install:
    1. Copy this file to a directory on your Home Assistant host, e.g.
       /config/custom_zha_quirks/amina_s.py
    2. In configuration.yaml add:

           zha:
             custom_quirks_path: /config/custom_zha_quirks/

    3. Restart Home Assistant.
    4. Reconfigure the Amina S device in the ZHA UI (or re-pair it) so that
       ZHA picks up the new cluster definitions and entities.

Based on the Amina Zigbee specification (Revision E) and the published
Zigbee2MQTT converter by somlioy.
"""

from __future__ import annotations

import asyncio
from typing import Any

from zigpy.quirks import CustomCluster
from zigpy.quirks.v2 import EntityType, QuirkBuilder
from zigpy.quirks.v2.homeassistant import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfTime,
)
from zigpy.quirks.v2.homeassistant.binary_sensor import BinarySensorDeviceClass
from zigpy.quirks.v2.homeassistant.number import NumberDeviceClass
from zigpy.quirks.v2.homeassistant.sensor import SensorDeviceClass, SensorStateClass
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import LevelControl, OnOff
from zigpy.zcl.clusters.homeautomation import ElectricalMeasurement
from zigpy.zcl.foundation import BaseAttributeDefs, GeneralCommand, ZCLAttributeDef

# NOTE: this must match the manufacturer string the device actually reports
# *exactly* (ZHA v2 matches manufacturer + model verbatim). Real units report
# the lowercase "amina distribution AS"; the spec prose capitalises it. Both
# forms are registered below to be safe.
AMINA = "amina distribution AS"
AMINA_SPEC = "Amina Distribution AS"
AMINA_MANUFACTURER_CODE = 0x143B
AMINA_CONTROL_CLUSTER_ID = 0xFEE7
AMINA_ENDPOINT = 10

# Bits in the EV Status bitmap16 (attribute 0x0003)
EV_BIT_CONNECTED = 0
EV_BIT_RELAYS_ACTIVE = 1
EV_BIT_POWER_DELIVERED = 2
EV_BIT_PAUSED = 3
EV_BIT_EV_READY = 4
EV_BIT_DERATED = 15

# Human-readable labels for each bit of the Alarms bitmap16 (attribute 0x0002)
ALARM_NAMES = (
    "Welded relay(s)",            # bit 0
    "Wrong voltage balance",      # bit 1
    "RDC-DD DC leakage",          # bit 2
    "RDC-DD AC leakage",          # bit 3
    "Temperature error",          # bit 4
    "Overvoltage",                # bit 5
    "Undervoltage",               # bit 6
    "Overcurrent",                # bit 7
    "Car communication error",    # bit 8
    "Charger processing error",   # bit 9
    "Critical overcurrent",       # bit 10
    "Critical powerloss",         # bit 11
)


class AminaControlCluster(CustomCluster):
    """Manufacturer-specific 0xFEE7 cluster on endpoint 10.

    In addition to the attributes that the device exposes over the air, this
    cluster maintains a set of "virtual" attributes (IDs 0xF000+) that are
    derived from the EV Status and Alarms bitmaps whenever those arrive. They
    are never read from the device; they exist solely so that ZHA can surface
    them as individual binary_sensor / sensor entities.
    """

    cluster_id = AMINA_CONTROL_CLUSTER_ID
    name = "Amina S Control"
    ep_attribute = "amina_control"
    # Pin the manufacturer code so every read/write on this cluster sets the
    # manufacturer-specific frame bit with 0x143B, regardless of whether the
    # device's node descriptor has been parsed by the time we get here.
    manufacturer_id_override = AMINA_MANUFACTURER_CODE

    # Virtual (computed) attribute IDs. 0xFxxx is safely outside any real
    # attribute range used by the device.
    VIRT_EV_CONNECTED = 0xF000
    VIRT_CHARGING = 0xF001
    VIRT_EV_READY = 0xF002
    VIRT_PAUSED = 0xF003
    VIRT_DERATED = 0xF004
    VIRT_ALARM_ACTIVE = 0xF010
    VIRT_EV_STATUS_TEXT = 0xF011
    VIRT_ALARMS_TEXT = 0xF012

    class AttributeDefs(BaseAttributeDefs):
        """Attributes of the Amina S Control cluster."""

        alarms = ZCLAttributeDef(
            id=0x0002, type=t.bitmap16, is_manufacturer_specific=True
        )
        ev_status = ZCLAttributeDef(
            id=0x0003, type=t.bitmap16, is_manufacturer_specific=True
        )
        connect_status = ZCLAttributeDef(
            id=0x0004, type=t.bitmap16, is_manufacturer_specific=True
        )
        single_phase = ZCLAttributeDef(
            id=0x0005, type=t.uint8_t, is_manufacturer_specific=True
        )
        offline_current = ZCLAttributeDef(
            id=0x0006, type=t.uint8_t, is_manufacturer_specific=True
        )
        offline_single_phase = ZCLAttributeDef(
            id=0x0007, type=t.uint8_t, is_manufacturer_specific=True
        )
        time_to_offline = ZCLAttributeDef(
            id=0x0008, type=t.uint16_t, is_manufacturer_specific=True
        )
        enable_offline = ZCLAttributeDef(
            id=0x0009, type=t.uint8_t, is_manufacturer_specific=True
        )
        total_active_energy = ZCLAttributeDef(
            id=0x0010, type=t.uint32_t, is_manufacturer_specific=True
        )
        last_session_energy = ZCLAttributeDef(
            id=0x0011, type=t.uint32_t, is_manufacturer_specific=True
        )

        # Virtual attributes - derived locally from ev_status / alarms.
        ev_connected = ZCLAttributeDef(id=0xF000, type=t.Bool)
        charging = ZCLAttributeDef(id=0xF001, type=t.Bool)
        ev_ready = ZCLAttributeDef(id=0xF002, type=t.Bool)
        paused = ZCLAttributeDef(id=0xF003, type=t.Bool)
        derated = ZCLAttributeDef(id=0xF004, type=t.Bool)
        alarm_active = ZCLAttributeDef(id=0xF010, type=t.Bool)
        ev_status_text = ZCLAttributeDef(id=0xF011, type=t.CharacterString)
        alarms_text = ZCLAttributeDef(id=0xF012, type=t.CharacterString)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # ZHA's `bind()` only runs on the configure / reconfigure flow, so on
        # an ordinary Home Assistant restart we get no opportunity there to
        # repopulate the derived entities. Schedule a one-shot delayed read
        # of ev_status / alarms whenever the cluster is instantiated so the
        # virtual sensors recover even after a plain restart.
        self._priming_task: asyncio.Task | None = asyncio.ensure_future(
            self._prime_status_attributes()
        )

    async def _prime_status_attributes(self) -> None:
        # Brief delay so the network and parent device have finished coming
        # up. Failures are benign — bind() handles the first-pair case and
        # spontaneous reports cover the steady state.
        await asyncio.sleep(5)
        try:
            await self.read_attributes(["ev_status", "alarms"])
        except Exception as exc:  # noqa: BLE001
            self.debug("delayed prime read failed: %s", exc)

    async def bind(self):
        """Configure reporting on the real bitmap attributes and prime the cache.

        ZHA calls `bind()` on each input cluster during the configure phase
        after a device joins. The user-facing entities are all derived from
        ev_status / alarms inside `_update_attribute`, so without an initial
        read and reporting config on those two attributes the derived
        entities can stay "unknown" indefinitely on first pair / reconfigure.
        """
        result = await super().bind()

        for attr_name in ("ev_status", "alarms"):
            try:
                await self.configure_reporting(
                    attribute=getattr(self.AttributeDefs, attr_name).id,
                    min_interval=0,
                    max_interval=0xFFFE,
                    reportable_change=1,
                )
            except Exception as exc:  # noqa: BLE001
                self.debug(
                    "configure_reporting failed for %s: %s", attr_name, exc
                )

        try:
            await self.read_attributes(["ev_status", "alarms"])
        except Exception as exc:  # noqa: BLE001
            self.debug("initial read of ev_status/alarms failed: %s", exc)

        return result

    def _update_attribute(self, attrid: int, value: Any) -> None:
        super()._update_attribute(attrid, value)

        if value is None:
            return

        if attrid == self.AttributeDefs.ev_status.id:
            self._update_ev_status_virtuals(int(value))
        elif attrid == self.AttributeDefs.alarms.id:
            self._update_alarms_virtuals(int(value))

    def _update_ev_status_virtuals(self, status: int) -> None:
        connected = bool(status & (1 << EV_BIT_CONNECTED))
        relays = bool(status & (1 << EV_BIT_RELAYS_ACTIVE))
        power = bool(status & (1 << EV_BIT_POWER_DELIVERED))
        paused = bool(status & (1 << EV_BIT_PAUSED))
        ready = bool(status & (1 << EV_BIT_EV_READY))
        derated = bool(status & (1 << EV_BIT_DERATED))

        super()._update_attribute(self.VIRT_EV_CONNECTED, connected)
        super()._update_attribute(self.VIRT_CHARGING, power)
        super()._update_attribute(self.VIRT_EV_READY, ready)
        super()._update_attribute(self.VIRT_PAUSED, paused)
        super()._update_attribute(self.VIRT_DERATED, derated)

        if not connected:
            text = "Not connected"
        elif power:
            text = "Charging"
        elif paused:
            text = "Paused"
        elif ready or relays:
            text = "Ready to charge"
        else:
            text = "EV connected"
        if derated:
            text += " (derated)"
        super()._update_attribute(self.VIRT_EV_STATUS_TEXT, text)

    def _update_alarms_virtuals(self, alarms: int) -> None:
        active = [
            ALARM_NAMES[bit] if bit < len(ALARM_NAMES) else f"Alarm bit {bit}"
            for bit in range(16)
            if alarms & (1 << bit)
        ]
        super()._update_attribute(self.VIRT_ALARM_ACTIVE, bool(active))
        super()._update_attribute(
            self.VIRT_ALARMS_TEXT,
            ", ".join(active) if active else "No alarm",
        )


class AminaOnOff(CustomCluster, OnOff):
    """OnOff cluster that rewrites Toggle commands.

    The Amina spec explicitly warns that the Toggle (0x02) command "may
    result in unwanted results" on this charger, even though ZCL rev 2
    requires it to be implemented. Most HA actions issue explicit On/Off,
    but `zha.issue_zigbee_cluster_command`, scenes and group commands can
    still send Toggle. We intercept it and translate it to the
    corresponding explicit command based on the cached on/off state. If
    the state isn't known yet we fall back to Off — for an EV charger,
    "do nothing / disarm" is the safer default.
    """

    def _update_attribute(self, attrid: int, value: Any) -> None:
        super()._update_attribute(attrid, value)
        if attrid == OnOff.AttributeDefs.on_off.id:
            # Arming / disarming changes the charging state, but the charger
            # may not immediately push an ev_status report. Re-read it so the
            # derived status entities update promptly.
            asyncio.ensure_future(self._refresh_ev_status())

    async def _refresh_ev_status(self) -> None:
        amina = self.endpoint.in_clusters.get(AMINA_CONTROL_CLUSTER_ID)
        if amina is None:
            return
        try:
            await amina.read_attributes(["ev_status"])
        except Exception as exc:  # noqa: BLE001
            self.debug("ev_status refresh after on/off change failed: %s", exc)

    async def command(
        self,
        command_id: GeneralCommand | int | str,
        *args: Any,
        manufacturer: int | None = None,
        expect_reply: bool = True,
        tsn: int | None = None,
        **kwargs: Any,
    ) -> Any:
        toggle_id = OnOff.ServerCommandDefs.toggle.id
        is_toggle = command_id == toggle_id or command_id == "toggle"

        if is_toggle:
            current_state = self._attr_cache.get(OnOff.AttributeDefs.on_off.id)
            if current_state is None:
                self.warning(
                    "Toggle requested but on/off state is unknown; "
                    "translating to Off as the safer default for a charger"
                )
                target_id = OnOff.ServerCommandDefs.off.id
            else:
                target_id = (
                    OnOff.ServerCommandDefs.off.id
                    if current_state
                    else OnOff.ServerCommandDefs.on.id
                )
                self.debug(
                    "Translating disallowed Toggle command to %s based on"
                    " cached state %s",
                    "Off" if current_state else "On",
                    current_state,
                )
            return await super().command(
                target_id,
                *args,
                manufacturer=manufacturer,
                expect_reply=expect_reply,
                tsn=tsn,
                **kwargs,
            )

        return await super().command(
            command_id,
            *args,
            manufacturer=manufacturer,
            expect_reply=expect_reply,
            tsn=tsn,
            **kwargs,
        )


class AminaElectricalMeasurement(CustomCluster, ElectricalMeasurement):
    """Electrical Measurement that polls the energy counters on power updates.

    The charger does not support attribute reporting for Total Active Energy
    (0xFEE7 / 0x0010) or Last Session Energy (0x0011), so those entities would
    otherwise only ever reflect the value read once at start-up. Following the
    Zigbee2MQTT converter's approach, we piggy-back on the reportable
    TotalActivePower attribute: each time a power update arrives we read the
    energy counters from the sibling 0xFEE7 cluster on the same endpoint.
    """

    async def bind(self):
        """Ensure TotalActivePower actually reports so the poll has a trigger."""
        result = await super().bind()
        try:
            await self.configure_reporting(
                attribute=ElectricalMeasurement.AttributeDefs.total_active_power.id,
                min_interval=30,
                max_interval=3600,
                reportable_change=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.debug("configure_reporting for total_active_power failed: %s", exc)
        return result

    def _update_attribute(self, attrid: int, value: Any) -> None:
        super()._update_attribute(attrid, value)
        if attrid == ElectricalMeasurement.AttributeDefs.total_active_power.id:
            # Fire-and-forget; failures are logged inside the coroutine.
            asyncio.ensure_future(self._poll_on_power_update())

    async def _poll_on_power_update(self) -> None:
        amina = self.endpoint.in_clusters.get(AMINA_CONTROL_CLUSTER_ID)
        if amina is None:
            return
        try:
            # Re-read ev_status alongside the energy counters: the charger
            # does not always push a fresh ev_status report when charging
            # stops, so the "Charging" / "EV status" entities can otherwise
            # stay stuck on "Charging" even after power has dropped to zero.
            await amina.read_attributes(
                ["ev_status", "total_active_energy", "last_session_energy"]
            )
        except Exception as exc:  # noqa: BLE001
            self.debug("post-power-update poll failed: %s", exc)


class AminaLevelControl(CustomCluster, LevelControl):
    """Level Control that turns current_level writes into moveToLevel commands.

    The charger's Level Control cluster reports current_level as "Read Report"
    only; to change the allowed charging current the spec says to send a
    moveToLevel command. Translating attribute writes into commands lets the
    ZHA number entity target current_level directly.

    We deliberately use the plain MoveToLevel (0x00) command rather than the
    MoveToLevelWithOnOff (0x04) variant: the latter also arms the charger
    whenever the level is non-zero, which caused the on/off switch to flicker
    on (then off) when the current limit was changed while the charger was
    off. Arming stays the sole responsibility of the On/Off switch.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Make sure min_level / max_level land in the cache so write
        # clamping is deterministic, including across HA restarts where
        # bind() does not run.
        self._priming_task: asyncio.Task | None = asyncio.ensure_future(
            self._prime_level_limits()
        )

    async def _prime_level_limits(self) -> None:
        await asyncio.sleep(5)
        try:
            await self.read_attributes(["min_level", "max_level"])
        except Exception as exc:  # noqa: BLE001
            self.debug("delayed prime of level limits failed: %s", exc)

    async def bind(self):
        """Read MinLevel/MaxLevel during ZHA's configure phase."""
        result = await super().bind()
        try:
            await self.read_attributes(["min_level", "max_level"])
        except Exception as exc:  # noqa: BLE001
            self.debug("initial read of level limits failed: %s", exc)
        return result

    async def _ensure_level_limits_cached(self) -> None:
        min_id = LevelControl.AttributeDefs.min_level.id
        max_id = LevelControl.AttributeDefs.max_level.id
        if min_id in self._attr_cache and max_id in self._attr_cache:
            return
        try:
            await self.read_attributes(["min_level", "max_level"])
        except Exception as exc:  # noqa: BLE001
            self.debug(
                "synchronous read of level limits failed (using fallback): %s",
                exc,
            )

    async def write_attributes(
        self,
        attributes: dict,
        manufacturer: int | None = None,
        **kwargs: Any,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        current_level_id = LevelControl.AttributeDefs.current_level.id
        redirected_level: int | None = None
        remaining: dict = {}

        for key, value in attributes.items():
            if key in ("current_level", current_level_id):
                redirected_level = int(value)
            else:
                remaining[key] = value

        aggregated: list[foundation.WriteAttributesStatusRecord] = []

        if redirected_level is not None:
            # The HA number entity has a static UI range, but the device's
            # actual MaxLevel reflects the physical current selector and may
            # be lower than the spec maximum (32A). Clamp against whatever
            # the device has told us, falling back to the spec defaults.
            # If the cache is cold (first write after a fresh start), force
            # a read so the clamp uses the real hardware limit.
            await self._ensure_level_limits_cached()
            min_level = self._attr_cache.get(
                LevelControl.AttributeDefs.min_level.id, 6
            )
            max_level = self._attr_cache.get(
                LevelControl.AttributeDefs.max_level.id, 32
            )
            clamped_level = max(int(min_level), min(int(max_level), redirected_level))
            if clamped_level != redirected_level:
                self.debug(
                    "Clamping requested charging current %dA to device-reported"
                    " range [%d, %d]A",
                    redirected_level,
                    min_level,
                    max_level,
                )

            await self.move_to_level(
                level=clamped_level,
                transition_time=0,
            )
            # Cache the new value locally so the UI updates immediately.
            self._update_attribute(current_level_id, clamped_level)
            aggregated.append(
                foundation.WriteAttributesStatusRecord(
                    status=foundation.Status.SUCCESS,
                    attrid=current_level_id,
                )
            )

        if remaining:
            result = await super().write_attributes(
                remaining, manufacturer=manufacturer, **kwargs
            )
            for group in result:
                aggregated.extend(group)

        return [aggregated]


# ---------------------------------------------------------------------------
# Quirk registration
# ---------------------------------------------------------------------------

(
    QuirkBuilder(AMINA, "amina S")
    # Same model, but the capitalised manufacturer string from the spec, in
    # case some firmware/units report it that way.
    .applies_to(AMINA_SPEC, "amina S")
    # The spec text refers to the legacy ModelIdentifier as "Amina S".
    .applies_to(AMINA, "Amina S")
    .applies_to(AMINA_SPEC, "Amina S")
    # Future model identifiers promised in the spec (S1/S3/C1/C3, etc.).
    .applies_to(AMINA, "S1")
    .applies_to(AMINA, "S3")
    .applies_to(AMINA, "C1")
    .applies_to(AMINA, "C3")
    # Replace the real clusters on the charger-control endpoint (10).
    .replaces(AminaControlCluster, endpoint_id=AMINA_ENDPOINT)
    .replaces(AminaLevelControl, endpoint_id=AMINA_ENDPOINT)
    .replaces(AminaOnOff, endpoint_id=AMINA_ENDPOINT)
    .replaces(AminaElectricalMeasurement, endpoint_id=AMINA_ENDPOINT)
    # -------- Charging current ------------------------------------------
    # The slider's upper bound is hard-coded to the spec maximum (32A)
    # because QuirkBuilder v2 doesn't support binding a number entity's
    # range to a live device attribute. The actual hardware cap (set by the
    # physical current-selector wheel) is exposed below as a diagnostic
    # sensor, and AminaLevelControl.write_attributes clamps writes against
    # the device-reported MinLevel/MaxLevel so out-of-range values from the
    # slider can't reach the wire.
    .number(
        attribute_name=LevelControl.AttributeDefs.current_level.name,
        cluster_id=LevelControl.cluster_id,
        endpoint_id=AMINA_ENDPOINT,
        min_value=6,
        max_value=32,
        step=1,
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        entity_type=EntityType.STANDARD,
        translation_key="charge_limit",
        fallback_name="Charge current limit",
    )
    .sensor(
        attribute_name=LevelControl.AttributeDefs.max_level.name,
        cluster_id=LevelControl.cluster_id,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        translation_key="hardware_current_limit",
        fallback_name="Hardware current limit",
    )
    # -------- Status text sensors --------------------------------------
    .sensor(
        attribute_name=AminaControlCluster.AttributeDefs.ev_status_text.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        translation_key="ev_status",
        fallback_name="EV status",
    )
    .sensor(
        attribute_name=AminaControlCluster.AttributeDefs.alarms_text.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="alarms",
        fallback_name="Alarms",
    )
    # -------- Binary sensors derived from EV status bits --------------
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.ev_connected.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.STANDARD,
        device_class=BinarySensorDeviceClass.PLUG,
        translation_key="ev_connected",
        fallback_name="EV connected",
    )
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.charging.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.STANDARD,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        translation_key="charging",
        fallback_name="Charging",
    )
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.ev_ready.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="ev_ready",
        fallback_name="EV ready",
    )
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.paused.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="paused",
        fallback_name="Charging paused",
    )
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.derated.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.HEAT,
        translation_key="derated",
        fallback_name="Thermal derating",
    )
    .binary_sensor(
        attribute_name=AminaControlCluster.AttributeDefs.alarm_active.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        entity_type=EntityType.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.PROBLEM,
        translation_key="alarm_active",
        fallback_name="Alarm active",
    )
    # -------- Energy counters ------------------------------------------
    # The charger doesn't support attribute reporting for these, so
    # AminaElectricalMeasurement re-reads them whenever TotalActivePower
    # reports (i.e. roughly every 30s-1h while charging, sooner when power
    # changes). They are also primed once on bind / restart.
    .sensor(
        attribute_name=AminaControlCluster.AttributeDefs.total_active_energy.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        divisor=1000,
        suggested_display_precision=2,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        translation_key="total_active_energy",
        fallback_name="Total energy",
    )
    .sensor(
        attribute_name=AminaControlCluster.AttributeDefs.last_session_energy.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        divisor=1000,
        suggested_display_precision=2,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        translation_key="last_session_energy",
        fallback_name="Last session energy",
    )
    # -------- Phase / offline configuration ----------------------------
    .switch(
        attribute_name=AminaControlCluster.AttributeDefs.single_phase.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        translation_key="single_phase",
        fallback_name="Force single-phase charging",
    )
    .switch(
        attribute_name=AminaControlCluster.AttributeDefs.enable_offline.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        translation_key="enable_offline",
        fallback_name="Enable offline mode",
    )
    .switch(
        attribute_name=AminaControlCluster.AttributeDefs.offline_single_phase.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        translation_key="offline_single_phase",
        fallback_name="Offline: single-phase",
    )
    .number(
        attribute_name=AminaControlCluster.AttributeDefs.offline_current.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        min_value=6,
        max_value=32,
        step=1,
        unit=UnitOfElectricCurrent.AMPERE,
        device_class=NumberDeviceClass.CURRENT,
        translation_key="offline_current",
        fallback_name="Offline current limit",
    )
    .number(
        attribute_name=AminaControlCluster.AttributeDefs.time_to_offline.name,
        cluster_id=AMINA_CONTROL_CLUSTER_ID,
        endpoint_id=AMINA_ENDPOINT,
        min_value=0,
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        device_class=NumberDeviceClass.DURATION,
        translation_key="time_to_offline",
        fallback_name="Time to offline",
    )
    .add_to_registry()
)

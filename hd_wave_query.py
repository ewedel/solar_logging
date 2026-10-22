#!/usr/bin/env python3
"""
Table-driven Modbus TCP reader for a SolarEdge HD-Wave inverter.

Reads the SunSpec common block (model 1) and inverter model block
(model 101/102/103) in a single bulk register read -- per SolarEdge's
Modbus TCP documentation, one large read is cheaper for the inverter's
CPU than several small ones -- and decodes the result using the
register tables below (see hd_wave_modbus_tcp.pdf for the source
register map).
"""

import argparse
import json
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from pyModbusTCP.client import ModbusClient


# ---------------------------------------------------------------------------
# Sentinels SunSpec uses to mark a register as "not implemented" by this
# device. Values carrying these sentinels are reported as None.
# ---------------------------------------------------------------------------

U_NOT_IMPLEMENTED = 0xFFFF
S_NOT_IMPLEMENTED = -32768


@dataclass(frozen=True)
class FieldSpec:
    """
    Describes one register-table entry: how to decode it and, for value
    fields, how to describe it in the JSON output.

    `length` means char count for type "string", register count for
    type "pad", and is ignored (always 1 or 2) for the numeric types.

    `scale_of` is only set on a scale-factor field itself: it names the
    keys of the value field(s) this scale factor applies to. A field
    with `scale_of` set, or `hidden` set, is decoded but left out of the
    output value list (headers, padding, and raw scale factors are
    plumbing, not loggable values).
    """
    key: str
    name: str
    type: str  # "uint16" | "int16" | "uint32" | "string" | "pad"
    units: Optional[str] = None
    length: int = 1
    scale_of: Tuple[str, ...] = ()
    hidden: bool = False


# SunSpec common block (model 1), base register 40000.
COMMON_BLOCK_FIELDS = (
    FieldSpec("sunspec_id", "SunSpec ID", "uint32", length=2, hidden=True),
    FieldSpec("sunspec_did", "SunSpec DID (common block)", "uint16", hidden=True),
    FieldSpec("sunspec_length", "SunSpec Length (common block)", "uint16", hidden=True),
    FieldSpec("manufacturer", "Manufacturer", "string", length=32),
    FieldSpec("model", "Model", "string", length=32),
    # Options sits between Model and Version in the SunSpec layout but SolarEdge
    # always reports it as NOT_IMPLEMENTED, so it's decoded and dropped.
    FieldSpec("options", "Options", "string", length=16, hidden=True),
    FieldSpec("version", "CPU Software Version", "string", length=16),
    FieldSpec("serial_number", "Serial Number", "string", length=32),
    FieldSpec("device_address", "Modbus Device Address", "uint16"),
)

# SunSpec inverter model block (model 101/102/103), immediately following
# the common block.
INVERTER_MODEL_FIELDS = (
    FieldSpec("model_did", "SunSpec DID (inverter model)", "uint16", hidden=True),
    FieldSpec("model_length", "SunSpec Length (inverter model)", "uint16", hidden=True),

    FieldSpec("ac_current", "AC Total Current", "uint16", "A"),
    FieldSpec("ac_current_a", "AC Phase A Current", "uint16", "A"),
    FieldSpec("ac_current_b", "AC Phase B Current", "uint16", "A"),
    FieldSpec("ac_current_c", "AC Phase C Current", "uint16", "A"),
    FieldSpec("ac_current_sf", "AC Current scale factor", "int16", hidden=True,
              scale_of=("ac_current", "ac_current_a", "ac_current_b", "ac_current_c")),

    FieldSpec("ac_voltage_ab", "AC Voltage Phase A-B", "uint16", "V"),
    FieldSpec("ac_voltage_bc", "AC Voltage Phase B-C", "uint16", "V"),
    FieldSpec("ac_voltage_ca", "AC Voltage Phase C-A", "uint16", "V"),
    FieldSpec("ac_voltage_an", "AC Voltage Phase A-N", "uint16", "V"),
    FieldSpec("ac_voltage_bn", "AC Voltage Phase B-N", "uint16", "V"),
    FieldSpec("ac_voltage_cn", "AC Voltage Phase C-N", "uint16", "V"),
    FieldSpec("ac_voltage_sf", "AC Voltage scale factor", "int16", hidden=True,
              scale_of=("ac_voltage_ab", "ac_voltage_bc", "ac_voltage_ca",
                         "ac_voltage_an", "ac_voltage_bn", "ac_voltage_cn")),

    FieldSpec("ac_power", "AC Power", "int16", "W"),
    FieldSpec("ac_power_sf", "AC Power scale factor", "int16", hidden=True,
              scale_of=("ac_power",)),

    FieldSpec("ac_frequency", "AC Frequency", "uint16", "Hz"),
    FieldSpec("ac_frequency_sf", "AC Frequency scale factor", "int16", hidden=True,
              scale_of=("ac_frequency",)),

    FieldSpec("ac_va", "AC Apparent Power", "int16", "VA"),
    FieldSpec("ac_va_sf", "AC Apparent Power scale factor", "int16", hidden=True,
              scale_of=("ac_va",)),

    FieldSpec("ac_var", "AC Reactive Power", "int16", "VAR"),
    FieldSpec("ac_var_sf", "AC Reactive Power scale factor", "int16", hidden=True,
              scale_of=("ac_var",)),

    FieldSpec("ac_pf", "AC Power Factor", "int16", "%"),
    FieldSpec("ac_pf_sf", "AC Power Factor scale factor", "int16", hidden=True,
              scale_of=("ac_pf",)),

    FieldSpec("ac_energy_wh", "AC Lifetime Energy Production", "uint32", "Wh", length=2),
    FieldSpec("ac_energy_wh_sf", "AC Energy scale factor", "uint16", hidden=True,
              scale_of=("ac_energy_wh",)),

    FieldSpec("dc_current", "DC Current", "uint16", "A"),
    FieldSpec("dc_current_sf", "DC Current scale factor", "int16", hidden=True,
              scale_of=("dc_current",)),

    FieldSpec("dc_voltage", "DC Voltage", "uint16", "V"),
    FieldSpec("dc_voltage_sf", "DC Voltage scale factor", "int16", hidden=True,
              scale_of=("dc_voltage",)),

    FieldSpec("dc_power", "DC Power", "int16", "W"),
    FieldSpec("dc_power_sf", "DC Power scale factor", "int16", hidden=True,
              scale_of=("dc_power",)),

    FieldSpec("_reserved_1", "reserved", "pad", length=1, hidden=True),

    FieldSpec("temp_sink", "Heat Sink Temperature", "int16", "deg C"),

    FieldSpec("_reserved_2", "reserved", "pad", length=2, hidden=True),

    FieldSpec("temp_sink_sf", "Heat Sink Temperature scale factor", "int16", hidden=True,
              scale_of=("temp_sink",)),

    FieldSpec("status", "Operating State", "uint16"),
    FieldSpec("status_vendor", "Vendor Operating State / Error Code", "uint16"),

    # SunSpec pads the inverter model block out to a fixed 50 registers
    # regardless of how many trailing fields a given device implements.
    FieldSpec("_reserved_3", "reserved (model block padding)", "pad", length=12, hidden=True),
)

STATUS_NAMES = {
    1: "Off",
    2: "Sleeping (auto-shutdown) - Night mode",
    3: "Grid Monitoring/wake-up",
    4: "Inverter is ON and producing power",
    5: "Production (curtailed)",
    6: "Shutting down",
    7: "Fault",
    8: "Maintenance/setup",
}

INVERTER_DID_NAMES = {101: "single phase", 102: "split phase", 103: "three phase"}


def _field_register_count(f):
    if f.type == "string":
        return (f.length + 1) // 2
    if f.type == "uint32":
        return 2
    if f.type == "pad":
        return f.length
    return 1  # uint16 / int16


class _RegisterBuffer:
    """Sequential cursor over a flat list of 16-bit Modbus registers."""

    def __init__(self, registers):
        self._regs = registers
        self._pos = 0

    def skip(self, count):
        self._pos += count

    def next_uint16(self):
        val = self._regs[self._pos]
        self._pos += 1
        return val

    def next_int16(self):
        val = self.next_uint16()
        return val - 0x10000 if val > 0x7FFF else val

    def next_uint32(self):
        # SunSpec/Modbus 32-bit values are big-endian at the register level:
        # the first (lower-address) register holds the high word.
        hi = self.next_uint16()
        lo = self.next_uint16()
        return (hi << 16) | lo

    def next_string(self, char_count):
        # pyModbusTCP returns big-endian registers, so the first char of
        # each pair is the register's high byte.
        chars = []
        for _ in range((char_count + 1) // 2):
            reg = self.next_uint16()
            chars.append(chr((reg >> 8) & 0xFF))
            chars.append(chr(reg & 0xFF))
        return "".join(chars[:char_count]).strip(" \t\r\n\0")


def _decode(buf, fields):
    """Decode `fields` in order from `buf`, returning {key: raw_value}."""
    raw = {}
    for f in fields:
        if f.type == "pad":
            buf.skip(f.length)
        elif f.type == "uint16":
            raw[f.key] = buf.next_uint16()
        elif f.type == "int16":
            raw[f.key] = buf.next_int16()
        elif f.type == "uint32":
            raw[f.key] = buf.next_uint32()
        elif f.type == "string":
            raw[f.key] = buf.next_string(f.length)
        else:
            raise ValueError(f"FieldSpec {f.key!r}: unknown type {f.type!r}")
    return raw


def _build_values(fields, raw):
    """Turn decoded raw register values into the JSON-ready value list."""
    scale_field_for = {}
    for f in fields:
        for target_key in f.scale_of:
            scale_field_for[target_key] = f.key

    values = []
    for f in fields:
        if f.hidden or f.type == "pad":
            continue

        raw_val = raw[f.key]
        entry = {"name": f.key, "description": f.name, "units": f.units}

        if f.type == "string":
            entry["type"] = "string"
            entry["value"] = raw_val or None

        elif f.key in scale_field_for:
            sf_val = raw[scale_field_for[f.key]]
            not_implemented = raw_val in (U_NOT_IMPLEMENTED, S_NOT_IMPLEMENTED)
            entry["type"] = "float"
            entry["value"] = None if not_implemented else round(raw_val * (10.0 ** sf_val), 10)

        else:
            entry["type"] = "int"
            entry["value"] = None if raw_val == U_NOT_IMPLEMENTED else raw_val

        values.append(entry)

        if f.key == "status" and entry["value"] is not None:
            values.append({
                "name": "status_text",
                "description": "Operating State (decoded)",
                "units": None,
                "type": "string",
                "value": STATUS_NAMES.get(entry["value"], "unknown"),
            })

    return values


class HdWaveQuery:
    """Queries a SolarEdge HD-Wave inverter over Modbus TCP for SunSpec telemetry."""

    REG_BASE = 40000
    DEFAULT_PORT = 1502

    def __init__(self, host=None, port=DEFAULT_PORT):
        host = host or os.environ.get("INVERTER_HOST")
        if not host:
            raise ValueError("no inverter host given and INVERTER_HOST is not set")
        self.host = host
        self.port = port
        self._client = ModbusClient(host=host, port=port, auto_open=False, auto_close=False)

    @staticmethod
    def _total_register_count():
        return (sum(_field_register_count(f) for f in COMMON_BLOCK_FIELDS)
                + sum(_field_register_count(f) for f in INVERTER_MODEL_FIELDS))

    def read(self):
        """
        Perform the bulk register read and decode it.

        Returns a dict: {"host": ..., "phase": ..., "values": [...]}, where
        each entry in "values" has name/description/units/type/value.
        """
        total_regs = self._total_register_count()

        if not self._client.open():
            raise ConnectionError(f"unable to connect to inverter at {self.host}:{self.port}")
        try:
            regs = self._client.read_holding_registers(self.REG_BASE, total_regs)
        finally:
            self._client.close()

        if regs is None:
            raise IOError("modbus read_holding_registers returned no data")
        if len(regs) != total_regs:
            raise IOError(f"expected {total_regs} registers, got {len(regs)}")

        buf = _RegisterBuffer(regs)

        common_raw = _decode(buf, COMMON_BLOCK_FIELDS)
        if common_raw["sunspec_did"] != 1:
            raise ValueError(f"unexpected common block DID {common_raw['sunspec_did']} (expected 1)")

        model_raw = _decode(buf, INVERTER_MODEL_FIELDS)
        if model_raw["model_did"] not in INVERTER_DID_NAMES:
            raise ValueError(f"unexpected inverter model DID {model_raw['model_did']} "
                              f"(expected one of {sorted(INVERTER_DID_NAMES)})")

        values = _build_values(COMMON_BLOCK_FIELDS, common_raw)
        values += _build_values(INVERTER_MODEL_FIELDS, model_raw)

        return {
            "host": self.host,
            "phase": INVERTER_DID_NAMES[model_raw["model_did"]],
            "values": values,
        }

    def read_json(self, **json_kwargs):
        """Same as read(), but serialized to a JSON string."""
        return json.dumps(self.read(), **json_kwargs)


def _print_text(result):
    print(f"host:  {result['host']}")
    print(f"phase: {result['phase']}")
    for v in result["values"]:
        units = f" {v['units']}" if v["units"] else ""
        print(f"  {v['description']}: {v['value']}{units}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default=None,
                         help="inverter IP address (default: $INVERTER_HOST)")
    parser.add_argument("--port", type=int, default=HdWaveQuery.DEFAULT_PORT,
                         help="Modbus TCP port (default: %(default)s)")
    parser.add_argument("--json", action="store_true",
                         help="print result as a single JSON object instead of text")
    args = parser.parse_args()

    host = args.host or os.environ.get("INVERTER_HOST")
    if not host:
        parser.error("inverter host required: pass it as an argument or set $INVERTER_HOST")

    query = HdWaveQuery(host, args.port)

    if args.json:
        print(query.read_json(indent=2))
    else:
        _print_text(query.read())

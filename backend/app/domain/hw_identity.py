"""U-5d-0b Phase alpha — independent physical GPU identity evidence.

Pure evidence-correlation module: no IO, no OS calls, no subprocess.
All inputs are already-collected rows (WMI video-controller rows,
nvidia-smi query rows, Windows device-property rows); outputs are
internal identity records plus a separately versioned canonical
inventory digest.

Authority model (A1/A3):

- Physical identity TOKEN — derived solely from the full NVIDIA GPU
  UUID (driver authority). Never name/vendor/VRAM/driver/index/PCI
  location/timestamp.
- OS device-instance evidence — PNPDeviceID (identity of the OS device
  instance, not a decodable PCI address).
- PCI topology evidence — assembled ONLY from documented Windows
  device properties. Microsoft Learn "Obtaining PCI Segment Values"
  (windows-hardware/drivers/pci/obtaining-pci-segment-values) names
  DEVPKEY_Device_LocationInfo as the supported source for the PCI
  Segment value, with two documented string forms:
    multi-segment : "PCI segment <S> bus <B>, device <D>, function <F>"
    single-segment: "PCI bus <B>, device <D>, function <F>"  (S := 0)
  All four address components are taken from that documented string.
    segment   : DEVPKEY_Device_LocationInfo (documented segment source)
    bus       : DEVPKEY_Device_LocationInfo, cross-checked against
                DEVPKEY_Device_BusNumber (the device's bus number).
    dev+func  : DEVPKEY_Device_LocationInfo, cross-checked against
                DEVPKEY_Device_Address (Microsoft-documented PCI
                address encoding: (DeviceNumber << 16) | FunctionNumber)
                and the last PCI(ddff) hop of the location path.
  PCIROOT(n) in DEVPKEY_Device_LocationPaths is an ACPI root ordering,
  NOT a documented PCI segment value — LocationPaths is supporting
  topology evidence only (devfn cross-check + root-hop context); it can
  never establish a segment or a bus number. A missing component is
  reported missing — never fabricated.
- Vendor-specific identity — nvidia-smi pci.bus_id (PCI domain:bus:
  device.function) and uuid fields.
- Namespace association — the qualified join between OS topology and
  the vendor row (smi_index is a volatile ordinal, never identity).

Correlation requires a UNIQUE one-to-one BDF join plus a valid unique
UUID. Identical names, matching device counts, single-GPU degeneracy
and ordinal positions are never qualifying evidence (A2).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

HW_IDENTITY_VERSION = 1

CORR_QUALIFIED = "qualified"
CORR_AMBIGUOUS = "ambiguous"
CORR_UNAVAILABLE = "unavailable"
CORR_UNSUPPORTED = "unsupported"

_TOKEN_PREFIX = "hwid:nv:"
_DIGEST_PREFIX = "hwid:v1:"

# NVIDIA GPU UUID: 'GPU-' + canonical 8-4-4-4-12 hexadecimal form.
# Prefix-only or truncated UUIDs are never accepted.
_NVIDIA_UUID_RE = re.compile(
    r"^GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)

# nvidia-smi pci.bus_id: 8-hex PCI domain(segment) : 2-hex bus :
# 2-hex device . 1-hex function, e.g. "00000000:1B:00.0".
_SMI_BUS_ID_RE = re.compile(
    r"^([0-9A-Fa-f]{8}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})"
    r"\.([0-9A-Fa-f])$")

# DEVPKEY_Device_LocationInfo documented forms (Microsoft Learn
# "Obtaining PCI Segment Values"): multi-segment systems emit
# "PCI segment <S> bus <B>, device <D>, function <F>"; single-segment
# systems emit "PCI bus <B>, device <D>, function <F>" (segment := 0).
_LOCINFO_SEG_RE = re.compile(
    r"^PCI segment (\d+) bus (\d+), device (\d+), function (\d+)\s*$",
    re.IGNORECASE)
_LOCINFO_RE = re.compile(
    r"^PCI bus (\d+), device (\d+), function (\d+)\s*$", re.IGNORECASE)

# Documented location-path grammar: PCIROOT(n)#PCI(ddff)#PCI(ddff)...
# PCIROOT(n) is an ACPI root-bus ordering, NOT a PCI segment value;
# each PCI(ddff) hop encodes device (high byte) and function (low
# byte) of that bridge/device — supporting evidence only.
_PCIROOT_RE = re.compile(r"PCIROOT\((\d+)\)", re.IGNORECASE)
_PCI_HOP_RE = re.compile(r"PCI\(([0-9A-Fa-f]{4})\)")

_PCI_MAX_BUS = 0xFF
_PCI_MAX_DEVICE = 0x1F
_PCI_MAX_FUNCTION = 0x7
_PCI_MAX_SEGMENT = 0xFFFF


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def parse_nvidia_uuid(raw) -> str | None:
    """Normalize a full NVIDIA GPU UUID ('GPU-xxxxxxxx-...') or return
    None for malformed/empty/truncated/prefix/unsupported evidence."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Canonical form: 'GPU-' prefix + lowercase hex body.
    return ("GPU-" + s[4:].lower()) if _NVIDIA_UUID_RE.match(s) else None


def parse_smi_bus_id(raw) -> dict | None:
    """Parse nvidia-smi pci.bus_id 'DOM:BB:DD.F' into component dict;
    None when malformed. Ranges validated — no invented components."""
    if not isinstance(raw, str):
        return None
    m = _SMI_BUS_ID_RE.match(raw.strip())
    if not m:
        return None
    seg, bus, dev, fn = (int(m.group(i), 16) for i in (1, 2, 3, 4))
    if (bus > _PCI_MAX_BUS or dev > _PCI_MAX_DEVICE
            or fn > _PCI_MAX_FUNCTION or seg > _PCI_MAX_SEGMENT):
        return None
    return {"segment": seg, "bus": bus, "device": dev, "function": fn}


def parse_location_info(raw) -> dict:
    """Parse DEVPKEY_Device_LocationInfo per the documented contract:
    "PCI segment <S> bus <B>, device <D>, function <F>" on multi-segment
    systems, "PCI bus <B>, device <D>, function <F>" on single-segment
    systems (segment := 0). Unparseable or localized strings yield all
    None — the segment is then unproven, never assumed."""
    out = {"segment": None, "bus": None, "devfn": None, "form": None}
    if not isinstance(raw, str):
        return out
    s = raw.strip()
    m = _LOCINFO_SEG_RE.match(s)
    if m:
        seg, bus, dev, fn = (int(g) for g in m.groups())
        form = "multi"
    else:
        m = _LOCINFO_RE.match(s)
        if not m:
            return out
        seg, bus, dev, fn = 0, *(int(g) for g in m.groups())
        form = "single"
    if (bus > _PCI_MAX_BUS or dev > _PCI_MAX_DEVICE
            or fn > _PCI_MAX_FUNCTION or seg > _PCI_MAX_SEGMENT):
        return out
    return {"segment": seg, "bus": bus, "devfn": (dev, fn), "form": form}


def parse_location_paths(paths) -> dict:
    """Extract the ACPI root index (PCIROOT — NOT a PCI segment) and
    the device's own device/function (last PCI(ddff) hop) from
    DEVPKEY_Device_LocationPaths — supporting evidence only."""
    out = {"pciroot": None, "devfn": None}
    first = (paths or [None])[0]
    if not isinstance(first, str) or not first:
        return out
    m = _PCIROOT_RE.search(first)
    if m:
        root = int(m.group(1))
        if root <= _PCI_MAX_SEGMENT:
            out["pciroot"] = root
    hops = _PCI_HOP_RE.findall(first)
    if hops:
        ddff = int(hops[-1], 16)
        dev, fn = (ddff >> 8) & 0xFF, ddff & 0xFF
        if dev <= _PCI_MAX_DEVICE and fn <= _PCI_MAX_FUNCTION:
            out["devfn"] = (dev, fn)
    return out


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def pci_address_from_props(props: dict | None) -> dict:
    """Assemble the OS-side PCI address for one display device.
    Authoritative components come from DEVPKEY_Device_LocationInfo
    (documented segment/bus/device/function source); independently
    sourced DEVPKEY_Device_BusNumber, DEVPKEY_Device_Address and the
    LocationPaths devfn cross-check it — any contradiction is evidence
    conflict, never a favorable resolution. Returns {complete,
    conflict, segment, bus, device, function, sources}."""
    props = props or {}
    li = parse_location_info(props.get("location_info"))
    bus_dp = _as_int(props.get("bus_number"))
    addr = _as_int(props.get("address"))
    lp = parse_location_paths(props.get("location_paths") or [])

    devfn_dp = None
    if addr is not None:
        d, f = (addr >> 16) & 0xFFFF, addr & 0xFFFF
        if d <= _PCI_MAX_DEVICE and f <= _PCI_MAX_FUNCTION:
            devfn_dp = (d, f)

    conflict = False
    if (bus_dp is not None and li["bus"] is not None
            and bus_dp != li["bus"]):
        conflict = True  # independent bus sources disagree
    for alt in (devfn_dp, lp["devfn"]):
        if alt is not None and li["devfn"] is not None and alt != li["devfn"]:
            conflict = True  # independent devfn sources disagree
    devfn = li["devfn"]
    if devfn is None and (devfn_dp or lp["devfn"]):
        # LocationInfo gave no devfn — an independent source may supply
        # it only if the others agree; otherwise contradictory evidence.
        cand = {v for v in (devfn_dp, lp["devfn"]) if v is not None}
        if len(cand) == 1:
            devfn = cand.pop()
        elif cand:
            conflict = True

    bus = li["bus"]
    if bus is None and bus_dp is not None and 0 <= bus_dp <= _PCI_MAX_BUS:
        bus = bus_dp
    dev, fn = devfn if devfn else (None, None)
    complete = (bus is not None and dev is not None
                and li["segment"] is not None)
    return {
        "complete": complete and not conflict,
        "conflict": conflict,
        "segment": li["segment"],
        "bus": bus,
        "device": dev,
        "function": fn,
        "form": li["form"],
        "pciroot": lp["pciroot"],
        "sources": {
            "segment": "location_info" if li["segment"] is not None else None,
            "bus": ("location_info" if li["bus"] is not None
                    else "devpkey_bus_number" if bus is not None else None),
            "devfn": ("location_info" if li["devfn"] is not None
                      else "devpkey_address" if devfn_dp is not None
                      else "location_paths" if lp["devfn"] is not None
                      else None),
        },
    }


def _bdf_key(addr: dict) -> tuple:
    return (addr.get("segment"), addr.get("bus"),
            addr.get("device"), addr.get("function"))


def _record(index: int, vendor: str, pnp: str | None, correlation: str,
            reason: str) -> dict:
    return {
        "index": index, "vendor": vendor,
        "pnp_device_id": pnp or None, "pci": None,
        "gpu_uuid": None, "smi_index": None,
        "smi_vram_bytes": None, "smi_vram_free_bytes": None,
        "correlation": correlation, "reason": reason,
        "token": None,
    }


def identity_token(uuid_norm: str) -> str:
    """Pseudonymous physical-identity token for a qualified NVIDIA GPU
    UUID — namespaces the digest; the raw UUID stays internal evidence."""
    return (_TOKEN_PREFIX + hashlib.sha256(
        ("atomic.hwidentity.v1|nvidia|" + uuid_norm).encode()).hexdigest())


def correlate(meta: list[dict], smi_rows: list[dict],
              pci_props: dict) -> dict:
    """Build per-device identity records.

    meta — [{"index", "pnp", "vendor"}] in WMI enumeration order.
    smi_rows — parsed _nvidia_smi rows (may carry pci_bus_id/uuid).
    pci_props — {PNP_UPPER: {"bus_number", "address", "location_paths"}}.

    A qualified record requires: a complete, non-conflicting OS PCI
    address; a unique OS-side association; a unique vendor-row BDF join;
    and a valid UUID not duplicated across the vendor inventory.
    """
    parsed = []
    for r in smi_rows or []:
        parsed.append({**r,
                       "_bdf": parse_smi_bus_id(r.get("pci_bus_id")),
                       "_uuid": parse_nvidia_uuid(r.get("uuid"))})
    uuid_counts = Counter(p["_uuid"] for p in parsed if p["_uuid"])
    dup_uuids = {u for u, c in uuid_counts.items() if c > 1}
    bdf_counts = Counter(tuple(sorted(p["_bdf"].items()))
                         for p in parsed if p["_bdf"])
    dup_bdfs = {b for b, c in bdf_counts.items() if c > 1}

    addr_claims: dict[tuple, list[int]] = {}
    dev_addr = {}
    for m in meta or []:
        props = (pci_props or {}).get(m.get("pnp") or "")
        a = pci_address_from_props(props)
        dev_addr[m["index"]] = a
        if a["complete"] and not a["conflict"]:
            addr_claims.setdefault(
                (a["segment"], a["bus"], a["device"], a["function"]),
                []).append(m["index"])
    dup_addr = {k for k, v in addr_claims.items() if len(v) > 1}

    records = []
    for m in meta or []:
        rec = _record(m["index"], m.get("vendor") or "unknown",
                      m.get("pnp"), CORR_UNSUPPORTED, "unsupported_vendor")
        if rec["vendor"] != "NVIDIA":
            records.append(rec)
            continue
        a = dev_addr[m["index"]]
        rec["pci"] = a if a["complete"] and not a["conflict"] else None
        if a["conflict"]:
            rec.update(correlation=CORR_AMBIGUOUS,
                       reason="contradictory_topology_evidence")
        elif a["bus"] is None or a["device"] is None:
            rec.update(correlation=CORR_UNAVAILABLE,
                       reason="missing_pci_evidence")
        elif a["segment"] is None:
            rec.update(correlation=CORR_UNAVAILABLE,
                       reason="segment_unproven")
        elif _bdf_key(a) in dup_addr:
            rec.update(correlation=CORR_AMBIGUOUS,
                       reason="duplicate_pci_association")
        else:
            cand = [p for p in parsed
                    if p["_bdf"] and _bdf_key(p["_bdf"]) == _bdf_key(a)]
            if len(cand) > 1 or (cand and tuple(sorted(
                    cand[0]["_bdf"].items())) in dup_bdfs):
                rec.update(correlation=CORR_AMBIGUOUS,
                           reason="duplicate_vendor_row")
            elif not cand:
                rec.update(correlation=CORR_UNAVAILABLE,
                           reason="no_matching_vendor_row")
            elif cand[0]["_uuid"] is None:
                rec.update(correlation=CORR_UNAVAILABLE,
                           reason="invalid_uuid_evidence")
            elif cand[0]["_uuid"] in dup_uuids:
                rec.update(correlation=CORR_AMBIGUOUS,
                           reason="duplicate_uuid_evidence")
            else:
                p = cand[0]
                # Contradictory memory evidence (free > total) is
                # suppressed — the identity join stays qualified, but
                # impossible measurements are never attributed.
                mem_conflict = (
                    p.get("vram_bytes") is not None
                    and p.get("vram_free_bytes") is not None
                    and p["vram_free_bytes"] > p["vram_bytes"])
                rec.update(
                    correlation=CORR_QUALIFIED,
                    reason=("bdf_uuid_join|memory_conflict"
                            if mem_conflict else "bdf_uuid_join"),
                    gpu_uuid=p["_uuid"], smi_index=p.get("smi_index"),
                    smi_vram_bytes=p.get("vram_bytes"),
                    smi_vram_free_bytes=(None if mem_conflict
                                         else p.get("vram_free_bytes")),
                    token=identity_token(p["_uuid"]))
        records.append(rec)

    counts = Counter(r["correlation"] for r in records)
    completeness = {
        "wmi_devices": len(meta or []),
        "nvidia_devices": sum(1 for m in (meta or [])
                              if m.get("vendor") == "NVIDIA"),
        "smi_rows": len(smi_rows or []),
        "smi_rows_valid": sum(1 for p in parsed
                              if p["_bdf"] is not None),
        "qualified": counts.get(CORR_QUALIFIED, 0),
        "ambiguous": counts.get(CORR_AMBIGUOUS, 0),
        "unavailable": counts.get(CORR_UNAVAILABLE, 0),
        "unsupported": counts.get(CORR_UNSUPPORTED, 0),
    }
    return {"records": records, "completeness": completeness}


def canonical_inventory(records: list[dict], provenance: str) -> dict:
    """Separately versioned canonical identity inventory.

    Canonical field inventory (v1): pnp_device_id (OS device-instance
    evidence), vendor, pci [segment,bus,device,function] (PCI topology
    evidence), uuid (vendor identity evidence), correlation (join
    qualification), os_index and smi_index (CURRENT OS/scheduler and
    vendor namespace associations). Namespace associations are mutable
    but material — their drift is digest-detectable. Never included:
    name, VRAM (total or free), driver version, timestamps — volatile
    resource and descriptive evidence is not material identity."""
    devices = []
    for r in records or []:
        pci = r.get("pci")
        devices.append({
            "pnp_device_id": r.get("pnp_device_id"),
            "vendor": r.get("vendor"),
            "pci": ([pci.get("segment"), pci.get("bus"),
                     pci.get("device"), pci.get("function")]
                    if pci else None),
            "uuid": r.get("gpu_uuid"),
            "correlation": r.get("correlation"),
            "os_index": r.get("index"),
            "smi_index": r.get("smi_index"),
        })
    devices.sort(key=lambda d: (
        d["pnp_device_id"] or "", d["uuid"] or "",
        d["vendor"] or "", _canon(d["pci"]),
        -1 if d["os_index"] is None else d["os_index"],
        -1 if d["smi_index"] is None else d["smi_index"]))
    return {"v": HW_IDENTITY_VERSION, "provenance": provenance,
            "devices": devices}


def inventory_digest(records: list[dict], provenance: str) -> str:
    """Full SHA-256 over the canonical inventory — an evidence-
    consistency mechanism, not proof that inputs are authoritative."""
    return (_DIGEST_PREFIX + hashlib.sha256(
        _canon(canonical_inventory(records, provenance)).encode())
        .hexdigest())


def finalize(ident: dict | None, provenance: str) -> dict:
    """Attach version, provenance and the independent inventory digest
    to a correlate() result. Identical material evidence yields the
    same digest across collections and orderings."""
    records = (ident or {}).get("records") or []
    completeness = (ident or {}).get("completeness") or {}
    return {
        "version": HW_IDENTITY_VERSION,
        "provenance": provenance,
        "records": records,
        "completeness": completeness,
        "digest": inventory_digest(records, provenance),
    }


def simulated_payload(gpus: list[dict]) -> dict:
    """Identity evidence for a simulated inventory — every device is
    honestly unqualified (no real-source evidence exists); provenance
    makes simulated evidence impossible to confuse with real."""
    meta = [{"index": g.get("index", i), "pnp": None,
             "vendor": g.get("vendor") or "unknown"}
            for i, g in enumerate(gpus or [])]
    for m in meta:
        if m["vendor"] == "NVIDIA":
            m["vendor"] = "unknown"  # name-claimed vendor is not evidence
    ident = correlate(meta, [], {})
    return finalize(ident, provenance="simulated")


def unavailable_payload() -> dict:
    """No paired collection produced identity evidence (e.g. an injected
    or stubbed collection path) — recorded, never fabricated."""
    return {"version": HW_IDENTITY_VERSION, "provenance": "unavailable",
            "records": [], "completeness": {}, "digest": None,
            "collection_state": "unpaired"}


def public_view(rec: dict) -> dict:
    """Non-sensitive per-device view — pseudonymous token and class
    only; raw UUID/PNP/PCI evidence never leaves the internal record."""
    return {"token": rec.get("token"),
            "correlation": rec.get("correlation"),
            "vendor": rec.get("vendor")}


__all__ = [
    "CORR_AMBIGUOUS", "CORR_QUALIFIED", "CORR_UNAVAILABLE",
    "CORR_UNSUPPORTED", "HW_IDENTITY_VERSION", "canonical_inventory",
    "correlate", "finalize", "identity_token", "inventory_digest",
    "parse_location_paths", "parse_nvidia_uuid", "parse_smi_bus_id",
    "pci_address_from_props", "public_view", "simulated_payload",
    "unavailable_payload",
]

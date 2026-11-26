"""U-5d-0b Phase alpha — synthetic identity-evidence tests.

Every test is synthetic: external execution boundaries
(_ps_json / _nvidia_smi / subprocess.run) are intercepted; no live
hardware query, NVIDIA-SMI run, Windows property query, model read or
inference occurs here.
"""

from __future__ import annotations

import copy
import subprocess

import pytest

from app.domain import hardware
from app.domain import hw_identity as hi
from app.domain import hw_snapshot as hs

GIB = 1024 ** 3

PNP_A = ("PCI\\VEN_10DE&DEV_2484&SUBSYS_37003842&REV_A1"
         "\\4&1111AAAA&0&0008")
PNP_B = ("PCI\\VEN_10DE&DEV_2484&SUBSYS_37003842&REV_A1"
         "\\4&2222BBBB&0&0009")
PNP_AMD = ("PCI\\VEN_1002&DEV_744C&SUBSYS_11112222&REV_C1"
           "\\4&3333CCCC&0&0010")
PNP_INTEL = ("PCI\\VEN_8086&DEV_4680&SUBSYS_00001111&REV_0C"
             "\\4&4444DDDD&0&0011")
UUID_A = "GPU-12345678-1234-1234-1234-123456789abc"
UUID_B = "GPU-87654321-4321-4321-4321-cba987654321"


def wmi_row(name="NVIDIA GeForce RTX 3060", pnp=PNP_A,
            adapter_ram=4 * GIB - 1, driver="555.99"):
    return {"Name": name, "AdapterRAM": adapter_ram,
            "DriverVersion": driver, "PNPDeviceID": pnp}


def smi_row(index=0, name="NVIDIA GeForce RTX 3060", total=12 * GIB,
            free=11 * GIB, driver="555.99", bus_id="00000000:01:00.0",
            uuid=UUID_A):
    return {"smi_index": index, "name": name, "vram_bytes": total,
            "vram_free_bytes": free, "driver_version": driver,
            "pci_bus_id": bus_id, "uuid": uuid}


_AUTO = "auto"


def pci_props(bus=1, device=0, function=0, segment=None, address=None,
              locpath="PCIROOT(0)#PCI(1B00)#PCI(0000)", pnp=PNP_A,
              location_info=_AUTO):
    """Synthetic device-property evidence. ``location_info`` is the
    authoritative DEVPKEY_Device_LocationInfo value — 'auto' builds the
    documented string from the other components; None means the
    property was absent (segment then unproven)."""
    if address is None:
        address = (device << 16) | function
    if location_info == _AUTO:
        if segment is None:
            location_info = (f"PCI bus {bus}, device {device}, "
                             f"function {function}")
        else:
            location_info = (f"PCI segment {segment} bus {bus}, "
                             f"device {device}, function {function}")
    return {pnp: {"bus_number": bus, "address": address,
                  "location_info": location_info,
                  "location_paths": [locpath] if locpath else []}}


def meta(pnp=PNP_A, vendor="NVIDIA", index=0):
    return {"index": index, "pnp": pnp, "vendor": vendor}


# ------------------------------------------------------------- parsers


def test_uuid_valid_normalized():
    assert hi.parse_nvidia_uuid(UUID_A) == UUID_A
    assert hi.parse_nvidia_uuid(UUID_A.upper()) == UUID_A


@pytest.mark.parametrize("bad", [
    "", None, 42, "GPU-", "GPU-12345678",                      # prefix/short
    "GPU-12345678-1234-1234-1234-123456789ab",                 # truncated
    "GPU-12345678-1234-1234-1234-123456789abcde",              # overlong
    "GPU-12345678-1234-1234-1234-123456789abg",                # non-hex
    "12345678-1234-1234-1234-123456789abc",                    # no GPU-
    "MIG-12345678-1234-1234-1234-123456789abc",                # unsupported
    "gpu-12345678-1234-1234-1234-123456789abc1",
])
def test_uuid_rejects_malformed(bad):
    assert hi.parse_nvidia_uuid(bad) is None


def test_smi_bus_id_valid():
    assert hi.parse_smi_bus_id("00000000:1B:00.0") == {
        "segment": 0, "bus": 0x1B, "device": 0, "function": 0}
    assert hi.parse_smi_bus_id("00000001:ff:1f.7") == {
        "segment": 1, "bus": 0xFF, "device": 0x1F, "function": 7}


@pytest.mark.parametrize("bad", [
    "", None, "1:00.0", "00000000:1B:00", "00000000:1B:00.00",
    "00000000:1B:20.0",        # device > 0x1F
    "00000000:100:00.0",       # bus > 0xFF
    "00000000:1B:00.8",        # function > 7
    "00000000:1B:00.0 extra", "xyz",
])
def test_smi_bus_id_rejects_malformed(bad):
    assert hi.parse_smi_bus_id(bad) is None


def test_location_info_parses_documented_forms():
    m = hi.parse_location_info("PCI segment 3 bus 9, device 2, "
                             "function 1")
    assert m == {"segment": 3, "bus": 9, "devfn": (2, 1), "form": "multi"}
    s = hi.parse_location_info("PCI bus 1, device 0, function 0")
    assert s == {"segment": 0, "bus": 1, "devfn": (0, 0),
                 "form": "single"}


def test_location_info_rejects_undocumented_forms():
    for bad in (None, 42, "", "PCIe Controller",
                "PCI bus 1 device 0 function 0",
                "PCI segment x bus 1, device 0, function 0",
                "PCI bus 256, device 0, function 0",   # bus out of range
                "PCI bus 1, device 32, function 0",    # dev out of range
                "PCI segment 99999 bus 1, device 0, function 0"):
        assert hi.parse_location_info(bad) == {
            "segment": None, "bus": None, "devfn": None, "form": None}


def test_location_paths_pciroot_is_not_segment():
    # C1 — PCIROOT(n) is an ACPI root ordering, never segment evidence.
    out = hi.parse_location_paths(["PCIROOT(3)#PCI(1B00)#PCI(0200)"])
    assert out == {"pciroot": 3, "devfn": (2, 0)}
    assert hi.parse_location_paths([]) == {"pciroot": None, "devfn": None}
    assert hi.parse_location_paths(["junk"]) == {
        "pciroot": None, "devfn": None}
    assert hi.parse_location_paths(
        ["PCIROOT(0)#PCI(0300)#PCI(0200)#PCI(0701)"])["devfn"] == (7, 1)


def test_pci_address_requires_bus_component():
    # Neither LocationPaths nor devpkey Address alone can complete a
    # BDF (req 91): no authoritative bus.
    a = hi.pci_address_from_props({"bus_number": None, "address": 0,
                                   "location_paths":
                                   ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]})
    assert a["complete"] is False and a["bus"] is None
    assert a["device"] == 0


def test_pci_address_missing_segment_not_fabricated():
    # Bus+devfn present via independent sources but LocationInfo absent
    # → segment remains unproven, never assumed zero (req 92, C1).
    a = hi.pci_address_from_props({"bus_number": 1, "address": 0,
                                   "location_info": None,
                                   "location_paths":
                                   ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]})
    assert a["bus"] == 1 and a["device"] == 0
    assert a["segment"] is None and a["complete"] is False


def test_pci_address_conflict_devfn_sources():
    # DEVPKEY_Address devfn (1,0) contradicts LocationInfo devfn (0,0)
    # — independent-source disagreement is evidence conflict.
    a = hi.pci_address_from_props({
        "bus_number": 1, "address": (1 << 16),
        "location_info": "PCI bus 1, device 0, function 0",
        "location_paths": ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]})
    assert a["conflict"] is True and a["complete"] is False
    # LocationPaths devfn contradicts LocationInfo → also conflict.
    b = hi.pci_address_from_props({
        "bus_number": 1, "address": 0,
        "location_info": "PCI bus 1, device 0, function 0",
        "location_paths": ["PCIROOT(0)#PCI(1B00)#PCI(0100)"]})
    assert b["conflict"] is True


def test_pci_address_conflict_bus_sources():
    a = hi.pci_address_from_props({
        "bus_number": 5, "address": 0,
        "location_info": "PCI bus 1, device 0, function 0",
        "location_paths": ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]})
    assert a["conflict"] is True and a["complete"] is False


def test_pci_address_string_coercion_and_bool_rejection():
    a = hi.pci_address_from_props({
        "bus_number": "1", "address": "0",
        "location_info": "PCI bus 1, device 0, function 0",
        "location_paths": []})
    assert a["bus"] == 1 and a["device"] == 0 and a["complete"] is True
    b = hi.pci_address_from_props({"bus_number": True, "address": 0,
                                   "location_info":
                                   "PCI bus 1, device 0, function 0",
                                   "location_paths": []})
    # bool bus rejected as a devpkey source, but LocationInfo bus stands
    assert b["bus"] == 1 and b["complete"] is True


# --------------------------------------------------------- correlation


def test_qualified_single_gpu_join():
    r = hi.correlate([meta()], [smi_row()], pci_props())
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_QUALIFIED
    assert rec["token"].startswith("hwid:nv:")
    assert rec["smi_index"] == 0
    assert rec["gpu_uuid"] == UUID_A
    assert rec["smi_vram_free_bytes"] == 11 * GIB
    assert r["completeness"]["qualified"] == 1


def test_single_device_counts_do_not_qualify():
    # One WMI row + one SMI row + no topology evidence = unavailable
    # (req 93/66 — matching counts are never a join).
    r = hi.correlate([meta()], [smi_row()], {})
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_UNAVAILABLE
    assert rec["smi_vram_free_bytes"] is None and rec["token"] is None


def test_no_smi_row_stays_unavailable():
    r = hi.correlate([meta()], [], pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_UNAVAILABLE
    assert r["records"][0]["reason"] == "no_matching_vendor_row"


def test_missing_segment_blocks_qualification():
    # PCIROOT evidence exists, but LocationInfo (the authoritative
    # segment source) is absent → segment unproven → unavailable.
    props = pci_props(location_info=None)
    r = hi.correlate([meta()], [smi_row()], props)
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_UNAVAILABLE
    assert rec["reason"] == "segment_unproven"
    assert rec["token"] is None and rec["smi_vram_free_bytes"] is None


def test_pciroot_present_locationinfo_absent_never_qualifies():
    # C1 — PCIROOT(0) cannot substitute for segment authority even
    # with a single matching SMI row.
    props = pci_props(location_info=None,
                      locpath="PCIROOT(0)#PCI(1B00)#PCI(0000)")
    r = hi.correlate([meta()], [smi_row()], props)
    assert r["records"][0]["correlation"] == hi.CORR_UNAVAILABLE
    assert r["records"][0]["token"] is None


def test_pciroot_not_segment_root_index_differs():
    # C1 — PCIROOT(3) with documented segment 0: the root index is not
    # the segment; LocationInfo authority still qualifies.
    props = pci_props(locpath="PCIROOT(3)#PCI(1B00)#PCI(0000)")
    r = hi.correlate([meta()], [smi_row()], props)
    assert r["records"][0]["correlation"] == hi.CORR_QUALIFIED


def test_multiple_roots_one_segment_qualify():
    # C1 — two roots within segment 0; each device qualifies on its
    # own authoritative address.
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    props = {
        **pci_props(locpath="PCIROOT(0)#PCI(1B00)#PCI(0000)"),
        **pci_props(pnp=PNP_B, bus=9,
                    locpath="PCIROOT(1)#PCI(1B00)#PCI(0000)"),
    }
    smi2 = [smi_row(), smi_row(index=1, bus_id="00000000:09:00.0",
                               uuid=UUID_B, free=5 * GIB)]
    r = hi.correlate(meta2, smi2, props)
    assert all(rec["correlation"] == hi.CORR_QUALIFIED
               for rec in r["records"])
    assert r["records"][1]["smi_vram_free_bytes"] == 5 * GIB


def test_same_bus_devfn_across_segments():
    # C1 — identical bus/dev/fn in two segment groups; only the
    # segment-matched vendor row joins.
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    props = {
        **pci_props(),                                    # seg0 bus1
        **pci_props(pnp=PNP_B, segment=1,
                    locpath="PCIROOT(1)#PCI(1B00)#PCI(0000)"),
    }                                                     # seg1 bus1
    smi2 = [smi_row(bus_id="00000001:01:00.0", uuid=UUID_B,
                    index=1, free=5 * GIB),
            smi_row()]                                    # seg0 bus1
    r = hi.correlate(meta2, smi2, props)
    a, b = r["records"]
    assert a["correlation"] == hi.CORR_QUALIFIED
    assert a["gpu_uuid"] == UUID_A
    assert b["correlation"] == hi.CORR_QUALIFIED
    assert b["gpu_uuid"] == UUID_B
    assert b["smi_vram_free_bytes"] == 5 * GIB


def test_unparseable_location_info_segment_unproven():
    props = pci_props(location_info="PCIe Controller")
    r = hi.correlate([meta()], [smi_row()], props)
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_UNAVAILABLE
    assert rec["reason"] == "segment_unproven"


def test_identical_gpus_each_get_own_row():
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    smi2 = [smi_row(bus_id="00000000:09:00.0", uuid=UUID_B, index=1,
                    free=5 * GIB),
            smi_row(index=0)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    r = hi.correlate(meta2, smi2, props)
    a, b = r["records"]
    assert a["correlation"] == b["correlation"] == hi.CORR_QUALIFIED
    assert a["token"] != b["token"]
    assert a["smi_vram_free_bytes"] == 11 * GIB
    assert b["smi_vram_free_bytes"] == 5 * GIB  # no aliasing (req 58)


def test_duplicate_uuid_disqualifies_both():
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    smi2 = [smi_row(), smi_row(index=1, bus_id="00000000:09:00.0")]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    r = hi.correlate(meta2, smi2, props)
    assert all(rec["correlation"] == hi.CORR_AMBIGUOUS
               and rec["reason"] == "duplicate_uuid_evidence"
               for rec in r["records"])


def test_duplicate_pci_association_ambiguous():
    # Two OS devices resolving to one address — neither may claim it.
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    props = {**pci_props(), **pci_props(pnp=PNP_B)}   # same address
    r = hi.correlate(meta2, [smi_row()], props)
    assert all(rec["correlation"] == hi.CORR_AMBIGUOUS
               and rec["reason"] == "duplicate_pci_association"
               for rec in r["records"])


def test_duplicate_smi_rows_ambiguous():
    r = hi.correlate([meta()], [smi_row(), smi_row(index=1)],
                     pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_AMBIGUOUS
    assert r["records"][0]["reason"] == "duplicate_vendor_row"


def test_ambiguous_join_gives_no_memory():
    r = hi.correlate([meta()], [smi_row(), smi_row(index=1)],
                     pci_props())
    rec = r["records"][0]
    assert rec["smi_vram_free_bytes"] is None
    assert rec["smi_vram_bytes"] is None and rec["token"] is None


def test_partial_smi_inventory_honest():
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    r = hi.correlate(meta2, [smi_row()], props)     # only one smi row
    a, b = r["records"]
    assert a["correlation"] == hi.CORR_QUALIFIED
    assert b["correlation"] == hi.CORR_UNAVAILABLE
    assert r["completeness"]["nvidia_devices"] == 2
    assert r["completeness"]["qualified"] == 1


def test_measured_zero_free_preserved():
    r = hi.correlate([meta()], [smi_row(free=0)], pci_props())
    assert r["records"][0]["smi_vram_free_bytes"] == 0   # req 104


def test_mixed_vendor_unsupported():
    metas = [meta(index=0),
             meta(pnp=PNP_AMD, vendor="AMD", index=1),
             meta(pnp=PNP_INTEL, vendor="Intel", index=2)]
    r = hi.correlate(metas, [smi_row()], pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_QUALIFIED
    assert r["records"][1]["correlation"] == hi.CORR_UNSUPPORTED
    assert r["records"][2]["correlation"] == hi.CORR_UNSUPPORTED
    assert r["records"][1]["token"] is None
    assert r["records"][2]["token"] is None


def test_invalid_uuid_not_attributed():
    r = hi.correlate([meta()], [smi_row(uuid="GPU-1234")], pci_props())
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_UNAVAILABLE
    assert rec["reason"] == "invalid_uuid_evidence"
    assert rec["smi_vram_free_bytes"] is None


def test_malformed_smi_bdf_row_not_matched():
    r = hi.correlate([meta()], [smi_row(bus_id="garbage")],
                     pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_UNAVAILABLE


def test_conflict_topology_blocks_join():
    props = {PNP_A: {"bus_number": 1, "address": (1 << 16),
                     "location_paths": ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]}}
    r = hi.correlate([meta()], [smi_row()], props)
    assert r["records"][0]["correlation"] == hi.CORR_AMBIGUOUS
    assert r["records"][0]["reason"] == "contradictory_topology_evidence"


# --------------------------------------------------- identity/digest


def test_replacement_gpu_new_identity():
    # Same slot, same PNP instance, same name/VRAM — different UUID.
    r1 = hi.correlate([meta()], [smi_row()], pci_props())
    r2 = hi.correlate([meta()], [smi_row(uuid=UUID_B)], pci_props())
    t1, t2 = (r["records"][0]["token"] for r in (r1, r2))
    assert t1 is not None and t2 is not None and t1 != t2  # req 94


def test_identity_survives_relocation_and_index_change():
    r1 = hi.correlate([meta()], [smi_row()], pci_props())
    meta2 = [meta(index=7)]
    props2 = pci_props(bus=5, locpath="PCIROOT(0)#PCI(1700)#PCI(0000)")
    r2 = hi.correlate(meta2, [smi_row(bus_id="00000000:05:00.0")],
                      props2)
    assert r2["records"][0]["token"] == r1["records"][0]["token"]  # 95/96


def test_digest_order_independent():
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    smi2 = [smi_row(), smi_row(index=1, bus_id="00000000:09:00.0",
                               uuid=UUID_B)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    r1 = hi.correlate(meta2, smi2, props)
    r2 = hi.correlate(list(reversed(meta2)), list(reversed(smi2)),
                      props)
    # Same material evidence, input order reversed → identical digest.
    d1 = hi.inventory_digest(r1["records"], "real")
    d2 = hi.inventory_digest(r2["records"], "real")
    assert d1.startswith("hwid:v1:") and len(d1) == len("hwid:v1:") + 64
    assert d2 == d1


def test_digest_detects_namespace_association_drift():
    # C2 — os_index/smi_index are namespace-association evidence:
    # material to the digest while never affecting the identity token.
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    smi2 = [smi_row(), smi_row(index=1, bus_id="00000000:09:00.0",
                               uuid=UUID_B)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    r1 = hi.correlate(meta2, smi2, props)
    d1 = hi.inventory_digest(r1["records"], "real")
    t1 = [rec["token"] for rec in r1["records"]]
    # Same devices, indices swapped → association change is material.
    meta3 = [meta(index=1), meta(pnp=PNP_B, index=0)]
    r3 = hi.correlate(meta3, smi2, props)
    assert hi.inventory_digest(r3["records"], "real") != d1
    assert [rec["token"] for rec in r3["records"]] == t1
    # Same device, different SMI ordinal → association drift detected.
    r4 = hi.correlate(meta2,
                      [smi_row(index=4),
                       smi_row(index=1, bus_id="00000000:09:00.0",
                               uuid=UUID_B)],
                      props)
    assert hi.inventory_digest(r4["records"], "real") != d1
    assert r4["records"][0]["token"] == t1[0]


def test_digest_changes_on_material_identity_drift():
    r1 = hi.correlate([meta()], [smi_row()], pci_props())
    r2 = hi.correlate([meta()], [smi_row(uuid=UUID_B)], pci_props())
    assert (hi.inventory_digest(r1["records"], "real")
            != hi.inventory_digest(r2["records"], "real"))     # req 56


def test_digest_ignores_volatile_memory_and_timestamps():
    r1 = hi.correlate([meta()], [smi_row(free=3 * GIB)], pci_props())
    r2 = hi.correlate([meta()], [smi_row(free=9 * GIB)], pci_props())
    assert (hi.inventory_digest(r1["records"], "real")
            == hi.inventory_digest(r2["records"], "real"))     # req 57
    canon = hi.canonical_inventory(r1["records"], "real")
    assert not any("time" in k or "mono" in k
                   for d in canon["devices"] for k in d)         # req 97


def test_simulated_provenance_distinguishes_digest():
    r = hi.correlate([meta()], [smi_row()], pci_props())
    assert (hi.inventory_digest(r["records"], "real")
            != hi.inventory_digest(r["records"], "simulated"))  # req 110


def test_same_hwv2_fingerprint_different_identity():
    # Same index/name/vendor/VRAM → identical hw:v2; different PNP/UUID
    # → different identity digests (req 55, D1 demonstration).
    def report(pnp):
        g = {"index": 0, "name": "RTX", "vendor": "NVIDIA",
             "vram_bytes": 8 * GIB, "vram_source": "registry",
             "vram_free_bytes": 6 * GIB, "vram_capped_at_4gb": False,
             "driver_version": "555.0"}
        return {"gpus": [g], "backends": {}, "cpu": {}, "memory": {},
                "probed_at": 1.0, "probed_monotonic": 1.0}
    s1 = hs.build_snapshot(report(PNP_A), None, {})
    s2 = hs.build_snapshot(report(PNP_B), None, {})
    assert s1["snapshot_id"] == s2["snapshot_id"]
    r1 = hi.correlate([meta()], [smi_row()], pci_props())
    r2 = hi.correlate([meta(pnp=PNP_B)], [smi_row(uuid=UUID_B)],
                      pci_props(pnp=PNP_B))
    assert (hi.inventory_digest(r1["records"], "real")
            != hi.inventory_digest(r2["records"], "real"))


# ------------------------------------------------- _query_gpus seams


def _devinfo(props=None, vram_map=None):
    return {"vram_by_device": vram_map or {},
            "vulkan_icd_count": 0, "vulkan_dll": False,
            "pci_by_pnp": props or {}}


def _patch_collectors(monkeypatch, wmi_rows=None, smi_rows=None):
    monkeypatch.setattr(
        hardware, "_ps_json",
        lambda script, timeout=15: (wmi_rows if script is
                                    hardware._PS_GPU_QUERY else None))
    monkeypatch.setattr(hardware, "_nvidia_smi", lambda: smi_rows or [])


def test_query_gpus_qualified_attribution(monkeypatch):
    _patch_collectors(monkeypatch, [wmi_row()], [smi_row()])
    gpus = hardware._query_gpus(_devinfo(pci_props()))
    assert set(gpus[0]) == {
        "index", "name", "vendor", "vram_bytes", "vram_source",
        "vram_free_bytes", "vram_capped_at_4gb", "driver_version",
        "role", "inference_capable"}                    # contract exact
    assert gpus[0]["vram_free_bytes"] == 11 * GIB
    assert gpus[0]["vram_bytes"] == 12 * GIB
    assert gpus[0]["vram_source"] == "nvidia-smi"


def test_query_gpus_identity_not_on_public_rows(monkeypatch):
    _patch_collectors(monkeypatch, [wmi_row()], [smi_row()])
    devinfo = _devinfo(pci_props())
    gpus = hardware._query_gpus(devinfo)
    assert "_identity" in devinfo                      # out-channel
    assert all("_identity" not in g and "uuid" not in g
               and "pnp" not in g for g in gpus)      # req 114


def test_query_gpus_no_name_alias(monkeypatch):
    # Two same-name NVIDIAs; SMI rows in reverse order — only the BDF
    # match is attributed; name equality never joins (req 58/A2).
    rows = [wmi_row(), wmi_row(pnp=PNP_B)]
    smi2 = [smi_row(index=1, bus_id="00000000:09:00.0", uuid=UUID_B,
                    free=5 * GIB),
            smi_row(index=0)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    _patch_collectors(monkeypatch, rows, smi2)
    gpus = hardware._query_gpus(_devinfo(props))
    assert gpus[0]["vram_free_bytes"] == 11 * GIB
    assert gpus[1]["vram_free_bytes"] == 5 * GIB


def test_query_gpus_unqualified_free_is_none(monkeypatch):
    _patch_collectors(monkeypatch,
                      [wmi_row(adapter_ram=2 * GIB)], [smi_row()])
    gpus = hardware._query_gpus(_devinfo({}))          # no pci props
    assert gpus[0]["vram_free_bytes"] is None
    assert gpus[0]["vram_source"] == "wmi"


def test_query_gpus_smi_total_needs_join(monkeypatch):
    # Unqualified: SMI total may NOT overwrite WMI/registry (req 103).
    _patch_collectors(monkeypatch, [wmi_row(adapter_ram=None)],
                      [smi_row()])
    gpus = hardware._query_gpus(_devinfo({}))
    assert gpus[0]["vram_bytes"] is None
    assert gpus[0]["vram_source"] == "wmi"


def test_query_gpus_registry_still_preferred(monkeypatch):
    _patch_collectors(monkeypatch, [wmi_row()], [smi_row()])
    props = pci_props()
    gpus = hardware._query_gpus(_devinfo(props, vram_map={
        "PCI\\VEN_10DE&DEV_2484": 10 * GIB}))
    # smi total (12G) > registry (10G) → qualified upgrade allowed
    assert gpus[0]["vram_bytes"] == 12 * GIB
    assert gpus[0]["vram_source"] == "nvidia-smi"


def test_query_gpus_measured_zero_published(monkeypatch):
    _patch_collectors(monkeypatch, [wmi_row()], [smi_row(free=0)])
    gpus = hardware._query_gpus(_devinfo(pci_props()))
    assert gpus[0]["vram_free_bytes"] == 0             # req 60/104


def test_query_gpus_wmi_failure_empty(monkeypatch):
    _patch_collectors(monkeypatch, None, [smi_row()])
    assert hardware._query_gpus(_devinfo()) == []


# ---------------------------------------------------- _nvidia_smi


def _patch_smi(monkeypatch, stdout="0, GPU0, 12288, 11264, 555.99, "
              "00000000:01:00.0, GPU-12345678-1234-1234-1234-"
              "123456789abc\n", rc=0):
    monkeypatch.setattr(hardware.shutil, "which", lambda x: "smi")
    monkeypatch.setattr(
        hardware.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], rc, stdout=stdout, stderr=""))


def test_smi_extended_fields_parsed(monkeypatch):
    _patch_smi(monkeypatch)
    rows = hardware._nvidia_smi()
    assert rows[0]["pci_bus_id"] == "00000000:01:00.0"
    assert rows[0]["uuid"].startswith("GPU-")
    assert rows[0]["vram_bytes"] == 12288 * 1024 * 1024


def test_smi_short_row_fields_none(monkeypatch):
    _patch_smi(monkeypatch, stdout="0, GPU0, 12288, 11264, 555.99\n")
    row = hardware._nvidia_smi()[0]
    assert row["pci_bus_id"] is None and row["uuid"] is None


def test_smi_malformed_line_skipped(monkeypatch):
    _patch_smi(monkeypatch, stdout="x, bad\n0, GPU0, 12288, 11264, "
              "555.99, 00000000:01:00.0, " + UUID_A + "\n")
    rows = hardware._nvidia_smi()
    assert len(rows) == 1 and rows[0]["smi_index"] == 0


def test_smi_nonzero_exit(monkeypatch):
    _patch_smi(monkeypatch, rc=1)
    assert hardware._nvidia_smi() == []                # req 107


def test_smi_timeout(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda x: "smi")

    def _to(*a, **k):
        raise subprocess.TimeoutExpired("smi", 15)
    monkeypatch.setattr(hardware.subprocess, "run", _to)
    assert hardware._nvidia_smi() == []                # req 106


def test_smi_absent_binary(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda x: None)
    assert hardware._nvidia_smi() == []


# ----------------------------------------------- _pci_properties


def test_pci_props_non_windows_no_call(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")

    def _boom(*a, **k):
        raise AssertionError("Windows collector invoked off-Windows")
    monkeypatch.setattr(hardware, "_ps_json", _boom)
    assert hardware._pci_properties() == {}            # req 109


def test_pci_props_collects_display_only(monkeypatch):
    seen = {}

    def _ps(script, timeout=15):
        seen["script"] = script
        return {"pci": [{"id": PNP_A, "bus_number": 1, "address": 0,
                         "location_info":
                         "PCI bus 1, device 0, function 0",
                         "location_paths":
                         ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]}]}
    monkeypatch.setattr(hardware, "_ps_json", _ps)
    out = hardware._pci_properties()
    assert out[PNP_A]["bus_number"] == 1
    assert out[PNP_A]["location_info"] == ("PCI bus 1, device 0, "
                                         "function 0")
    assert "Win32_VideoController" in seen["script"]   # bounded class
    assert "Get-PnpDeviceProperty" in seen["script"]
    assert "DEVPKEY_Device_LocationInfo" in seen["script"]


def test_pci_props_failure_modes(monkeypatch):
    monkeypatch.setattr(hardware, "_ps_json", lambda s, timeout=15: None)
    assert hardware._pci_properties() == {}            # 106/107
    monkeypatch.setattr(hardware, "_ps_json",
                        lambda s, timeout=15: {"pci": "garbage"})
    assert hardware._pci_properties() == {}            # 108
    monkeypatch.setattr(hardware, "_ps_json",
                        lambda s, timeout=15: {"pci": [{"id": ""}]})
    assert hardware._pci_properties() == {}            # malformed row


def test_pci_props_single_object_normalized(monkeypatch):
    monkeypatch.setattr(
        hardware, "_ps_json",
        lambda s, timeout=15: {"pci": {"id": PNP_A, "bus_number": 1,
                                     "address": 0,
                                     "location_paths":
                                     "PCIROOT(0)#PCI(0000)"}})
    out = hardware._pci_properties()
    assert out[PNP_A]["location_paths"] == ["PCIROOT(0)#PCI(0000)"]


# -------------------------------------------------------- publication


def _patch_collection(monkeypatch, report=None, ident=None):
    rep = report if report is not None else {
        "probed_at": 1000.0, "probed_monotonic": 500.0,
        "os": {}, "cpu": {}, "memory": {},
        "gpus": [{"index": 0, "name": "RTX", "vendor": "NVIDIA",
                  "vram_bytes": 8 * GIB, "vram_source": "registry",
                  "vram_free_bytes": 6 * GIB,
                  "vram_capped_at_4gb": False,
                  "driver_version": "555.0", "role": "display",
                  "inference_capable": None}],
        "disks": [], "backends": {}, "runtimes": {},
        "hardware_profile": "high", "_identity": ident}
    monkeypatch.setattr(hardware, "_cached_probe", lambda b: rep)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: None)
    return rep


@pytest.fixture(autouse=True)
def _cold():
    hardware._reset_published()
    yield
    hardware._reset_published()


def test_probe_publishes_paired_identity(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    _patch_collection(monkeypatch, ident=ident)
    report = hardware.probe()
    assert "_identity" not in report                   # stripped
    pub = hardware.identity_cached()
    assert pub["provenance"] == "real"
    assert pub["digest"].startswith("hwid:v1:")
    assert pub["records"][0]["correlation"] == hi.CORR_QUALIFIED
    assert pub["collection"] == {"probed_at": 1000.0,
                                 "probed_monotonic": 500.0,
                                 "simulated": False}


def test_report_public_shape_unchanged(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    rep = _patch_collection(monkeypatch, ident=ident)
    hardware.probe()
    rec = hardware.probe_cached()
    assert "_identity" not in rec["report"]
    assert "_identity" not in rec                      # shape intact
    assert set(rec) == {"report", "probed_at", "probed_monotonic",
                       "simulated", "order"}
    assert set(rec["report"]["gpus"][0]) == set(rep["gpus"][0])


def test_probe_cached_never_invokes_collectors(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    _patch_collection(monkeypatch, ident=ident)
    hardware.probe()

    def _boom(*a, **k):
        raise AssertionError("cache-only path invoked a collector")
    monkeypatch.setattr(hardware, "_ps_json", _boom)
    monkeypatch.setattr(hardware, "_nvidia_smi", _boom)
    monkeypatch.setattr(hardware, "_pci_properties", _boom)
    assert hardware.probe_cached() is not None          # req 111
    assert hardware.identity_cached() is not None


def test_identity_deep_copy_isolation(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    _patch_collection(monkeypatch, ident=ident)
    hardware.probe()
    pub = hardware.identity_cached()
    pub["records"][0]["gpu_uuid"] = "MUTATED"
    pub["digest"] = "tampered"
    again = hardware.identity_cached()
    assert again["records"][0]["gpu_uuid"] == UUID_A    # req 112
    assert again["digest"].startswith("hwid:v1:")


def test_identity_monotonic_ordering(monkeypatch):
    _patch_collection(monkeypatch)                      # mono 500
    hardware.probe()
    older = copy.deepcopy(hardware._PUBLISHED["report"])
    older["probed_monotonic"] = 400.0
    hardware._publish(older, identity={"stale": True})
    pub = hardware.identity_cached()
    assert pub.get("collection", {}).get(
        "probed_monotonic") == 500.0                    # req 113


def test_unpaired_collection_honest(monkeypatch):
    rep = {"probed_at": 1.0, "probed_monotonic": 1.0, "gpus": [],
           "os": {}, "cpu": {}, "memory": {}, "disks": [],
           "backends": {}, "runtimes": {},
           "hardware_profile": "x"}                     # no _identity
    _patch_collection(monkeypatch, report=rep)
    hardware.probe()
    pub = hardware.identity_cached()
    assert pub["collection_state"] == "unpaired"
    assert pub["digest"] is None


def test_simulated_provenance_published(monkeypatch):
    _patch_collection(monkeypatch)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: {
        "gpus": [{"index": 0, "name": "FakeGPU", "vendor": "NVIDIA"}]})
    hardware.probe()
    pub = hardware.identity_cached()
    assert pub["provenance"] == "simulated"             # req 110
    assert all(r["token"] is None for r in pub["records"])
    assert pub["collection"]["simulated"] is True


def test_sim_inventory_never_qualifies():
    p = hi.simulated_payload([{"index": 0, "name": "X",
                               "vendor": "NVIDIA"}])
    assert p["records"][0]["correlation"] == hi.CORR_UNSUPPORTED


def test_probe_no_identity_leak_to_caller(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    _patch_collection(monkeypatch, ident=ident)
    rep = hardware.probe()
    assert "uuid" not in str(rep.get("gpus"))
    assert all(k != "_identity" for k in rep)


# ------------------------------------------- C3 publication pairing


def test_equal_key_different_identity_never_overwrites():
    rep = {"probed_at": 1.0, "probed_monotonic": 500.0, "gpus": [],
           "os": {}, "cpu": {}, "memory": {}, "disks": [],
           "backends": {}, "runtimes": {}}
    id_a = {"records": [{"uuid": "A"}], "digest": "hwid:v1:a"}
    id_b = {"records": [{"uuid": "B"}], "digest": "hwid:v1:b"}
    hardware._publish(rep, identity=id_a)
    hardware._publish(rep, identity=id_b)   # same key, same report
    pub = hardware.evidence_cached()
    assert pub["identity"]["digest"] == "hwid:v1:a"   # C3-C retained


def test_equal_key_identical_pair_republishes():
    rep = {"probed_at": 1.0, "probed_monotonic": 500.0, "gpus": [],
           "os": {}, "cpu": {}, "memory": {}, "disks": [],
           "backends": {}, "runtimes": {}}
    id_a = {"records": [], "digest": "hwid:v1:a"}
    hardware._publish(rep, identity=id_a)
    g1 = hardware.evidence_cached()["generation"]
    hardware._publish(dict(rep), identity=dict(id_a))
    g2 = hardware.evidence_cached()["generation"]
    assert g2 == g1 + 1                                 # accepted replay


def test_equal_key_different_report_conflict_retained():
    rep = {"probed_at": 1.0, "probed_monotonic": 500.0, "gpus": [],
           "os": {}, "cpu": {}, "memory": {}, "disks": [],
           "backends": {}, "runtimes": {}}
    other = dict(rep, gpus=[{"index": 0}])
    hardware._publish(rep, identity={"d": 1})
    hardware._publish(other, identity={"d": 1})
    pub = hardware.evidence_cached()
    assert pub["report"]["gpus"] == []                # C3-D


def test_failed_publish_leaves_pair_intact():
    rep = {"probed_at": 1.0, "probed_monotonic": 500.0, "gpus": [],
           "os": {}, "cpu": {}, "memory": {}, "disks": [],
           "backends": {}, "runtimes": {}}
    stale = dict(rep, probed_monotonic=400.0)
    hardware._publish(rep, identity={"digest": "hwid:v1:new"})
    hardware._publish(stale, identity={"digest": "hwid:v1:stale"})
    pub = hardware.evidence_cached()
    assert pub["report"]["probed_monotonic"] == 500.0  # C3-G
    assert pub["identity"]["digest"] == "hwid:v1:new"


def test_missing_identity_does_not_retain_prior(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    rep1 = _patch_collection(monkeypatch, ident=ident)
    hardware.probe()
    assert hardware.identity_cached()["records"][0]["token"]
    rep2 = dict(rep1, probed_monotonic=900.0)
    rep2.pop("_identity", None)                        # unpaired newer
    monkeypatch.setattr(hardware, "_cached_probe", lambda b: rep2)
    hardware.probe()
    pub = hardware.identity_cached()                   # C3-I
    assert pub["collection_state"] == "unpaired"


def test_caller_mutation_cannot_alter_published(monkeypatch):
    ident = hi.correlate([meta()], [smi_row()], pci_props())
    rep = _patch_collection(monkeypatch, ident=ident)
    hardware.probe()
    rep["probed_monotonic"] = -1
    ident["records"][0]["token"] = "poison"
    pub = hardware.evidence_cached()                   # C3-E
    assert pub["probed_monotonic"] == 500.0
    assert pub["identity"]["records"][0]["token"].startswith("hwid:nv:")


def test_reader_mutation_isolated(monkeypatch):
    _patch_collection(monkeypatch)
    hardware.probe()
    ev = hardware.evidence_cached()
    ev["report"]["probed_monotonic"] = -1
    ev["identity"]["collection_state"] = "corrupt"
    again = hardware.evidence_cached()                 # C3-F
    assert again["probed_monotonic"] == 500.0
    assert again["identity"]["collection_state"] == "unpaired"


def test_paired_read_never_mixes_collections():
    """C3-J — deterministic interleaving: a reader can only observe a
    coherent (report, identity, generation) triple; the lock-held
    snapshot never assembles pair members across publications."""
    import threading
    rep1 = {"probed_at": 1.0, "probed_monotonic": 500.0, "gpus": [],
            "os": {}, "cpu": {}, "memory": {}, "disks": [],
            "backends": {}, "runtimes": {}}
    rep2 = dict(rep1, probed_monotonic=900.0)
    hardware._publish(rep1, identity={"digest": "hwid:v1:one"})
    barrier = threading.Barrier(2)
    seen = []

    def writer():
        barrier.wait()
        hardware._publish(rep2, identity={"digest": "hwid:v1:two"})

    t = threading.Thread(target=writer)
    t.start()
    barrier.wait()
    seen.append(hardware.evidence_cached())
    t.join()
    seen.append(hardware.evidence_cached())
    for ev in seen:
        pairs = {ev["report"]["probed_monotonic"],
                 ev["identity"]["digest"]}
        assert pairs in ({500.0, "hwid:v1:one"},
                         {900.0, "hwid:v1:two"})
        assert (ev["report"]["probed_monotonic"] == 500.0) == (
            ev["identity"]["digest"] == "hwid:v1:one")
    gens = [ev["generation"] for ev in seen]
    assert gens == sorted(gens) and len(set(gens)) == len(gens)


# ------------------------------------- C4 partial/duplicate evidence


def test_dup_uuid_one_row_malformed_bdf():
    # A malformed-BDF SMI row cannot join, but its duplicate UUID still
    # poisons uniqueness for the well-formed row (C4-2).
    smi2 = [smi_row(), {**smi_row(index=1), "pci_bus_id": "broken"}]
    r = hi.correlate([meta()], smi2, pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_AMBIGUOUS
    assert r["records"][0]["reason"] == "duplicate_uuid_evidence"


def test_dup_pnp_records_share_address():
    # Duplicate OS device-instance records resolving to one address —
    # neither may claim it (C4-4/5).
    meta2 = [meta(index=0), meta(index=1)]             # same pnp
    r = hi.correlate(meta2, [smi_row()], pci_props())
    assert all(rec["correlation"] == hi.CORR_AMBIGUOUS
               and rec["reason"] == "duplicate_pci_association"
               for rec in r["records"])


def test_identical_and_contradictory_dup_vendor_rows():
    # Identical duplicate vendor rows (same BDF+UUID) and contradictory
    # duplicates (same BDF, different UUID) both block join (C4-6/7).
    r = hi.correlate([meta()], [smi_row(), smi_row(index=7)],
                     pci_props())
    assert r["records"][0]["correlation"] == hi.CORR_AMBIGUOUS
    r2 = hi.correlate([meta()],
                      [smi_row(), smi_row(index=7, uuid=UUID_B)],
                      pci_props())
    assert r2["records"][0]["correlation"] == hi.CORR_AMBIGUOUS


def test_unmatched_vendor_row_ignored():
    # An SMI row matching no OS device must not influence the unmatched
    # device, and never fabricates inventory (C4-9).
    r = hi.correlate(
        [meta()], [smi_row(bus_id="00000000:77:00.0")],
        pci_props())
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_UNAVAILABLE
    assert rec["smi_vram_free_bytes"] is None


def test_non_nvidia_with_pci_evidence_unsupported():
    # OS PCI evidence never upgrades a non-NVIDIA device (C4-11/12).
    metas = [meta(pnp=PNP_AMD, vendor="AMD", index=0),
             meta(pnp="ROOT\\VMBUS\\0000", vendor="unknown", index=1)]
    props = {PNP_AMD: pci_props()[PNP_A]}
    r = hi.correlate(metas, [smi_row()], props)
    assert all(rec["correlation"] == hi.CORR_UNSUPPORTED
               for rec in r["records"])
    assert all(rec["token"] is None for rec in r["records"])


def test_invalid_total_and_free_measurements():
    # Qualified join but absent measurements stay absent (C4-13/14).
    r = hi.correlate([meta()], [smi_row(total=None, free=None)],
                     pci_props())
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_QUALIFIED
    assert rec["smi_vram_bytes"] is None
    assert rec["smi_vram_free_bytes"] is None
    assert rec["token"].startswith("hwid:nv:")


def test_contradictory_free_over_total_suppressed():
    # free > total is impossible evidence — suppressed, never
    # attributed; the identity join itself remains qualified (C4-16).
    r = hi.correlate([meta()],
                     [smi_row(total=8 * GIB, free=10 * GIB)],
                     pci_props())
    rec = r["records"][0]
    assert rec["correlation"] == hi.CORR_QUALIFIED
    assert rec["smi_vram_free_bytes"] is None
    assert rec["smi_vram_bytes"] == 8 * GIB
    assert "memory_conflict" in rec["reason"]


def test_independent_qualified_row_not_punished():
    # Ambiguity on one device must not invalidate an unrelated
    # independently established qualified row (C4 independence).
    meta2 = [meta(index=0), meta(pnp=PNP_B, index=1)]
    props = {**pci_props(), **pci_props(pnp=PNP_B, bus=9)}
    # device A's address has one SMI row; device B's has two (ambiguous)
    r = hi.correlate(
        meta2,
        [smi_row(),
         smi_row(index=1, bus_id="00000000:09:00.0", uuid=UUID_B),
         smi_row(index=2, bus_id="00000000:09:00.0",
                 uuid="GPU-00000000-0000-0000-0000-000000000001")],
        props)
    assert r["records"][0]["correlation"] == hi.CORR_QUALIFIED
    assert r["records"][1]["correlation"] == hi.CORR_AMBIGUOUS

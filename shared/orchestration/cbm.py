"""CBM from the real packages, on the documents (todo 13.5).

``container_choice`` estimates the volume from the catalogue's stowage factor (``m3_per_mt``). Once the
packing spec says how big a package is (bag / carton ``length_cm`` × ``width_cm`` × ``height_cm``,
net ``unit_weight_kg`` and gross ``gross_weight_kg``), the shipment's numbers are exact:

    packages   = quantity (kg) / net weight per package   (rounded up)
    CBM        = packages × package volume
    gross (kg) = packages × gross weight per package (net + 1 % when the gross is not given)

They go onto the packing list, the commercial invoice, the shipping instructions and the Maersk
booking (``cargoGrossWeight`` from the packages, ``volume`` in CBM), into the LCL freight (W/M) and
the container fill check against usable volume (20' ≈ 33, 40' ≈ 67, 40' HC ≈ 76 CBM) and payload.
Without dimensions the stowage factor is used, marked ``cbm_source=estimate``.
"""
from __future__ import annotations

import math
from typing import Any

USABLE_CBM = {"20GP": 33.0, "40GP": 67.0, "40HC": 76.0, "20RF": 28.0, "40RF": 59.0}
PAYLOAD_T = {"20GP": 28.0, "40GP": 26.5, "40HC": 26.5, "20RF": 27.0, "40RF": 29.0}


def _g(spec: Any, key: str) -> float | None:
    v = spec.get(key) if isinstance(spec, dict) else getattr(spec, key, None)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def package_cbm(spec: Any) -> float | None:
    """Pure: one package's volume in m³ (None without all three dimensions)."""
    dims = [_g(spec, k) for k in ("length_cm", "width_cm", "height_cm")] if spec is not None else []
    if len(dims) != 3 or any(d is None or d <= 0 for d in dims):
        return None
    return round(dims[0] * dims[1] * dims[2] / 1_000_000.0, 5)


def shipment(spec: Any, quantity_mt: float | None, m3_per_mt: float | None = None) -> dict[str, Any]:
    """Pure: packages, CBM, net / gross kg for a lot — exact from the package, else estimated."""
    qty_kg = float(quantity_mt or 0) * 1000.0
    unit = _g(spec, "unit_weight_kg") if spec is not None else None
    pkg = package_cbm(spec)
    out: dict[str, Any] = {"net_kg": round(qty_kg, 1), "packages": None, "cbm": None, "gross_kg": None,
                           "cbm_source": None, "package_cbm": pkg}
    if unit and unit > 0 and qty_kg > 0:
        packages = math.ceil(qty_kg / unit - 1e-9)
        gross_each = _g(spec, "gross_weight_kg") or unit * 1.01
        out.update(packages=packages, gross_kg=round(packages * gross_each, 1))
        if pkg:
            out.update(cbm=round(packages * pkg, 2), cbm_source="packages")
    if out["cbm"] is None and m3_per_mt and quantity_mt:
        out.update(cbm=round(float(quantity_mt) * float(m3_per_mt), 2), cbm_source="estimate")
    return out


def fill_check(cbm: float | None, gross_kg: float | None, boxes: dict[str, int]) -> dict[str, Any]:
    """Pure: does the cargo fit the planned boxes by volume and by payload?"""
    vol = sum(USABLE_CBM.get(code, 33.0) * n for code, n in (boxes or {}).items() if code != "LCL")
    pay = sum(PAYLOAD_T.get(code, 26.0) * 1000.0 * n for code, n in (boxes or {}).items() if code != "LCL")
    out = {"usable_cbm": vol or None, "payload_kg": pay or None, "fits": True, "problems": []}
    if cbm and vol and cbm > vol:
        out["fits"] = False
        out["problems"].append(f"{cbm:g} CBM does not fit the planned boxes ({vol:g} CBM usable)")
    if gross_kg and pay and gross_kg > pay:
        out["fits"] = False
        out["problems"].append(f"{gross_kg:,.0f} kg is above the boxes' payload ({pay:,.0f} kg)")
    if cbm and vol and out["fits"]:
        out["volume_used_pct"] = round(cbm / vol * 100.0, 1)
    return out


def loaded_count_check(planned_packages: int | None, loaded_packages: int | None, tolerance_pct: float = 1.0) -> str | None:
    """Pure: the inspector's loaded package count against the plan (None when they agree or one is unknown)."""
    if not planned_packages or loaded_packages is None:
        return None
    if abs(loaded_packages - planned_packages) > planned_packages * tolerance_pct / 100.0:
        return f"inspector counted {loaded_packages} packages, the packing plan has {planned_packages}"
    return None

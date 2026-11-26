"""Transmittance_info: thermal transmittance (U-value) of the constructions and glazing of the model.

Copyright (c) 2026 Germán Ramos Ruiz. SPDX-License-Identifier: Apache-2.0

ModelMeasure for OpenStudio 3.11, meant for "Apply Measure Now" (Components & Measures menu): without simulating, it
computes the U-value of the constructions applied in the model (directly or through construction sets) and opens an
HTML report in the web browser. It does not modify the model.

  * Opaque constructions: U = 1 / (Rsi + Σ e/λ + Rse) with the surface resistances of EN ISO 6946 (the ones used by the
    Spanish Building Code, CTE DA DB-HE/1), according to the position of the element and the direction of heat flow.
    For comparison, the U-value with the ASHRAE air films reported by EnergyPlus is also given.
  * "Simple Glazing System" glazing: the U-factor of the object itself.
  * Layered glazing: the centre-of-glass U-value as EnergyPlus computes it (ISO 15099, NFRC winter conditions: 21 °C
    inside, -18 °C outside, outside convection 26 W/m²K) and the Ug of EN 673 (the value declared by manufacturers).
    Frames are not included.

Tested with OpenStudio 3.11.0 (EnergyPlus 25.2.0).
"""

import datetime
import html
import json
import math
import os
import re
import tempfile
import typing
import webbrowser
from pathlib import Path

import openstudio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGMA = 5.670374419e-8  # W/m²K⁴
GRAV = 9.81  # m/s²
R_GAS = 8314.462618  # J/kmol·K
P_ATM = 101325.0  # Pa
KELVIN = 273.15

# Surface resistances (m²K/W), EN ISO 6946 / CTE DA DB-HE/1
RSI = {"h": 0.13, "up": 0.10, "down": 0.17}
RSE_EXT = 0.04
FLOW_LABEL = {"h": "horizontal", "up": "upward", "down": "downward"}
# ASHRAE 90.1 (Appendix A) air films used by the EnergyPlus Envelope Summary report (m²K/W)
IP_R = 0.176110  # h·ft²·°F/Btu -> m²K/W
ASHRAE_RSI = {"Wall": 0.68 * IP_R, "RoofCeiling": 0.61 * IP_R, "Floor": 0.92 * IP_R}
ASHRAE_RSE = 0.17 * IP_R

# Gases according to ISO 15099, the same coefficients as EnergyPlus: (k: a, b), (μ: a, b), (cp: a, b), M
# k = a + b·T [W/m·K], μ = a + b·T [Pa·s], cp = a + b·T [J/kg·K], T in K
GASES_15099 = {
    "Air": ((2.873e-3, 7.760e-5), (3.723e-6, 4.940e-8), (1002.737, 1.2324e-2), 28.97),
    "Argon": ((2.285e-3, 5.149e-5), (3.379e-6, 6.451e-8), (521.9285, 0.0), 39.948),
    "Krypton": ((9.443e-4, 2.826e-5), (2.213e-6, 7.777e-8), (248.0907, 0.0), 83.8),
    "Xenon": ((4.538e-4, 1.723e-5), (1.069e-6, 7.414e-8), (158.3397, 0.0), 131.3),
}
# Gases according to EN 673 (10 °C): ρ [kg/m³], μ [Pa·s], λ [W/m·K], c [J/kg·K]
GASES_EN673 = {
    "Air": (1.232, 1.761e-5, 2.496e-2, 1008.0),
    "Argon": (1.699, 2.164e-5, 1.684e-2, 519.0),
    "Krypton": (3.56, 2.4e-5, 0.900e-2, 245.0),
    "Xenon": (5.689, 2.226e-5, 0.529e-2, 161.0),
}
GAS_NAMES = {"Air": "air", "Argon": "argon", "Krypton": "krypton", "Xenon": "xenon", "Custom": "custom gas"}

# NFRC winter conditions (EnergyPlus nominal glass U-factor)
NFRC_T_IN, NFRC_T_OUT, NFRC_HC_OUT = 21.0 + KELVIN, -18.0 + KELVIN, 26.0
NOMINAL_HEIGHT = 1.0  # m, glass height for convection (nominal calculation)

GROUND_BCS = ("Ground", "GroundFCfactorMethod", "Foundation", "GroundSlabPreprocessorAverage",
              "GroundSlabPreprocessorCore", "GroundSlabPreprocessorPerimeter",
              "GroundBasementPreprocessorAverageWall", "GroundBasementPreprocessorAverageFloor",
              "GroundBasementPreprocessorUpperWall", "GroundBasementPreprocessorLowerWall")
GLAZED_SUBSURFACES = {"FixedWindow": "Window", "OperableWindow": "Window", "GlassDoor": "Glass door",
                      "Skylight": "Skylight", "TubularDaylightDome": "Tubular daylighting device",
                      "TubularDaylightDiffuser": "Tubular daylighting device"}

CAT_WALL, CAT_ROOF, CAT_EXP_FLOOR = "Exterior wall", "Roof", "Exposed floor (outdoor air below)"
CAT_G_WALL, CAT_G_FLOOR, CAT_G_ROOF = "Below-grade wall", "Slab / ground floor", "Below-grade roof"
CAT_PART_V, CAT_PART_H = "Interior partition (vertical)", "Interior partition (horizontal)"
GROUND_CATS = (CAT_G_WALL, CAT_G_FLOOR, CAT_G_ROOF)
CATEGORY_ORDER = [CAT_WALL, CAT_ROOF, CAT_EXP_FLOOR, CAT_G_WALL, CAT_G_FLOOR, CAT_G_ROOF, CAT_PART_V, CAT_PART_H,
                  "Exterior door", "Interior door",
                  "Window", "Glass door", "Skylight", "Tubular daylighting device", "Interior window"]


# ---------------------------------------------------------------------------
# Opaque constructions
# ---------------------------------------------------------------------------


def flow_direction(tilt_deg):
    """Direction of heat flow (inside to outside) from the tilt of the outward normal with respect to the vertical."""
    if 60.0 <= tilt_deg <= 120.0:
        return "h"
    return "up" if tilt_deg < 60.0 else "down"


def opaque_layer(material):
    """dict(name, kind, e, λ, R) of an opaque layer, or None if it cannot be computed."""
    name = material.nameString()
    o = material.to_StandardOpaqueMaterial()
    if o.is_initialized():
        m = o.get()
        return dict(name=name, kind="Material", e=m.thickness(), lam=m.thermalConductivity(),
                    R=m.thickness() / m.thermalConductivity())
    o = material.to_MasslessOpaqueMaterial()
    if o.is_initialized():
        return dict(name=name, kind="No mass (R)", e=None, lam=None, R=o.get().thermalResistance())
    o = material.to_AirGap()
    if o.is_initialized():
        return dict(name=name, kind="Air gap (R)", e=None, lam=None, R=o.get().thermalResistance())
    o = material.to_InfraredTransparentMaterial()
    if o.is_initialized():
        return dict(name=name, kind="Infrared transparent", e=None, lam=None, R=0.0)
    o = material.to_RoofVegetation()
    if o.is_initialized():
        m = o.get()
        return dict(name=name, kind="Green roof (dry soil)", e=m.thickness(), lam=m.conductivityofDrySoil(),
                    R=m.thickness() / m.conductivityofDrySoil())
    return None


def opaque_u(r_layers, rsi, rse):
    return 1.0 / (rsi + r_layers + rse)


def ashrae_films(surface_type, exterior, ground):
    """ASHRAE Rsi + Rse as in the EnergyPlus Envelope Summary (by surface type, not by tilt)."""
    rsi = ASHRAE_RSI.get(surface_type, ASHRAE_RSI["Wall"])
    if exterior:
        return rsi + ASHRAE_RSE
    if ground:
        return rsi
    if surface_type == "Wall":
        return 2 * ASHRAE_RSI["Wall"]
    return ASHRAE_RSI["RoofCeiling"] + ASHRAE_RSI["Floor"]  # interior floor: one face upward and the other downward


# ---------------------------------------------------------------------------
# Gases
# ---------------------------------------------------------------------------


def _pure_15099(gas, t):
    """(k, μ, cp, M) of a pure gas at temperature t [K]. gas: name or dict of coefficients (custom gas)."""
    if isinstance(gas, str):
        (ka, kb), (ma, mb), (ca, cb), mw = GASES_15099[gas]
        return ka + kb * t, ma + mb * t, ca + cb * t, mw
    k = gas["k"][0] + gas["k"][1] * t + gas["k"][2] * t * t
    mu = gas["mu"][0] + gas["mu"][1] * t + gas["mu"][2] * t * t
    cp = gas["cp"][0] + gas["cp"][1] * t + gas["cp"][2] * t * t
    return k, mu, cp, gas["M"]


def gas_properties_15099(mix, t):
    """(k, μ, cp, ρ) of a mixture [(fraction, gas)] at t [K], with the ISO 15099 mixing rules."""
    comps = [(x,) + _pure_15099(g, t) for x, g in mix if x > 0]
    m_mix = sum(x * mw for x, _, _, _, mw in comps)
    rho = P_ATM * m_mix / (R_GAS * t)
    if len(comps) == 1:
        _, k, mu, cp, _ = comps[0]
        return k, mu, cp, rho
    # viscosity
    mu_mix = 0.0
    for i, (xi, _, mui, _, mi) in enumerate(comps):
        s = 0.0
        for j, (xj, _, muj, _, mj) in enumerate(comps):
            if i != j:
                phi = (1 + math.sqrt(mui / muj) * (mj / mi) ** 0.25) ** 2 / (math.sqrt(8) * math.sqrt(1 + mi / mj))
                s += phi * xj / xi
        mu_mix += mui / (1 + s)
    # conductivity: monatomic part (k') and the rest (k'')
    kp = [3.75 * R_GAS / mi * mui for _, _, mui, _, mi in comps]
    kpp = [ki - kpi for (_, ki, _, _, _), kpi in zip(comps, kp)]
    k1 = k2 = 0.0
    for i, (xi, _, _, _, mi) in enumerate(comps):
        s1 = s2 = 0.0
        for j, (xj, _, _, _, mj) in enumerate(comps):
            if i == j:
                continue
            base = (1 + math.sqrt(kp[i] / kp[j]) * (mi / mj) ** 0.25) ** 2 / (math.sqrt(8) * math.sqrt(1 + mi / mj))
            psi = base * (1 + 2.41 * (mi - mj) * (mi - 0.142 * mj) / (mi + mj) ** 2)
            s1 += psi * xj / xi
            s2 += base * xj / xi
        k1 += kp[i] / (1 + s1)
        k2 += kpp[i] / (1 + s2)
    cp_mix = sum(x * cp * mw for x, _, _, cp, mw in comps) / m_mix
    return k1 + k2, mu_mix, cp_mix, rho


def gas_properties_en673(mix):
    """(ρ, μ, λ, c) of a mixture according to EN 673 (10 °C, linear weighting by volume fraction)."""
    out = [0.0, 0.0, 0.0, 0.0]
    for x, g in mix:
        if isinstance(g, str):
            vals = GASES_EN673[g]
        else:  # custom gas: ISO 15099 coefficients at 10 °C
            k, mu, cp, rho = gas_properties_15099([(1.0, g)], 283.15)
            vals = (rho, mu, k, cp)
        for i in range(4):
            out[i] += x * vals[i]
    return tuple(out)


# ---------------------------------------------------------------------------
# Layered glazing
# ---------------------------------------------------------------------------


def _gap_nusselt_15099(ra, aspect):
    """Nusselt number of a vertical cavity (ISO 15099, tilt 90°)."""
    if ra > 5.0e4:
        nu1 = 0.0673838 * ra ** (1.0 / 3.0)
    elif ra > 1.0e4:
        nu1 = 0.028154 * ra ** 0.4134
    else:
        nu1 = 1.0 + 1.7596678e-10 * ra ** 2.2984755
    nu2 = 0.242 * (ra / aspect) ** 0.272
    return max(nu1, nu2)


def _inside_hc_15099(t_surf, t_air, height=NOMINAL_HEIGHT):
    """Natural convection on the inside face of a vertical glazing (ISO 15099, section 8.3.2.2)."""
    dt = abs(t_surf - t_air)
    if dt < 1e-6:
        return 0.0
    tm = t_air + 0.25 * (t_surf - t_air)
    k, mu, cp, rho = gas_properties_15099([(1.0, "Air")], tm)
    ra = rho ** 2 * height ** 3 * GRAV * cp * dt / (tm * mu * k)
    ra_cv = 2.5e5 * (math.exp(0.72 * 90.0) / math.sin(math.radians(90.0))) ** 0.2
    if ra <= ra_cv:
        nu = 0.56 * ra ** 0.25
    else:
        nu = 0.13 * (ra ** (1.0 / 3.0) - ra_cv ** (1.0 / 3.0)) + 0.56 * ra_cv ** 0.25
    return nu * k / height


def u_glass_15099(layers, t_in=NFRC_T_IN, t_out=NFRC_T_OUT, hc_out=NFRC_HC_OUT):
    """Centre-of-glass U-value [W/m²K] under nominal conditions, as EnergyPlus computes it (ISO 15099).

    layers: outside to inside; glass = dict(kind="glass", t, k, ef, eb); gap = dict(kind="gas", t, mix).
    """
    n = len(layers)
    if n == 0 or layers[0]["kind"] != "glass" or layers[-1]["kind"] != "glass":
        return None
    # face temperatures: 2 per glass pane, from outside to inside
    glasses = [i for i, lay in enumerate(layers) if lay["kind"] == "glass"]
    nfaces = 2 * len(glasses)
    temps = [t_out + (t_in - t_out) * (f + 1) / (nfaces + 1) for f in range(nfaces)]
    u = None
    for _ in range(200):
        res = []  # resistances in series
        t1 = temps[0]
        hr_out = layers[0]["ef"] * SIGMA * (t1 ** 4 - t_out ** 4) / (t1 - t_out) if abs(t1 - t_out) > 1e-9 else \
            4 * layers[0]["ef"] * SIGMA * t_out ** 3
        res.append(1.0 / (hc_out + hr_out))
        face = 0
        for i, lay in enumerate(layers):
            if lay["kind"] == "glass":
                res.append(lay["t"] / lay["k"])
                face += 2
            else:
                ta, tb = temps[face - 1], temps[face]
                ea, eb = layers[i - 1]["eb"], layers[i + 1]["ef"]
                tm = 0.5 * (ta + tb)
                k, mu, cp, rho = gas_properties_15099(lay["mix"], tm)
                dt = max(abs(ta - tb), 1e-6)
                ra = rho ** 2 * lay["t"] ** 3 * GRAV * cp * dt / (tm * mu * k)
                hc = _gap_nusselt_15099(ra, NOMINAL_HEIGHT / lay["t"]) * k / lay["t"]
                denom = 1.0 / ea + 1.0 / eb - 1.0
                hr = SIGMA * (ta ** 4 - tb ** 4) / (denom * (ta - tb)) if abs(ta - tb) > 1e-9 else \
                    4 * SIGMA * tm ** 3 / denom
                res.append(1.0 / (hc + hr))
        tn = temps[-1]
        e_in = layers[-1]["eb"]
        hr_in = e_in * SIGMA * (t_in ** 4 - tn ** 4) / (t_in - tn) if abs(t_in - tn) > 1e-9 else \
            4 * e_in * SIGMA * t_in ** 3
        res.append(1.0 / (_inside_hc_15099(tn, t_in) + hr_in))
        u_new = 1.0 / sum(res)
        q = u_new * (t_in - t_out)
        # new face temperatures, from the outside
        new, t = [], t_out
        for r in res[:-1]:
            t += q * r
            new.append(t)
        temps = [0.5 * a + 0.5 * b for a, b in zip(temps, new)]
        if u is not None and abs(u_new - u) < 1e-7:
            u = u_new
            break
        u = u_new
    return u


def ug_en673(layers):
    """Ug [W/m²K] according to EN 673 (vertical glazing, ΔT = 15 K, Tm = 283 K, he = 25 W/m²K)."""
    if not layers or layers[0]["kind"] != "glass" or layers[-1]["kind"] != "glass":
        return None
    r_glass = sum(lay["t"] * 1.0 for lay in layers if lay["kind"] == "glass")  # glass resistivity 1 m·K/W
    gaps = [(i, lay) for i, lay in enumerate(layers) if lay["kind"] == "gas"]
    tm = 283.0
    dts = [15.0 / max(len(gaps), 1)] * len(gaps)
    r_gaps = []
    for _ in range(50):
        r_gaps = []
        for (i, lay), dt in zip(gaps, dts):
            rho, mu, lam, c = gas_properties_en673(lay["mix"])
            s = lay["t"]
            gr = GRAV * s ** 3 * dt * rho ** 2 / (tm * mu ** 2)
            pr = mu * c / lam
            nu = max(1.0, 0.035 * (gr * pr) ** 0.38)
            e1, e2 = layers[i - 1]["eb"], layers[i + 1]["ef"]
            hr = 4 * SIGMA * tm ** 3 / (1.0 / e1 + 1.0 / e2 - 1.0)
            r_gaps.append(1.0 / (nu * lam / s + hr))
        total = sum(r_gaps)
        new = [15.0 * r / total for r in r_gaps] if total > 0 else dts
        if all(abs(a - b) < 1e-6 for a, b in zip(new, dts)):
            break
        dts = new
    hi = 3.6 + 4.1 * layers[-1]["eb"] / 0.837  # 7.7 W/m²K with uncoated glass on the inside face
    return 1.0 / (1.0 / 25.0 + r_glass + sum(r_gaps) + 1.0 / hi)


def glazing_layers(construction):
    """(layers for the calculation, composition text, warning) of a layered glazing construction."""
    out, text = [], []
    for mat in construction.layers():
        g = mat.to_StandardGlazing()
        if g.is_initialized():
            g = g.get()
            ef, eb = g.frontSideInfraredHemisphericalEmissivity(), g.backSideInfraredHemisphericalEmissivity()
            out.append(dict(kind="glass", t=g.thickness(), k=g.thermalConductivity(), ef=ef, eb=eb))
            coat = f" (ε {min(ef, eb):.2f})" if min(ef, eb) < 0.5 else ""
            text.append(f"glass {g.thickness() * 1000:.0f} mm{coat}")
            continue
        g = mat.to_Gas()
        if g.is_initialized():
            g = g.get()
            gt = g.gasType()
            if gt == "Custom":
                gas = dict(k=(g.customConductivityCoefficientA(), g.customConductivityCoefficientB(),
                              g.customConductivityCoefficientC()),
                           mu=(g.customViscosityCoefficientA(), g.customViscosityCoefficientB(),
                               g.customViscosityCoefficientC()),
                           cp=(g.customSpecificHeatCoefficientA(), g.customSpecificHeatCoefficientB(),
                               g.customSpecificHeatCoefficientC()),
                           M=g.customMolecularWeight())
            else:
                gas = gt
            out.append(dict(kind="gas", t=g.thickness(), mix=[(1.0, gas)]))
            text.append(f"{g.thickness() * 1000:.0f} mm {GAS_NAMES.get(gt, gt)}")
            continue
        g = mat.to_GasMixture()
        if g.is_initialized():
            g = g.get()
            mix = []
            for i in range(1, g.numGases() + 1):
                gt = getattr(g, f"gas{i}Type")()
                fr = getattr(g, f"gas{i}Fraction")()
                if fr > 0:
                    mix.append((fr, gt))
            out.append(dict(kind="gas", t=g.thickness(), mix=mix))
            desc = " + ".join(f"{fr * 100:.0f}% {GAS_NAMES.get(gt, gt)}" for fr, gt in mix)
            text.append(f"{g.thickness() * 1000:.0f} mm ({desc})")
            continue
        return None, " / ".join(text + [mat.nameString()]), (
            f"layer '{mat.nameString()}' ({mat.iddObjectType().valueName().replace('OS_', '')}) not supported: "
            "shading devices, thermochromic or other glazing types")
    return out, " / ".join(text), ""


# ---------------------------------------------------------------------------
# Model traversal
# ---------------------------------------------------------------------------


def _multiplier(planar):
    sp = planar.space()
    return sp.get().multiplier() if sp.is_initialized() else 1


def _deg(rad):
    return math.degrees(rad)


def collect(model, include_interior=True):
    """Data of the opaque elements and of the fenestration, with their applied construction."""
    opaque, glazing, details, issues = {}, {}, {}, []

    def add_opaque(cons, category, area, rs, films, flow, defaulted, pair=None):
        key = (cons.nameString(), category)
        row = opaque.get(key)
        if row is None:
            row = dict(cons=cons.nameString(), cat=category, n=0, area=0.0, flow=flow, ashrae_ua=0.0, ashrae_a=0.0,
                       pairs=set(), n_set=0, n_hand=0)
            info = construction_info(cons)
            row.update(info)
            if info["R"] is not None:
                if flow == "both":
                    row["rsi"], row["rse"] = (RSI["up"], RSI["down"]), (RSI["up"], RSI["down"])
                    row["U"] = (opaque_u(info["R"], RSI["up"], RSI["up"]), opaque_u(info["R"], RSI["down"], RSI["down"]))
                else:
                    row["rsi"], row["rse"] = rs
                    row["U"] = opaque_u(info["R"], rs[0], rs[1]) if info["kind"] == "layers" else info["U_eq"]
            opaque[key] = row
            details[cons.nameString()] = info
        row["n"] += 1
        row["area"] += area
        row["n_set" if defaulted else "n_hand"] += 1
        if pair and pair != cons.nameString():
            row["pairs"].add(pair)
        if row["kind"] == "layers" and area > 0:
            row["ashrae_ua"] += opaque_u(row["R"], films, 0.0) * area
            row["ashrae_a"] += area

    for s in sorted(model.getSurfaces(), key=lambda x: x.nameString()):
        oc = s.construction()
        if not oc.is_initialized():
            issues.append(f"Surface '{s.nameString()}' has no construction.")
            continue
        cons = oc.get()
        bc = s.outsideBoundaryCondition()
        flow = flow_direction(_deg(s.tilt()))
        mult = _multiplier(s)
        area = s.netArea() * mult
        pair = None
        if bc == "Outdoors":
            cat = {"h": CAT_WALL, "up": CAT_ROOF, "down": CAT_EXP_FLOOR}[flow]
            rs = (RSI[flow], RSE_EXT)
        elif bc in GROUND_BCS:
            cat = {"h": CAT_G_WALL, "down": CAT_G_FLOOR, "up": CAT_G_ROOF}[flow]
            rs = (RSI[flow], 0.0)
        else:  # Surface, Adiabatic, OtherSide...
            if not include_interior:
                continue
            adj = s.adjacentSurface()
            if adj.is_initialized():
                if str(adj.get().handle()) < str(s.handle()):
                    continue  # the other surface of this partition was already counted (same physical area)
                ac = adj.get().construction()
                pair = ac.get().nameString() if ac.is_initialized() else None
            if flow == "h":
                cat, rs = CAT_PART_V, (RSI["h"], RSI["h"])
            else:
                cat, rs, flow = CAT_PART_H, None, "both"
        films = ashrae_films(s.surfaceType(), bc == "Outdoors", bc in GROUND_BCS)
        add_opaque(cons, cat, area, rs, films, flow, s.isConstructionDefaulted(), pair)

    for ss in sorted(model.getSubSurfaces(), key=lambda x: x.nameString()):
        oc = ss.construction()
        if not oc.is_initialized():
            issues.append(f"Sub-surface '{ss.nameString()}' has no construction.")
            continue
        cons = oc.get()
        parent = ss.surface()
        bc = parent.get().outsideBoundaryCondition() if parent.is_initialized() else "Outdoors"
        exterior = bc == "Outdoors"
        if not exterior and not include_interior:
            continue
        mult = _multiplier(ss)
        area = ss.grossArea() * mult
        st = ss.subSurfaceType()
        if cons.isFenestration():
            cat = GLAZED_SUBSURFACES.get(st, "Window") if exterior else "Interior window"
            key = (cons.nameString(), cat)
            row = glazing.get(key)
            if row is None:
                row = dict(cons=cons.nameString(), cat=cat, n=0, area=0.0, frames=0, n_set=0, n_hand=0)
                row.update(glazing_info(cons))
                glazing[key] = row
            row["n"] += 1
            row["area"] += area
            row["n_set" if ss.isConstructionDefaulted() else "n_hand"] += 1
            if ss.windowPropertyFrameAndDivider().is_initialized():
                row["frames"] += 1
        else:
            flow = flow_direction(_deg(ss.tilt()))
            if exterior:
                cat, rs = "Exterior door", (RSI[flow], RSE_EXT)
            else:
                cat, rs = "Interior door", (RSI[flow], RSI[flow])
            ptype = parent.get().surfaceType() if parent.is_initialized() else "Wall"
            films = ashrae_films(ptype, exterior, bc in GROUND_BCS) if (exterior or ptype == "Wall") else \
                2 * ASHRAE_RSI["Wall"]
            add_opaque(cons, cat, area, rs, films, flow, ss.isConstructionDefaulted())
    return opaque, glazing, details, issues


def construction_info(cons):
    """Layers and resistance of an opaque construction (or its F/C-factor)."""
    ff = cons.to_FFactorGroundFloorConstruction()
    if ff.is_initialized():
        ff = ff.get()
        u_eq = ff.fFactor() * ff.perimeterExposed() / ff.area() if ff.area() > 0 else None
        return dict(kind="ffactor", layers=[], R=0.0, thickness=None, U_eq=u_eq,
                    note=f"F-factor {ff.fFactor():.3f} W/m·K; equivalent U = F·P/A "
                         f"(P = {ff.perimeterExposed():.1f} m, A = {ff.area():.1f} m²), ground included")
    cf = cons.to_CFactorUndergroundWallConstruction()
    if cf.is_initialized():
        cf = cf.get()
        return dict(kind="cfactor", layers=[], R=1.0 / cf.cFactor(), thickness=None, U_eq=None,
                    note=f"C-factor {cf.cFactor():.3f} W/m²K (no air films or soil), height {cf.height():.2f} m")
    if cons.to_ConstructionAirBoundary().is_initialized():
        return dict(kind="other", layers=[], R=None, thickness=None, U_eq=None,
                    note="air boundary (Construction:AirBoundary): separates zones without a physical element")
    lc = cons.to_LayeredConstruction()
    if not lc.is_initialized():
        return dict(kind="other", layers=[], R=None, thickness=None, U_eq=None,
                    note=f"construction type not computable ({cons.iddObjectType().valueName().replace('OS_', '')})")
    layers, unknown = [], []
    for mat in lc.get().layers():
        info = opaque_layer(mat)
        if info is None:
            unknown.append(mat.nameString())
        else:
            layers.append(info)
    if unknown:
        return dict(kind="other", layers=layers, R=None, thickness=None, U_eq=None,
                    note="unsupported layers: " + ", ".join(unknown))
    thickness = sum(lay["e"] for lay in layers if lay["e"] is not None)
    return dict(kind="layers", layers=layers, R=sum(lay["R"] for lay in layers), thickness=thickness, U_eq=None,
                note="")


def glazing_info(cons):
    """Glass U-value (EnergyPlus / EN 673), SHGC and visible transmittance of a fenestration construction."""
    lc = cons.to_LayeredConstruction()
    layers = list(lc.get().layers()) if lc.is_initialized() else []
    if len(layers) == 1 and layers[0].to_SimpleGlazing().is_initialized():
        sg = layers[0].to_SimpleGlazing().get()
        vt = sg.visibleTransmittance()
        return dict(kind="Simple Glazing System", U_ep=sg.uFactor(), Ug=None, g=sg.solarHeatGainCoefficient(),
                    vt=vt.get() if vt.is_initialized() else None,
                    comp=f"U {sg.uFactor():.2f}, SHGC {sg.solarHeatGainCoefficient():.2f}",
                    note="values entered in the object (if defined that way, they are whole-window values)")
    if not lc.is_initialized():
        return dict(kind="Not computable", U_ep=None, Ug=None, g=None, vt=None, comp="",
                    note=f"type {cons.iddObjectType().valueName().replace('OS_', '')}")
    calc, comp, note = glazing_layers(lc.get())
    if calc is None:
        return dict(kind="Not computable", U_ep=None, Ug=None, g=None, vt=None, comp=comp, note=note)
    if any(lay["kind"] == "gas" for i, lay in enumerate(calc) if i in (0, len(calc) - 1)) or \
            any(a["kind"] == b["kind"] for a, b in zip(calc, calc[1:])):
        return dict(kind="Not computable", U_ep=None, Ug=None, g=None, vt=None, comp=comp,
                    note="the layers must alternate glass and gas, with glass on both faces")
    return dict(kind="Layered", U_ep=u_glass_15099(calc), Ug=ug_en673(calc), g=None, vt=None, comp=comp,
                note="")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _f(v, d=2):
    if v is None:
        return "—"
    if isinstance(v, tuple):
        return " / ".join(_f(x, d) for x in v)
    return f"{v:,.{d}f}"


def _e(s):
    return html.escape(str(s))


def origin_text(row):
    """Origin of the construction: construction set (default) or assigned to the surface."""
    a, b = row.get("n_set", 0), row.get("n_hand", 0)
    if a and b:
        return f"Set ({a}) and assigned ({b})"
    return "Construction set" if a else "Assigned"


def _cat_key(row):
    c = row["cat"]
    return (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, row["cons"].lower())


def summary_rows(opaque, glazing):
    """By category: number of constructions, area and area-weighted U-value."""
    out = {}
    for row in list(opaque.values()) + list(glazing.values()):
        u = row.get("U") if "U" in row else row.get("U_ep")
        acc = out.setdefault(row["cat"], dict(cat=row["cat"], n=0, area=0.0, ua=[0.0, 0.0], aw=0.0))
        acc["n"] += 1
        acc["area"] += row["area"]
        if u is not None and row["area"] > 0:
            us = u if isinstance(u, tuple) else (u, u)
            acc["ua"][0] += us[0] * row["area"]
            acc["ua"][1] += us[1] * row["area"]
            acc["aw"] += row["area"]
    res = []
    for acc in out.values():
        if acc["aw"] > 0:
            a, b = acc["ua"][0] / acc["aw"], acc["ua"][1] / acc["aw"]
            acc["U"] = (a, b) if acc["cat"] == CAT_PART_H else a
        else:
            acc["U"] = None
        res.append(acc)
    return sorted(res, key=lambda r: CATEGORY_ORDER.index(r["cat"]) if r["cat"] in CATEGORY_ORDER else 99)


CSS = """
:root { --fg:#1d2433; --muted:#5b6475; --line:#d9dee7; --head:#eef2f7; --accent:#1f5fa8; --warn:#9a5b00; --bg:#fff; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", system-ui, -apple-system, sans-serif; color: var(--fg); background: var(--bg);
       font-size: 14px; line-height: 1.45; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 8px; color: var(--accent); }
.sub { color: var(--muted); margin: 0 0 12px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 4px; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: right; vertical-align: top; }
th { background: var(--head); font-weight: 600; font-size: 12.5px; }
th:first-child, td:first-child, td.l, th.l { text-align: left; }
td.u { font-weight: 700; }
tr.cat td { background: #f7f9fc; font-weight: 600; color: var(--accent); text-align: left; }
.note { color: var(--muted); font-size: 12.5px; }
.warn { color: var(--warn); }
details { border: 1px solid var(--line); border-radius: 6px; margin: 6px 0; padding: 4px 10px; }
summary { cursor: pointer; font-weight: 600; }
.bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
button { font: inherit; padding: 6px 12px; border: 1px solid var(--accent); color: var(--accent); background: #fff;
         border-radius: 6px; cursor: pointer; }
button:hover { background: var(--accent); color: #fff; }
ul { margin: 6px 0; padding-left: 20px; }
@media print { .bar { display: none; } details { break-inside: avoid; } }
"""

# CSV: comma + decimal point (international) or semicolon + decimal comma (Excel with a European locale)
JS = """
function csv(sep, dec){
  const d = JSON.parse(document.getElementById('report-data').textContent);
  const rows = [d.head].concat(d.rows);
  const num = /^-?[0-9.]+( \\/ -?[0-9.]+)*$/;
  const txt = rows.map(r => r.map(v => {
      if (v === null || v === undefined) return '';
      let s = String(v);
      if ((typeof v === 'number' || num.test(s)) && dec !== '.') s = s.split('.').join(dec);
      return (s.includes(sep) || /["\\n]/.test(s)) ? '"' + s.replace(/"/g, '""') + '"' : s; }).join(sep)).join('\\r\\n');
  const blob = new Blob(['\\ufeff' + txt], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = d.file; a.click();
}
"""


def build_html(title, opaque, glazing, details, issues, meta):
    opq = sorted(opaque.values(), key=_cat_key)
    glz = sorted(glazing.values(), key=_cat_key)
    summ = summary_rows(opaque, glazing)

    srows = "".join(
        f"<tr><td>{_e(r['cat'])}</td><td>{r['n']}</td><td>{_f(r['area'], 1)}</td><td class='u'>{_f(r['U'], 2)}"
        f"{'†' if r['cat'] in GROUND_CATS and r['U'] is not None else ''}</td></tr>" for r in summ)

    body, last = [], None
    for r in opq:
        if r["cat"] != last:
            body.append(f"<tr class='cat'><td colspan='9'>{_e(r['cat'])}</td></tr>")
            last = r["cat"]
        ashrae = r["ashrae_ua"] / r["ashrae_a"] if r["ashrae_a"] > 0 else None
        if r["kind"] in ("ffactor", "cfactor", "other"):
            rs_txt = "—"
        elif isinstance(r.get("rsi"), tuple):
            rs_txt = f"{_f(r['rsi'][0])} + {_f(r['rse'][0])} / {_f(r['rsi'][1])} + {_f(r['rse'][1])}"
        else:
            rs_txt = f"{_f(r['rsi'])} + {_f(r['rse'])}"
        mark = "†" if r["cat"] in GROUND_CATS and r["kind"] == "layers" else ""
        notes = [r["note"]] if r.get("note") else []
        if r.get("pairs"):
            notes.append("from the adjacent zone: " + ", ".join(f"'{p}'" for p in sorted(r["pairs"])))
        note = "".join(f"<div class='note'>{_e(n)}</div>" for n in notes)
        body.append(
            f"<tr><td class='l'>{_e(r['cons'])}{note}</td><td class='l'>{_e(origin_text(r))}</td><td>{r['n']}</td>"
            f"<td>{_f(r['area'], 1)}</td><td>{_f(r['thickness'], 3)}</td><td>{_f(r['R'], 3) if r['kind'] == 'layers' else '—'}</td>"
            f"<td>{rs_txt}</td><td class='u'>{_f(r.get('U'), 3)}{mark}</td><td>{_f(ashrae, 3)}</td></tr>")
    otable = ("<div class='scroll'><table><thead><tr><th class='l'>Construction</th><th class='l'>Origin</th>"
              "<th>No. of surfaces</th>"
              "<th>Area [m²]</th><th>Thickness [m]</th><th>Layers R [m²K/W]</th><th>Rsi + Rse [m²K/W]</th>"
              "<th>U ISO 6946 [W/m²K]</th><th>U with ASHRAE films<br>(OpenStudio / EnergyPlus)</th></tr></thead>"
              f"<tbody>{''.join(body)}</tbody></table></div>") if opq else "<p>No opaque elements.</p>"

    gbody, last = [], None
    for r in glz:
        if r["cat"] != last:
            gbody.append(f"<tr class='cat'><td colspan='9'>{_e(r['cat'])}</td></tr>")
            last = r["cat"]
        notes = [r["note"]] if r.get("note") else []
        if r["frames"]:
            notes.append(f"{r['frames']} of {r['n']} windows have a frame (not included in the U-value)")
        note = "".join(f"<div class='note'>{_e(n)}</div>" for n in notes)
        gbody.append(
            f"<tr><td class='l'>{_e(r['cons'])}{note}</td><td class='l'>{_e(r['comp'])}</td>"
            f"<td class='l'>{_e(origin_text(r))}</td><td>{r['n']}</td>"
            f"<td>{_f(r['area'], 1)}</td><td class='u'>{_f(r['U_ep'], 3)}</td><td>{_f(r['Ug'], 2)}</td>"
            f"<td>{_f(r['g'], 2)}</td><td>{_f(r['vt'], 2)}</td></tr>")
    gtable = ("<div class='scroll'><table><thead><tr><th class='l'>Construction</th><th class='l'>Composition "
              "(outside → inside)</th><th class='l'>Origin</th><th>No. of windows</th><th>Area [m²]</th>"
              "<th>Glass U EnergyPlus<br>(ISO 15099, NFRC) [W/m²K]</th><th>Ug EN 673<br>[W/m²K]</th><th>SHGC</th>"
              "<th>Tvis</th></tr></thead>"
              f"<tbody>{''.join(gbody)}</tbody></table></div>") if glz else "<p>No glazed openings.</p>"

    det = []
    for name in sorted(details, key=str.lower):
        info = details[name]
        if not info["layers"]:
            continue
        rows = "".join(
            f"<tr><td class='l'>{i}</td><td class='l'>{_e(lay['name'])}</td><td class='l'>{_e(lay['kind'])}</td>"
            f"<td>{_f(lay['e'], 3)}</td><td>{_f(lay['lam'], 3)}</td><td>{_f(lay['R'], 3)}</td></tr>"
            for i, lay in enumerate(info["layers"], 1))
        det.append(f"<details><summary>{_e(name)} — layers R = {_f(info['R'], 3)} m²K/W</summary>"
                   "<div class='scroll'><table><thead><tr><th class='l'>#</th><th class='l'>Material</th>"
                   "<th class='l'>Type</th><th>e [m]</th><th>λ [W/m·K]</th><th>R [m²K/W]</th></tr></thead>"
                   f"<tbody>{rows}</tbody></table></div></details>")

    csv_head = ["Type", "Construction", "Composition", "Origin", "Count", "Area [m2]", "Thickness [m]",
                "Layers R [m2K/W]", "U ISO 6946 [W/m2K]", "U ASHRAE [W/m2K]", "Glass U EnergyPlus [W/m2K]",
                "Ug EN 673 [W/m2K]", "SHGC", "Tvis"]
    csv_rows = []
    for r in opq:
        u = r.get("U")
        csv_rows.append([r["cat"], r["cons"], "", origin_text(r), r["n"], round(r["area"], 2),
                         None if r["thickness"] is None else round(r["thickness"], 4),
                         round(r["R"], 4) if r["kind"] == "layers" else None,
                         None if u is None else (" / ".join(f"{x:.3f}" for x in u)
                                                 if isinstance(u, tuple) else round(u, 4)),
                         round(r["ashrae_ua"] / r["ashrae_a"], 4) if r["ashrae_a"] > 0 else None,
                         None, None, None, None])
    for r in glz:
        csv_rows.append([r["cat"], r["cons"], r["comp"], origin_text(r), r["n"], round(r["area"], 2), None, None,
                         None, None,
                         None if r["U_ep"] is None else round(r["U_ep"], 4),
                         None if r["Ug"] is None else round(r["Ug"], 4),
                         None if r["g"] is None else round(r["g"], 3), None if r["vt"] is None else round(r["vt"], 3)])
    data = json.dumps(dict(head=csv_head, rows=csv_rows, file=meta["csv_name"]), ensure_ascii=False)
    data = data.replace("</", "<\\/")

    issues_html = ("<h2>Warnings</h2><ul>" + "".join(f"<li class='warn'>{_e(i)}</li>" for i in issues) + "</ul>") \
        if issues else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head>
<body><main>
<h1>Thermal transmittance of the model</h1>
<p class="sub">{_e(meta['building'])} · {_e(meta['date'])} · OpenStudio {_e(meta['os'])} (EnergyPlus {_e(meta['ep'])})</p>
<div class="bar"><button onclick="csv(',', '.')">Download CSV</button>
<button onclick="csv(';', ',')">Download CSV (; and decimal comma)</button>
<button onclick="window.print()">Print / PDF</button></div>
<p class="note">Only the constructions applied to some surface or sub-surface of the model: the one assigned to the surface
if any, otherwise the one it receives from its construction set ("Origin" column). Areas net of openings and multiplied
by the zone multiplier.</p>
<h2>Summary by element type</h2>
<div class="scroll"><table><thead><tr><th class="l">Type</th><th>No. of constructions</th><th>Area [m²]</th>
<th>Area-weighted U [W/m²K]</th></tr></thead><tbody>{srows}</tbody></table></div>
<p class="note">Area-weighted average: opaque elements with the ISO 6946 U-value; glazing with the EnergyPlus U-value. No
thermal bridges or frames. Horizontal partitions: upward / downward heat flow. † Without the ground (see the note of the
opaque table).</p>
<h2>Opaque elements</h2>
{otable}
<p class="note">U ISO 6946 = 1 / (Rsi + Σ e/λ + Rse) with the surface resistances of EN ISO 6946 (the same values as the
Spanish CTE, DA DB-HE/1): horizontal heat flow 0.13 / 0.04; upward 0.10 / 0.04; downward 0.17 / 0.04 (inside / outside);
for interior partitions, Rsi on both sides. † In contact with the ground: U-value of the construction with Rsi and
without the ground (national methods such as ISO 13370 or the CTE compute the U-value of slabs and basement walls with
the ground, according to their geometry and insulation). The U-value with ASHRAE films is the one computed by OpenStudio
and published in the EnergyPlus report (Envelope Summary); EnergyPlus uses neither of them in the simulation, because
it computes the convection and radiation of each surface at every time step.</p>
<h2>Glazed openings</h2>
{gtable}
<p class="note">Simple Glazing System: U-factor, SHGC and visible transmittance of the object itself. Layered glazing:
centre-of-glass U-value, without frame. EnergyPlus computes it with ISO 15099 under NFRC winter conditions (21 °C inside,
−18 °C outside, outside convection 26 W/m²K): it is the "Glass U-Factor" of its report. The Ug of EN 673 (ΔT = 15 K,
Tm = 10 °C, he = 25 W/m²K) is the value declared by glass manufacturers and required by European building codes; it is
published rounded to one decimal place.</p>
{issues_html}
<h2>Composition of the opaque elements</h2>
{''.join(det) if det else '<p>—</p>'}
<script type="application/json" id="report-data">{data}</script>
<script>{JS}</script>
</main></body></html>"""


def _safe(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "model"


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------


class TransmittanceInfo(openstudio.measure.ModelMeasure):
    """U-values of the constructions and glazing applied in the model."""

    def name(self):
        """Returns the human readable name."""
        return "Transmittance_info"

    def description(self):
        """Human readable description."""
        return (
            "Computes, without simulating, the thermal transmittance (U-value) of the opaque constructions and glazing "
            "applied in the model and opens a report in the web browser. Use it with 'Apply Measure Now' (Components & "
            "Measures menu). It does not modify the model."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Goes through Surfaces and SubSurfaces and takes their effective construction (including those from Default "
            "Construction Sets). Opaque: U = 1/(Rsi + Σe/λ + Rse) with Rsi/Rse from EN ISO 6946 / CTE DA DB-HE/1 "
            "according to the tilt (horizontal flow if 60° ≤ tilt ≤ 120°) and the boundary condition (ground: Rse = 0, "
            "ground not included; partitions: Rsi on both sides); also the U-value with the ASHRAE films of the "
            "EnergyPlus Envelope Summary. F/C-factor: equivalent U = F·P/A and C. Glazing: SimpleGlazing with its own "
            "values; layered glazing with the ISO 15099 centre-of-glass U-value under NFRC winter conditions "
            "(21/−18 °C, hc,out = 26 W/m²K, ISO 15099 inside convection with a 1 m height, gases and mixtures with the "
            "EnergyPlus coefficients) and the EN 673 Ug. Net areas × zone multiplier; paired partitions are counted "
            "once. The HTML report is saved in the chosen folder (or in the system temporary folder) and opened with "
            "the default web browser."
        )

    def arguments(self, model: typing.Optional[openstudio.model.Model] = None):
        """Prepares user arguments for the measure."""
        args = openstudio.measure.OSArgumentVector()

        a = openstudio.measure.OSArgument.makeBoolArgument("open_browser", True)
        a.setDisplayName("Open the report in the web browser when finished")
        a.setDefaultValue(True)
        args.append(a)

        a = openstudio.measure.OSArgument.makeStringArgument("output_folder", False)
        a.setDisplayName("Folder where the report is saved (empty = system temporary folder)")
        a.setDescription("For example C:\\Users\\name\\Documents. The report path is shown in the measure messages.")
        a.setDefaultValue("")
        args.append(a)

        a = openstudio.measure.OSArgument.makeBoolArgument("include_interior", True)
        a.setDisplayName("Include interior partitions, doors and windows")
        a.setDefaultValue(True)
        args.append(a)

        return args

    def run(
        self,
        model: openstudio.model.Model,
        runner: openstudio.measure.OSRunner,
        user_arguments: openstudio.measure.OSArgumentMap,
    ):
        """Defines what happens when the measure is run."""
        super().run(model, runner, user_arguments)  # Do **NOT** remove this line

        if not (runner.validateUserArguments(self.arguments(model), user_arguments)):
            return False

        open_browser = runner.getBoolArgumentValue("open_browser", user_arguments)
        folder = runner.getStringArgumentValue("output_folder", user_arguments).strip().strip('"')
        interior = runner.getBoolArgumentValue("include_interior", user_arguments)

        opaque, glazing, details, issues = collect(model, include_interior=interior)
        n_cons = len({r["cons"] for r in opaque.values()} | {r["cons"] for r in glazing.values()})
        runner.registerInitialCondition(f"The model has {n_cons} constructions applied to surfaces or sub-surfaces.")
        if not opaque and not glazing:
            runner.registerAsNotApplicable("The model has no surfaces with a construction.")
            return True
        for i in issues:
            runner.registerWarning(i)

        # summary in the messages (in case the browser does not open)
        for r in sorted(opaque.values(), key=_cat_key):
            u = r.get("U")
            utxt = "not computable" if u is None else (
                f"U↑ {u[0]:.3f} / U↓ {u[1]:.3f}" if isinstance(u, tuple) else f"U = {u:.3f}")
            runner.registerInfo(f"{r['cat']} | {r['cons']}: {utxt} W/m²K ({r['area']:.1f} m²)")
        for r in sorted(glazing.values(), key=_cat_key):
            utxt = "not computable" if r["U_ep"] is None else f"U = {r['U_ep']:.3f}"
            ug = "" if r["Ug"] is None else f", Ug EN 673 = {r['Ug']:.2f}"
            runner.registerInfo(f"{r['cat']} | {r['cons']}: {utxt}{ug} W/m²K ({r['area']:.1f} m²)")

        b = model.building()  # without creating the Building object if it does not exist (the model is not modified)
        building = b.get().nameString() if b.is_initialized() else "Model"
        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        meta = dict(building=building, date=now.strftime("%Y-%m-%d %H:%M"), os=openstudio.openStudioVersion(),
                    ep=openstudio.energyPlusVersion(), csv_name=f"u_values_{_safe(building)}_{stamp}.csv")
        page = build_html(f"U-values · {building}", opaque, glazing, details, issues, meta)

        # report: chosen (or temporary) folder + report.html in the measure run folder
        try:
            out_dir = Path(folder) if folder else Path(tempfile.gettempdir()) / "OpenStudio_U_values"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"u_values_{_safe(building)}_{stamp}.html"
            path.write_text(page, encoding="utf-8")
        except OSError as e:
            runner.registerError(f"The report could not be saved in '{folder or tempfile.gettempdir()}': {e}")
            return False
        try:
            Path("report.html").write_text(page, encoding="utf-8")
        except OSError:
            pass

        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(path.resolve().as_uri())
            except Exception:  # noqa: BLE001 - no browser available
                opened = False
        runner.registerInfo(f"Report: {path}")
        if open_browser and not opened:
            runner.registerWarning(f"The web browser could not be opened. Open the report manually: {path}")
        runner.registerFinalCondition(f"U-value report saved in {path}. The model has not been modified.")
        return True


# register the measure to be used by the application
TransmittanceInfo().registerWithApplication()

"""Transmitancias térmicas de los cerramientos y huecos del modelo. ModelMeasure para OpenStudio 3.11.

Pensada para «Apply Measure Now» (menú Components & Measures): calcula, sin simular, la transmitancia U de las
construcciones que están aplicadas en el modelo (directamente o mediante conjuntos de construcciones) y abre un
informe HTML en el navegador. No modifica el modelo.

  * Cerramientos opacos: U = 1 / (Rsi + Σ e/λ + Rse) con las resistencias superficiales de UNE-EN ISO 6946
    (las del DA DB-HE/1 del CTE), según la posición del cerramiento y el sentido del flujo de calor. Se añade, para
    comparar, la U con las películas de ASHRAE que usa OpenStudio y que publica el informe de EnergyPlus.
  * Vidrios «Simple Glazing System»: la U del propio objeto.
  * Vidrios por capas: la U del vidrio (centro del vidrio) como la calcula EnergyPlus (ISO 15099, condiciones de
    invierno NFRC: 21 ºC interior, -18 ºC exterior, hc exterior 26 W/m²K) y la Ug de UNE-EN 673 (la que declaran
    los fabricantes). No incluye el marco.

Probada con OpenStudio 3.11.0 (EnergyPlus 25.2.0).
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
# Constantes
# ---------------------------------------------------------------------------

SIGMA = 5.670374419e-8  # W/m²K⁴
GRAV = 9.81  # m/s²
R_GAS = 8314.462618  # J/kmol·K
P_ATM = 101325.0  # Pa
KELVIN = 273.15

# Resistencias superficiales (m²K/W), UNE-EN ISO 6946 / DA DB-HE/1
RSI = {"h": 0.13, "up": 0.10, "down": 0.17}
RSE_EXT = 0.04
FLOW_LABEL = {"h": "horizontal", "up": "ascendente", "down": "descendente"}
# Películas de ASHRAE 90.1 (apéndice A) que usa el informe Envelope Summary de EnergyPlus (m²K/W)
IP_R = 0.176110  # h·ft²·ºF/Btu -> m²K/W
ASHRAE_RSI = {"Wall": 0.68 * IP_R, "RoofCeiling": 0.61 * IP_R, "Floor": 0.92 * IP_R}
ASHRAE_RSE = 0.17 * IP_R

# Gases según ISO 15099, los mismos coeficientes que EnergyPlus: (k: a, b), (μ: a, b), (cp: a, b), M
# k = a + b·T [W/m·K], μ = a + b·T [Pa·s], cp = a + b·T [J/kg·K], T en K
GASES_15099 = {
    "Air": ((2.873e-3, 7.760e-5), (3.723e-6, 4.940e-8), (1002.737, 1.2324e-2), 28.97),
    "Argon": ((2.285e-3, 5.149e-5), (3.379e-6, 6.451e-8), (521.9285, 0.0), 39.948),
    "Krypton": ((9.443e-4, 2.826e-5), (2.213e-6, 7.777e-8), (248.0907, 0.0), 83.8),
    "Xenon": ((4.538e-4, 1.723e-5), (1.069e-6, 7.414e-8), (158.3397, 0.0), 131.3),
}
# Gases según UNE-EN 673 (10 ºC): ρ [kg/m³], μ [Pa·s], λ [W/m·K], c [J/kg·K]
GASES_EN673 = {
    "Air": (1.232, 1.761e-5, 2.496e-2, 1008.0),
    "Argon": (1.699, 2.164e-5, 1.684e-2, 519.0),
    "Krypton": (3.56, 2.4e-5, 0.900e-2, 245.0),
    "Xenon": (5.689, 2.226e-5, 0.529e-2, 161.0),
}
GAS_ES = {"Air": "aire", "Argon": "argón", "Krypton": "kriptón", "Xenon": "xenón", "Custom": "gas personalizado"}

# Condiciones de invierno NFRC (EnergyPlus, U nominal del vidrio)
NFRC_T_IN, NFRC_T_OUT, NFRC_HC_OUT = 21.0 + KELVIN, -18.0 + KELVIN, 26.0
NOMINAL_HEIGHT = 1.0  # m, altura del vidrio para la convección (cálculo nominal)

GROUND_BCS = ("Ground", "GroundFCfactorMethod", "Foundation", "GroundSlabPreprocessorAverage",
              "GroundSlabPreprocessorCore", "GroundSlabPreprocessorPerimeter",
              "GroundBasementPreprocessorAverageWall", "GroundBasementPreprocessorAverageFloor",
              "GroundBasementPreprocessorUpperWall", "GroundBasementPreprocessorLowerWall")
GLAZED_SUBSURFACES = {"FixedWindow": "Ventana", "OperableWindow": "Ventana", "GlassDoor": "Puerta acristalada",
                      "Skylight": "Lucernario", "TubularDaylightDome": "Lucernario tubular",
                      "TubularDaylightDiffuser": "Lucernario tubular"}

CATEGORY_ORDER = ["Fachada", "Cubierta", "Suelo en contacto con el aire exterior",
                  "Muro en contacto con el terreno", "Solera / suelo en contacto con el terreno",
                  "Cubierta enterrada", "Partición interior vertical", "Partición interior horizontal",
                  "Puerta exterior", "Puerta interior",
                  "Ventana", "Puerta acristalada", "Lucernario", "Lucernario tubular", "Hueco interior"]


# ---------------------------------------------------------------------------
# Cerramientos opacos
# ---------------------------------------------------------------------------


def flow_direction(tilt_deg):
    """Sentido del flujo (de dentro a fuera) según la inclinación de la normal exterior respecto a la vertical."""
    if 60.0 <= tilt_deg <= 120.0:
        return "h"
    return "up" if tilt_deg < 60.0 else "down"


def opaque_layer(material):
    """dict(nombre, tipo, e, λ, R) de una capa opaca, o None si no se puede calcular."""
    name = material.nameString()
    o = material.to_StandardOpaqueMaterial()
    if o.is_initialized():
        m = o.get()
        return dict(name=name, kind="Material", e=m.thickness(), lam=m.thermalConductivity(),
                    R=m.thickness() / m.thermalConductivity())
    o = material.to_MasslessOpaqueMaterial()
    if o.is_initialized():
        return dict(name=name, kind="Sin masa (R)", e=None, lam=None, R=o.get().thermalResistance())
    o = material.to_AirGap()
    if o.is_initialized():
        return dict(name=name, kind="Cámara de aire (R)", e=None, lam=None, R=o.get().thermalResistance())
    o = material.to_InfraredTransparentMaterial()
    if o.is_initialized():
        return dict(name=name, kind="Transparente al infrarrojo", e=None, lam=None, R=0.0)
    o = material.to_RoofVegetation()
    if o.is_initialized():
        m = o.get()
        return dict(name=name, kind="Cubierta vegetal (suelo seco)", e=m.thickness(), lam=m.conductivityofDrySoil(),
                    R=m.thickness() / m.conductivityofDrySoil())
    return None


def opaque_u(r_layers, rsi, rse):
    return 1.0 / (rsi + r_layers + rse)


def ashrae_films(surface_type, exterior, ground):
    """Rsi + Rse de ASHRAE como en el Envelope Summary de EnergyPlus (por tipo de superficie, no por inclinación)."""
    rsi = ASHRAE_RSI.get(surface_type, ASHRAE_RSI["Wall"])
    if exterior:
        return rsi + ASHRAE_RSE
    if ground:
        return rsi
    if surface_type == "Wall":
        return 2 * ASHRAE_RSI["Wall"]
    return ASHRAE_RSI["RoofCeiling"] + ASHRAE_RSI["Floor"]  # forjado interior: una cara ascendente y otra descendente


# ---------------------------------------------------------------------------
# Gases
# ---------------------------------------------------------------------------


def _pure_15099(gas, t):
    """(k, μ, cp, M) de un gas puro a la temperatura t [K]. gas: nombre o dict de coeficientes (gas personalizado)."""
    if isinstance(gas, str):
        (ka, kb), (ma, mb), (ca, cb), mw = GASES_15099[gas]
        return ka + kb * t, ma + mb * t, ca + cb * t, mw
    k = gas["k"][0] + gas["k"][1] * t + gas["k"][2] * t * t
    mu = gas["mu"][0] + gas["mu"][1] * t + gas["mu"][2] * t * t
    cp = gas["cp"][0] + gas["cp"][1] * t + gas["cp"][2] * t * t
    return k, mu, cp, gas["M"]


def gas_properties_15099(mix, t):
    """(k, μ, cp, ρ) de una mezcla [(fracción, gas)] a t [K], con las reglas de mezcla de ISO 15099."""
    comps = [(x,) + _pure_15099(g, t) for x, g in mix if x > 0]
    m_mix = sum(x * mw for x, _, _, _, mw in comps)
    rho = P_ATM * m_mix / (R_GAS * t)
    if len(comps) == 1:
        _, k, mu, cp, _ = comps[0]
        return k, mu, cp, rho
    # viscosidad
    mu_mix = 0.0
    for i, (xi, _, mui, _, mi) in enumerate(comps):
        s = 0.0
        for j, (xj, _, muj, _, mj) in enumerate(comps):
            if i != j:
                phi = (1 + math.sqrt(mui / muj) * (mj / mi) ** 0.25) ** 2 / (math.sqrt(8) * math.sqrt(1 + mi / mj))
                s += phi * xj / xi
        mu_mix += mui / (1 + s)
    # conductividad: parte monoatómica (k') y resto (k'')
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
    """(ρ, μ, λ, c) de una mezcla según UNE-EN 673 (10 ºC, ponderación lineal por fracción en volumen)."""
    out = [0.0, 0.0, 0.0, 0.0]
    for x, g in mix:
        if isinstance(g, str):
            vals = GASES_EN673[g]
        else:  # gas personalizado: coeficientes de ISO 15099 a 10 ºC
            k, mu, cp, rho = gas_properties_15099([(1.0, g)], 283.15)
            vals = (rho, mu, k, cp)
        for i in range(4):
            out[i] += x * vals[i]
    return tuple(out)


# ---------------------------------------------------------------------------
# Vidrios por capas
# ---------------------------------------------------------------------------


def _gap_nusselt_15099(ra, aspect):
    """Número de Nusselt de una cámara vertical (ISO 15099, inclinación 90º)."""
    if ra > 5.0e4:
        nu1 = 0.0673838 * ra ** (1.0 / 3.0)
    elif ra > 1.0e4:
        nu1 = 0.028154 * ra ** 0.4134
    else:
        nu1 = 1.0 + 1.7596678e-10 * ra ** 2.2984755
    nu2 = 0.242 * (ra / aspect) ** 0.272
    return max(nu1, nu2)


def _inside_hc_15099(t_surf, t_air, height=NOMINAL_HEIGHT):
    """Convección natural en la cara interior de un vidrio vertical (ISO 15099, apartado 8.3.2.2)."""
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
    """U del centro del vidrio [W/m²K] en condiciones nominales, como EnergyPlus (ISO 15099).

    layers: de fuera a dentro; vidrio = dict(kind="glass", t, k, ef, eb); cámara = dict(kind="gas", t, mix).
    """
    n = len(layers)
    if n == 0 or layers[0]["kind"] != "glass" or layers[-1]["kind"] != "glass":
        return None
    # temperaturas de las caras: 2 por vidrio, en orden de fuera a dentro
    glasses = [i for i, lay in enumerate(layers) if lay["kind"] == "glass"]
    nfaces = 2 * len(glasses)
    temps = [t_out + (t_in - t_out) * (f + 1) / (nfaces + 1) for f in range(nfaces)]
    u = None
    for _ in range(200):
        res = []  # resistencias en serie
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
        # nuevas temperaturas de las caras, desde el exterior
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
    """Ug [W/m²K] según UNE-EN 673 (vidrio vertical, ΔT = 15 K, Tm = 283 K, he = 25 W/m²K)."""
    if not layers or layers[0]["kind"] != "glass" or layers[-1]["kind"] != "glass":
        return None
    r_glass = sum(lay["t"] * 1.0 for lay in layers if lay["kind"] == "glass")  # resistividad del vidrio 1 m·K/W
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
    hi = 3.6 + 4.1 * layers[-1]["eb"] / 0.837  # 7,7 W/m²K con vidrio sin capa en la cara interior
    return 1.0 / (1.0 / 25.0 + r_glass + sum(r_gaps) + 1.0 / hi)


def glazing_layers(construction):
    """(capas para el cálculo, texto de composición, aviso) de una construcción de vidrio por capas."""
    out, text = [], []
    for mat in construction.layers():
        g = mat.to_StandardGlazing()
        if g.is_initialized():
            g = g.get()
            ef, eb = g.frontSideInfraredHemisphericalEmissivity(), g.backSideInfraredHemisphericalEmissivity()
            out.append(dict(kind="glass", t=g.thickness(), k=g.thermalConductivity(), ef=ef, eb=eb))
            coat = f" (ε {min(ef, eb):.2f})".replace(".", ",") if min(ef, eb) < 0.5 else ""
            text.append(f"vidrio {g.thickness() * 1000:.0f} mm{coat}")
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
            text.append(f"{g.thickness() * 1000:.0f} mm {GAS_ES.get(gt, gt)}")
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
            desc = " + ".join(f"{fr * 100:.0f}% {GAS_ES.get(gt, gt)}" for fr, gt in mix)
            text.append(f"{g.thickness() * 1000:.0f} mm ({desc})")
            continue
        return None, " / ".join(text + [mat.nameString()]), (
            f"capa «{mat.nameString()}» ({mat.iddObjectType().valueName().replace('OS_', '')}) no admitida: "
            "dispositivos de sombra, vidrios termocrómicos o de otro tipo")
    return out, " / ".join(text), ""


# ---------------------------------------------------------------------------
# Recorrido del modelo
# ---------------------------------------------------------------------------


def _multiplier(planar):
    sp = planar.space()
    return sp.get().multiplier() if sp.is_initialized() else 1


def _deg(rad):
    return math.degrees(rad)


def collect(model, include_interior=True):
    """Datos de los cerramientos opacos y de los huecos con su construcción aplicada."""
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
            issues.append(f"La superficie «{s.nameString()}» no tiene construcción.")
            continue
        cons = oc.get()
        bc = s.outsideBoundaryCondition()
        flow = flow_direction(_deg(s.tilt()))
        mult = _multiplier(s)
        area = s.netArea() * mult
        pair = None
        if bc == "Outdoors":
            cat = {"h": "Fachada", "up": "Cubierta", "down": "Suelo en contacto con el aire exterior"}[flow]
            rs = (RSI[flow], RSE_EXT)
        elif bc in GROUND_BCS:
            cat = {"h": "Muro en contacto con el terreno", "down": "Solera / suelo en contacto con el terreno",
                   "up": "Cubierta enterrada"}[flow]
            rs = (RSI[flow], 0.0)
        else:  # Surface, Adiabatic, OtherSide...
            if not include_interior:
                continue
            adj = s.adjacentSurface()
            if adj.is_initialized():
                if str(adj.get().handle()) < str(s.handle()):
                    continue  # la pareja de esta partición ya se ha contado (misma área física)
                ac = adj.get().construction()
                pair = ac.get().nameString() if ac.is_initialized() else None
            if flow == "h":
                cat, rs = "Partición interior vertical", (RSI["h"], RSI["h"])
            else:
                cat, rs, flow = "Partición interior horizontal", None, "both"
        films = ashrae_films(s.surfaceType(), bc == "Outdoors", bc in GROUND_BCS)
        add_opaque(cons, cat, area, rs, films, flow, s.isConstructionDefaulted(), pair)

    for ss in sorted(model.getSubSurfaces(), key=lambda x: x.nameString()):
        oc = ss.construction()
        if not oc.is_initialized():
            issues.append(f"El hueco «{ss.nameString()}» no tiene construcción.")
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
            cat = GLAZED_SUBSURFACES.get(st, "Ventana") if exterior else "Hueco interior"
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
                cat, rs = "Puerta exterior", (RSI[flow], RSE_EXT)
            else:
                cat, rs = "Puerta interior", (RSI[flow], RSI[flow])
            ptype = parent.get().surfaceType() if parent.is_initialized() else "Wall"
            films = ashrae_films(ptype, exterior, bc in GROUND_BCS) if (exterior or ptype == "Wall") else \
                2 * ASHRAE_RSI["Wall"]
            add_opaque(cons, cat, area, rs, films, flow, ss.isConstructionDefaulted())
    return opaque, glazing, details, issues


def construction_info(cons):
    """Capas y resistencia de una construcción opaca (o su F/C-factor)."""
    ff = cons.to_FFactorGroundFloorConstruction()
    if ff.is_initialized():
        ff = ff.get()
        u_eq = ff.fFactor() * ff.perimeterExposed() / ff.area() if ff.area() > 0 else None
        return dict(kind="ffactor", layers=[], R=0.0, thickness=None, U_eq=u_eq,
                    note=f"F-factor {ff.fFactor():.3f} W/m·K; U equivalente = F·P/A "
                         f"(P = {ff.perimeterExposed():.1f} m, A = {ff.area():.1f} m²), incluye el terreno")
    cf = cons.to_CFactorUndergroundWallConstruction()
    if cf.is_initialized():
        cf = cf.get()
        return dict(kind="cfactor", layers=[], R=1.0 / cf.cFactor(), thickness=None, U_eq=None,
                    note=f"C-factor {cf.cFactor():.3f} W/m²K (sin películas ni terreno), altura {cf.height():.2f} m")
    if cons.to_ConstructionAirBoundary().is_initialized():
        return dict(kind="other", layers=[], R=None, thickness=None, U_eq=None,
                    note="límite de aire (Construction:AirBoundary): separa zonas sin cerramiento físico")
    lc = cons.to_LayeredConstruction()
    if not lc.is_initialized():
        return dict(kind="other", layers=[], R=None, thickness=None, U_eq=None,
                    note=f"tipo de construcción no calculable ({cons.iddObjectType().valueName().replace('OS_', '')})")
    layers, unknown = [], []
    for mat in lc.get().layers():
        info = opaque_layer(mat)
        if info is None:
            unknown.append(mat.nameString())
        else:
            layers.append(info)
    if unknown:
        return dict(kind="other", layers=layers, R=None, thickness=None, U_eq=None,
                    note="capas no admitidas: " + ", ".join(unknown))
    thickness = sum(lay["e"] for lay in layers if lay["e"] is not None)
    return dict(kind="layers", layers=layers, R=sum(lay["R"] for lay in layers), thickness=thickness, U_eq=None,
                note="")


def glazing_info(cons):
    """U del vidrio (EnergyPlus / UNE-EN 673), g y Tv de una construcción de hueco."""
    lc = cons.to_LayeredConstruction()
    layers = list(lc.get().layers()) if lc.is_initialized() else []
    if len(layers) == 1 and layers[0].to_SimpleGlazing().is_initialized():
        sg = layers[0].to_SimpleGlazing().get()
        vt = sg.visibleTransmittance()
        return dict(kind="Simple Glazing System", U_ep=sg.uFactor(), Ug=None, g=sg.solarHeatGainCoefficient(),
                    vt=vt.get() if vt.is_initialized() else None,
                    comp=f"U {sg.uFactor():.2f}, SHGC {sg.solarHeatGainCoefficient():.2f}".replace(".", ","),
                    note="valores introducidos en el objeto (si se definieron así, son los del hueco completo)")
    if not lc.is_initialized():
        return dict(kind="No calculable", U_ep=None, Ug=None, g=None, vt=None, comp="",
                    note=f"tipo {cons.iddObjectType().valueName().replace('OS_', '')}")
    calc, comp, note = glazing_layers(lc.get())
    if calc is None:
        return dict(kind="No calculable", U_ep=None, Ug=None, g=None, vt=None, comp=comp, note=note)
    if any(lay["kind"] == "gas" for i, lay in enumerate(calc) if i in (0, len(calc) - 1)) or \
            any(a["kind"] == b["kind"] for a, b in zip(calc, calc[1:])):
        return dict(kind="No calculable", U_ep=None, Ug=None, g=None, vt=None, comp=comp,
                    note="la composición debe alternar vidrio y cámara, con vidrio en ambas caras")
    return dict(kind="Por capas", U_ep=u_glass_15099(calc), Ug=ug_en673(calc), g=None, vt=None, comp=comp,
                note="")


# ---------------------------------------------------------------------------
# Informe HTML
# ---------------------------------------------------------------------------


def _f(v, d=2):
    if v is None:
        return "—"
    if isinstance(v, tuple):
        return " / ".join(_f(x, d) for x in v)
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def _e(s):
    return html.escape(str(s))


def origin_text(row):
    """Origen de la construcción: conjunto de construcciones (por defecto) o asignada a la superficie."""
    a, b = row.get("n_set", 0), row.get("n_hand", 0)
    if a and b:
        return f"Conjunto ({a}) y asignada ({b})"
    return "Conjunto" if a else "Asignada"


def _cat_key(row):
    c = row["cat"]
    return (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, row["cons"].lower())


def summary_rows(opaque, glazing):
    """Por categoría: nº de construcciones, área y U media ponderada por área."""
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
            acc["U"] = (a, b) if acc["cat"] == "Partición interior horizontal" else a
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

JS = """
function csv(){
  const d = JSON.parse(document.getElementById('datos').textContent);
  const rows = [d.head].concat(d.rows);
  const txt = rows.map(r => r.map(v => {
      if (v === null || v === undefined) return '';
      if (typeof v === 'number') return String(v).replace('.', ',');
      const s = String(v); return /[;"\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }).join(';')).join('\\r\\n');
  const blob = new Blob(['\\ufeff' + txt], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = d.file; a.click();
}
"""


def build_html(title, opaque, glazing, details, issues, meta):
    opq = sorted(opaque.values(), key=_cat_key)
    glz = sorted(glazing.values(), key=_cat_key)
    summ = summary_rows(opaque, glazing)

    ground_cats = ("Muro en contacto con el terreno", "Solera / suelo en contacto con el terreno", "Cubierta enterrada")
    srows = "".join(
        f"<tr><td>{_e(r['cat'])}</td><td>{r['n']}</td><td>{_f(r['area'], 1)}</td><td class='u'>{_f(r['U'], 2)}"
        f"{'†' if r['cat'] in ground_cats and r['U'] is not None else ''}</td></tr>" for r in summ)

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
        mark = "†" if r["cat"] in ("Muro en contacto con el terreno", "Solera / suelo en contacto con el terreno",
                                    "Cubierta enterrada") and r["kind"] == "layers" else ""
        notes = [r["note"]] if r.get("note") else []
        if r.get("pairs"):
            notes.append("desde la zona contigua: " + ", ".join(f"«{p}»" for p in sorted(r["pairs"])))
        note = "".join(f"<div class='note'>{_e(n)}</div>" for n in notes)
        body.append(
            f"<tr><td class='l'>{_e(r['cons'])}{note}</td><td class='l'>{_e(origin_text(r))}</td><td>{r['n']}</td>"
            f"<td>{_f(r['area'], 1)}</td><td>{_f(r['thickness'], 3)}</td><td>{_f(r['R'], 3) if r['kind'] == 'layers' else '—'}</td>"
            f"<td>{rs_txt}</td><td class='u'>{_f(r.get('U'), 3)}{mark}</td><td>{_f(ashrae, 3)}</td></tr>")
    otable = ("<div class='scroll'><table><thead><tr><th class='l'>Construcción</th><th class='l'>Origen</th>"
              "<th>Nº sup.</th>"
              "<th>Área [m²]</th><th>Espesor [m]</th><th>R capas [m²K/W]</th><th>Rsi + Rse [m²K/W]</th>"
              "<th>U CTE / ISO 6946 [W/m²K]</th><th>U películas ASHRAE<br>(OpenStudio / EnergyPlus)</th></tr></thead>"
              f"<tbody>{''.join(body)}</tbody></table></div>") if opq else "<p>No hay cerramientos opacos.</p>"

    gbody, last = [], None
    for r in glz:
        if r["cat"] != last:
            gbody.append(f"<tr class='cat'><td colspan='9'>{_e(r['cat'])}</td></tr>")
            last = r["cat"]
        notes = [r["note"]] if r.get("note") else []
        if r["frames"]:
            notes.append(f"{r['frames']} de {r['n']} huecos tienen marco (no incluido en la U)")
        note = "".join(f"<div class='note'>{_e(n)}</div>" for n in notes)
        gbody.append(
            f"<tr><td class='l'>{_e(r['cons'])}{note}</td><td class='l'>{_e(r['comp'])}</td>"
            f"<td class='l'>{_e(origin_text(r))}</td><td>{r['n']}</td>"
            f"<td>{_f(r['area'], 1)}</td><td class='u'>{_f(r['U_ep'], 3)}</td><td>{_f(r['Ug'], 2)}</td>"
            f"<td>{_f(r['g'], 2)}</td><td>{_f(r['vt'], 2)}</td></tr>")
    gtable = ("<div class='scroll'><table><thead><tr><th class='l'>Construcción</th><th class='l'>Composición "
              "(exterior → interior)</th><th class='l'>Origen</th><th>Nº huecos</th><th>Área [m²]</th><th>U vidrio EnergyPlus<br>"
              "(ISO 15099, NFRC) [W/m²K]</th><th>Ug UNE-EN 673<br>[W/m²K]</th><th>g (SHGC)</th><th>Tv</th></tr></thead>"
              f"<tbody>{''.join(gbody)}</tbody></table></div>") if glz else "<p>No hay huecos acristalados.</p>"

    det = []
    for name in sorted(details, key=str.lower):
        info = details[name]
        if not info["layers"]:
            continue
        rows = "".join(
            f"<tr><td class='l'>{i}</td><td class='l'>{_e(lay['name'])}</td><td class='l'>{_e(lay['kind'])}</td>"
            f"<td>{_f(lay['e'], 3)}</td><td>{_f(lay['lam'], 3)}</td><td>{_f(lay['R'], 3)}</td></tr>"
            for i, lay in enumerate(info["layers"], 1))
        det.append(f"<details><summary>{_e(name)} — R capas = {_f(info['R'], 3)} m²K/W</summary>"
                   "<div class='scroll'><table><thead><tr><th class='l'>#</th><th class='l'>Material</th>"
                   "<th class='l'>Tipo</th><th>e [m]</th><th>λ [W/m·K]</th><th>R [m²K/W]</th></tr></thead>"
                   f"<tbody>{rows}</tbody></table></div></details>")

    csv_head = ["Tipo", "Construcción", "Composición", "Origen", "Nº", "Área [m2]", "Espesor [m]", "R capas [m2K/W]",
                "U CTE/ISO 6946 [W/m2K]", "U ASHRAE [W/m2K]", "U vidrio EnergyPlus [W/m2K]", "Ug EN 673 [W/m2K]",
                "g", "Tv"]
    csv_rows = []
    for r in opq:
        u = r.get("U")
        csv_rows.append([r["cat"], r["cons"], "", origin_text(r), r["n"], round(r["area"], 2),
                         None if r["thickness"] is None else round(r["thickness"], 4),
                         round(r["R"], 4) if r["kind"] == "layers" else None,
                         None if u is None else (" / ".join(f"{x:.3f}".replace(".", ",") for x in u)
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

    issues_html = ("<h2>Avisos</h2><ul>" + "".join(f"<li class='warn'>{_e(i)}</li>" for i in issues) + "</ul>") \
        if issues else ""

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head>
<body><main>
<h1>Transmitancias térmicas del modelo</h1>
<p class="sub">{_e(meta['building'])} · {_e(meta['date'])} · OpenStudio {_e(meta['os'])} (EnergyPlus {_e(meta['ep'])})</p>
<div class="bar"><button onclick="csv()">Descargar CSV</button><button onclick="window.print()">Imprimir / PDF</button></div>
<p class="note">Solo las construcciones aplicadas a alguna superficie o hueco del modelo: la asignada a la superficie si la
tiene y, si no, la que le corresponde por su conjunto de construcciones (columna «Origen»). Áreas netas de huecos y
multiplicadas por el multiplicador de la zona.</p>
<h2>Resumen por tipo de cerramiento</h2>
<div class="scroll"><table><thead><tr><th class="l">Tipo</th><th>Nº construcciones</th><th>Área [m²]</th>
<th>U media ponderada [W/m²K]</th></tr></thead><tbody>{srows}</tbody></table></div>
<p class="note">Media ponderada por área: opacos con la U del CTE / ISO 6946; huecos con la U de EnergyPlus. Sin puentes
térmicos ni marcos. En particiones horizontales: flujo ascendente / descendente. † Sin el terreno (ver nota de la tabla
de opacos).</p>
<h2>Cerramientos opacos</h2>
{otable}
<p class="note">U CTE / ISO 6946 = 1 / (Rsi + Σ e/λ + Rse) con las resistencias superficiales de UNE-EN ISO 6946 (DA DB-HE/1):
flujo horizontal 0,13 / 0,04; ascendente 0,10 / 0,04; descendente 0,17 / 0,04 (interior / exterior); en particiones
interiores, Rsi a ambos lados. † En contacto con el terreno: U de la construcción con Rsi y sin terreno (el CTE calcula
la Us de soleras y muros enterrados con el terreno, según su geometría y aislamiento). La U con películas ASHRAE es la que
calcula OpenStudio y la que publica el informe de EnergyPlus (Envelope Summary); EnergyPlus no usa ninguna de las dos en la
simulación, porque calcula la convección y la radiación de cada superficie en cada paso de tiempo.</p>
<h2>Huecos acristalados</h2>
{gtable}
<p class="note">Simple Glazing System: U, g y Tv del propio objeto. Vidrios por capas: U del centro del vidrio, sin marco.
EnergyPlus la calcula con ISO 15099 en condiciones de invierno NFRC (21 ºC interior, −18 ºC exterior, convección exterior
26 W/m²K): es la «Glass U-Factor» de su informe. La Ug de UNE-EN 673 (ΔT = 15 K, Tm = 10 ºC, he = 25 W/m²K) es la que
declaran los fabricantes y la que pide el CTE; se publica redondeada a una décima.</p>
{issues_html}
<h2>Composición de los cerramientos opacos</h2>
{''.join(det) if det else '<p>—</p>'}
<script type="application/json" id="datos">{data}</script>
<script>{JS}</script>
</main></body></html>"""


def _safe(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "modelo"


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------


class TransmittanceInfo(openstudio.measure.ModelMeasure):
    """Transmitancias de los cerramientos y huecos aplicados en el modelo."""

    def name(self):
        """Returns the human readable name."""
        return "Transmittance_info"

    def description(self):
        """Human readable description."""
        return (
            "Calcula, sin simular, la transmitancia térmica U de los cerramientos opacos y de los vidrios que están "
            "aplicados en el modelo y abre un informe en el navegador. Úsela con «Apply Measure Now» (menú Components & "
            "Measures). No modifica el modelo."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Recorre Surfaces y SubSurfaces y toma su construcción efectiva (incluidas las de los Default Construction "
            "Sets). Opacos: U = 1/(Rsi + Σe/λ + Rse) con Rsi/Rse de UNE-EN ISO 6946 / DA DB-HE/1 según la inclinación "
            "(flujo horizontal si 60º ≤ inclinación ≤ 120º) y la condición de contorno (terreno: Rse = 0, sin terreno; "
            "particiones: Rsi a ambos lados); también la U con películas ASHRAE de PlanarSurface::uFactor(). F/C-factor: "
            "U equivalente F·P/A y C. Huecos: SimpleGlazing con sus valores; vidrios por capas con la U del centro del "
            "vidrio de ISO 15099 en condiciones NFRC de invierno (21/−18 ºC, hc,e = 26 W/m²K, convección interior "
            "ISO 15099 con altura 1 m, gases y mezclas con los coeficientes de EnergyPlus) y la Ug de UNE-EN 673. Áreas "
            "netas × multiplicador de zona; las particiones emparejadas con la misma construcción se cuentan una vez. "
            "El informe HTML se guarda en la carpeta indicada (o en la temporal del sistema) y se abre con el navegador "
            "predeterminado."
        )

    def arguments(self, model: typing.Optional[openstudio.model.Model] = None):
        """Prepares user arguments for the measure."""
        args = openstudio.measure.OSArgumentVector()

        a = openstudio.measure.OSArgument.makeBoolArgument("abrir_navegador", True)
        a.setDisplayName("Abrir el informe en el navegador al terminar")
        a.setDefaultValue(True)
        args.append(a)

        a = openstudio.measure.OSArgument.makeStringArgument("carpeta_salida", False)
        a.setDisplayName("Carpeta donde guardar el informe (vacío = carpeta temporal del sistema)")
        a.setDescription("Por ejemplo C:\\Users\\nombre\\Documents. La ruta del informe aparece en los mensajes.")
        a.setDefaultValue("")
        args.append(a)

        a = openstudio.measure.OSArgument.makeBoolArgument("incluir_interiores", True)
        a.setDisplayName("Incluir particiones y huecos interiores")
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

        open_browser = runner.getBoolArgumentValue("abrir_navegador", user_arguments)
        folder = runner.getStringArgumentValue("carpeta_salida", user_arguments).strip().strip('"')
        interior = runner.getBoolArgumentValue("incluir_interiores", user_arguments)

        opaque, glazing, details, issues = collect(model, include_interior=interior)
        n_cons = len({r["cons"] for r in opaque.values()} | {r["cons"] for r in glazing.values()})
        runner.registerInitialCondition(f"El modelo tiene {n_cons} construcciones aplicadas a superficies o huecos.")
        if not opaque and not glazing:
            runner.registerAsNotApplicable("El modelo no tiene superficies con construcción.")
            return True
        for i in issues:
            runner.registerWarning(i)

        # resumen en los mensajes (por si no se abre el navegador)
        for r in sorted(opaque.values(), key=_cat_key):
            u = r.get("U")
            utxt = "no calculable" if u is None else (
                f"U↑ {u[0]:.3f} / U↓ {u[1]:.3f}" if isinstance(u, tuple) else f"U = {u:.3f}")
            runner.registerInfo(f"{r['cat']} | {r['cons']}: {utxt} W/m²K ({r['area']:.1f} m²)")
        for r in sorted(glazing.values(), key=_cat_key):
            utxt = "no calculable" if r["U_ep"] is None else f"U = {r['U_ep']:.3f}"
            ug = "" if r["Ug"] is None else f", Ug EN 673 = {r['Ug']:.2f}"
            runner.registerInfo(f"{r['cat']} | {r['cons']}: {utxt}{ug} W/m²K ({r['area']:.1f} m²)")

        b = model.building()  # sin crear el objeto Building si no existe (no se modifica el modelo)
        building = b.get().nameString() if b.is_initialized() else "Modelo"
        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        meta = dict(building=building, date=now.strftime("%d/%m/%Y %H:%M"), os=openstudio.openStudioVersion(),
                    ep=openstudio.energyPlusVersion(), csv_name=f"transmitancias_{_safe(building)}_{stamp}.csv")
        page = build_html(f"Transmitancias · {building}", opaque, glazing, details, issues, meta)

        # informe: carpeta elegida (o temporal) + report.html en la carpeta de ejecución de la measure
        try:
            out_dir = Path(folder) if folder else Path(tempfile.gettempdir()) / "OpenStudio_transmitancias"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"transmitancias_{_safe(building)}_{stamp}.html"
            path.write_text(page, encoding="utf-8")
        except OSError as e:
            runner.registerError(f"No se pudo guardar el informe en «{folder or tempfile.gettempdir()}»: {e}")
            return False
        try:
            Path("report.html").write_text(page, encoding="utf-8")
        except OSError:
            pass

        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(path.resolve().as_uri())
            except Exception:  # noqa: BLE001 - sin navegador disponible
                opened = False
        runner.registerInfo(f"Informe: {path}")
        if open_browser and not opened:
            runner.registerWarning(f"No se pudo abrir el navegador. Abra el informe a mano: {path}")
        runner.registerFinalCondition(f"Informe de transmitancias guardado en {path}. El modelo no se ha modificado.")
        return True


# register the measure to be used by the application
TransmittanceInfo().registerWithApplication()

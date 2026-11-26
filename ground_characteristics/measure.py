"""Mejora del contacto con el terreno. EnergyPlusMeasure en Python para OpenStudio 3.11 (EnergyPlus 25.2).

Sustituye el valor por defecto de EnergyPlus (18 ºC constantes bajo el edificio) por uno de estos métodos,
elegidos con un desplegable:

  * Ground (Site:GroundTemperature:BuildingSurface): temperaturas mensuales escritas por el usuario.
  * F/C-factor (Site:GroundTemperature:FCfactorMethod): soleras con Construction:FfactorGroundFloor y muros
    enterrados con Construction:CfactorUndergroundWall. Las temperaturas pueden ser las del EPW a 0,5 m
    (lo que hace EnergyPlus si no existe el objeto) o escritas por el usuario.
  * Kiva (Foundation:Kiva + Foundation:Kiva:Settings): cálculo 2D del terreno con aislamiento perimetral; también
    los muros enterrados de las zonas con solera Kiva (sótanos).

Opcionalmente escribe Site:GroundTemperature:Shallow y :Deep, que SOLO usan algunos intercambiadores de calor
enterrados (no afectan a soleras ni muros).

El área y el perímetro expuesto de cada solera se calculan a partir de la geometría: un lado de la solera no
está expuesto si coincide con el lado de otra solera en contacto con el terreno.

Probada con OpenStudio 3.11.0 (EnergyPlus 25.2.0).
"""

import math

import openstudio

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

METHODS = [
    ("Ground", "Ground: temperaturas mensuales bajo el edificio (BuildingSurface)"),
    ("FCfactor", "F/C-factor: soleras F-factor y muros C-factor (FCfactorMethod)"),
    ("Kiva", "Kiva: cálculo 2D del terreno (Foundation:Kiva)"),
]
FC_SOURCES = [
    ("EPW", "Del fichero climático (EPW) a 0,5 m de profundidad"),
    ("Manual", "Escritas a mano (12 valores)"),
]
KIVA_DEEP_BC = ["Autoselect", "ZeroFlux", "GroundWater"]

# Condiciones de contorno de EnergyPlus que significan «en contacto con el terreno»
GROUND_BCS = {
    "GROUND", "GROUNDFCFACTORMETHOD", "FOUNDATION",
    "GROUNDSLABPREPROCESSORAVERAGE", "GROUNDSLABPREPROCESSORCORE", "GROUNDSLABPREPROCESSORPERIMETER",
    "GROUNDBASEMENTPREPROCESSORAVERAGEWALL", "GROUNDBASEMENTPREPROCESSORAVERAGEFLOOR",
    "GROUNDBASEMENTPREPROCESSORUPPERWALL", "GROUNDBASEMENTPREPROCESSORLOWERWALL",
}
# Materiales que Kiva no admite en la construcción de la solera (necesita espesor y masa)
NO_MASS_TYPES = {"Material:NoMass", "Material:AirGap", "Material:InfraredTransparent"}
FC_CONSTRUCTION_TYPES = {"Construction:FfactorGroundFloor", "Construction:CfactorUndergroundWall"}

SURF = "BuildingSurface:Detailed"
TOL = 1e-3  # m

# Propiedades por defecto (se escriben en los materiales que crea la measure)
INSULATION_DENSITY, INSULATION_CP = 35.0, 1400.0  # poliestireno extruido
EQUIV_LAMBDA, EQUIV_DENSITY, EQUIV_CP = 0.06, 200.0, 1300.0  # capa equivalente a un Material:NoMass
CONCRETE = (1.95, 2240.0, 900.0)  # muro de cimentación: hormigón (λ, ρ, cp)
F_UNINSULATED = 1.264  # W/m·K, solera sin aislar (ASHRAE 90.1, F-0,73)


# ---------------------------------------------------------------------------
# Utilidades del workspace (campos por NOMBRE, no por índice)
# ---------------------------------------------------------------------------


def _iot(name):
    return openstudio.IddObjectType(name)


def _idx(obj, field):
    opt = obj.iddObject().getFieldIndex(field)
    if not opt.is_initialized():
        raise KeyError(f"{obj.iddObject().name()} no tiene el campo '{field}'")
    return opt.get()


def _get(obj, field):
    i = _idx(obj, field)
    if i >= obj.numFields():
        return ""
    v = obj.getString(i)
    return v.get().strip() if v.is_initialized() else ""


def _set(obj, field, value):
    return obj.setString(_idx(obj, field), str(value))


def _num(v):
    return f"{v:.6g}"


def _new(workspace, type_name, values):
    """Añade un objeto con valores {campo: valor} y lo devuelve."""
    obj = openstudio.IdfObject(_iot(type_name))
    for field, value in values.items():
        obj.setString(_idx(obj, field), str(value))
    return workspace.addObject(obj).get()


def _unique(workspace, type_name):
    """Objeto único existente o uno nuevo."""
    objs = workspace.getObjectsByType(_iot(type_name))
    if objs:
        for extra in objs[1:]:
            workspace.removeObject(extra.handle())
        return objs[0]
    return workspace.addObject(openstudio.IdfObject(_iot(type_name))).get()


def _find(workspace, name, types):
    for t in types:
        o = workspace.getObjectByTypeAndName(_iot(t), name)
        if o.is_initialized():
            return o.get()
    return None


def construction_resistance(workspace, cname):
    """Resistencia térmica (sin películas) de una Construction por capas, o None si no se puede calcular."""
    cons = workspace.getObjectByTypeAndName(_iot("Construction"), cname)
    if not cons.is_initialized():
        return None
    cons = cons.get()
    total = 0.0
    for i in range(1, cons.numFields()):
        ln = cons.getString(i)
        if not ln.is_initialized() or not ln.get():
            continue
        mat = _find(workspace, ln.get(), ["Material", "Material:NoMass", "Material:AirGap", "Material:InfraredTransparent"])
        if mat is None:
            return None
        kind = mat.iddObject().name()
        try:
            if kind == "Material":
                total += float(_get(mat, "Thickness")) / float(_get(mat, "Conductivity"))
            elif kind in ("Material:NoMass", "Material:AirGap"):
                total += float(_get(mat, "Thermal Resistance"))
        except (ValueError, ZeroDivisionError):
            return None
    return total if total > 0 else None


def parse_values(text, n, label):
    """Lista de n números. Separador ',' con punto decimal, o ';' si se usa coma decimal."""
    text = (text or "").strip()
    parts = [p.strip().replace(",", ".") for p in text.split(";")] if ";" in text else [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"{label}: '{text}' no son números válidos")
    if len(values) != n:
        raise ValueError(f"{label}: se esperaban {n} valores y hay {len(values)} ('{text}')")
    return values


def epw_ground_temperatures(path):
    """{profundidad_m: [12 temperaturas]} de la cabecera GROUND TEMPERATURES del EPW."""
    try:
        with open(path, encoding="latin-1") as f:
            for _ in range(8):
                line = f.readline()
                if line.upper().startswith("GROUND TEMPERATURES"):
                    p = [x.strip() for x in line.strip().split(",")]
                    n, out, i = int(p[1] or 0), {}, 2
                    for _ in range(n):
                        depth = float(p[i])
                        out[depth] = [float(x) for x in p[i + 4:i + 16]]
                        i += 16
                    return out
    except (OSError, ValueError, IndexError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Geometría
# ---------------------------------------------------------------------------


def _zone_transforms(workspace):
    """Zona -> función que pasa coordenadas relativas a absolutas (según GlobalGeometryRules)."""
    ggr = workspace.getObjectsByType(_iot("GlobalGeometryRules"))
    relative = not ggr or _get(ggr[0], "Coordinate System").upper() != "WORLD"
    out = {}
    for z in workspace.getObjectsByType(_iot("Zone")):
        if relative:
            def num(f, z=z):
                v = _get(z, f)
                try:
                    return float(v) if v else 0.0
                except ValueError:
                    return 0.0
            ang = -math.radians(num("Direction of Relative North"))
            ox, oy, oz = num("X Origin"), num("Y Origin"), num("Z Origin")
            c, s = math.cos(ang), math.sin(ang)
            out[z.nameString().upper()] = (lambda p, c=c, s=s, ox=ox, oy=oy, oz=oz:
                                          (p[0] * c - p[1] * s + ox, p[0] * s + p[1] * c + oy, p[2] + oz))
        else:
            out[z.nameString().upper()] = lambda p: p
    return out


def surface_vertices(surface, transforms):
    """Vértices absolutos de un BuildingSurface:Detailed."""
    pts = []
    for g in surface.extensibleGroups():
        try:
            pts.append(tuple(float(g.getString(i).get()) for i in range(3)))
        except (ValueError, RuntimeError):
            continue
    tf = transforms.get(_get(surface, "Zone Name").upper(), lambda p: p)
    return [tf(p) for p in pts]


def start_upper_left(surface):
    """Gira cíclicamente los vértices de un muro para que el lado 1-2 sea vertical (de arriba abajo).

    EnergyPlus calcula la altura del muro con ese lado (convención UpperLeftCorner) y la compara con la altura de la
    Construction:CfactorUndergroundWall; OpenStudio no siempre empieza por esa esquina. El polígono y su orientación
    no cambian."""
    groups = surface.extensibleGroups()
    try:
        pts = [tuple(float(g.getString(i).get()) for i in range(3)) for g in groups]
    except (ValueError, RuntimeError):
        return False
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if abs(a[0] - b[0]) < TOL and abs(a[1] - b[1]) < TOL and a[2] - b[2] > TOL:
            if i == 0:
                return False
            new = pts[i:] + pts[:i]
            for g, p in zip(groups, new):
                for j in range(3):
                    g.setString(j, _num(p[j]))
            return True
    return False


def polygon_area(pts):
    """Área de un polígono 3D (método de Newell)."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1, z1 = pts[i]
        x2, y2, z2 = pts[(i + 1) % n]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)


def _segments(pts):
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _shared_length(seg, others):
    """Longitud del segmento que coincide (colineal y solapado) con alguno de los otros segmentos."""
    (ax, ay, az), (bx, by, bz) = seg
    dx, dy, dz = bx - ax, by - ay, bz - az
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < TOL:
        return 0.0
    ux, uy, uz = dx / length, dy / length, dz / length
    intervals = []
    for (p, q) in others:
        ok, ts = True, []
        for (px, py, pz) in (p, q):
            vx, vy, vz = px - ax, py - ay, pz - az
            t = vx * ux + vy * uy + vz * uz
            # distancia del punto a la recta del segmento
            ex, ey, ez = vx - t * ux, vy - t * uy, vz - t * uz
            if math.sqrt(ex * ex + ey * ey + ez * ez) > 0.01:
                ok = False
                break
            ts.append(t)
        if ok:
            lo, hi = max(0.0, min(ts)), min(length, max(ts))
            if hi - lo > TOL:
                intervals.append((lo, hi))
    intervals.sort()
    covered, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in intervals:
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                covered += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        covered += cur_hi - cur_lo
    return min(covered, length)


def exposed_perimeters(floor_pts):
    """{nombre: (área, perímetro total, perímetro expuesto)} de las soleras en contacto con el terreno."""
    segs = {name: _segments(pts) for name, pts in floor_pts.items()}
    out = {}
    for name, pts in floor_pts.items():
        others = [s for other, ss in segs.items() if other != name for s in ss]
        total = exposed = 0.0
        for seg in segs[name]:
            (a, b) = seg
            length = math.dist(a, b)
            total += length
            exposed += length - _shared_length(seg, others)
        out[name] = (polygon_area(pts), total, max(0.0, exposed))
    return out


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------


class GroundCharacteristics(openstudio.measure.EnergyPlusMeasure):
    """Temperaturas y método de cálculo del terreno."""

    def name(self):
        """Returns the human readable name."""
        return "Ground_characteristics"

    def description(self):
        """Human readable description."""
        return (
            "Mejora el cálculo del calor que el edificio intercambia con el terreno. Por defecto EnergyPlus supone "
            "18 ºC constantes bajo el edificio todo el año. Esta measure permite elegir entre temperaturas mensuales "
            "propias, el método F/C-factor (con las temperaturas del clima a 0,5 m o propias) o Kiva (cálculo 2D "
            "del terreno con aislamiento perimetral), sin editar el IDF."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Actúa sobre las BuildingSurface:Detailed con condición de contorno de terreno (Ground, "
            "GroundFCfactorMethod, Foundation o preprocesadores). Perímetro expuesto de cada solera: suma de sus lados "
            "que no coinciden con lados de otra solera en contacto con el terreno (coordenadas absolutas). "
            "GROUND: condición Ground + Site:GroundTemperature:BuildingSurface. "
            "F/C-FACTOR: una Construction:FfactorGroundFloor por solera (área y perímetro expuesto de la geometría; "
            "las soleras sin perímetro expuesto pasan a Adiabatic) y una Construction:CfactorUndergroundWall por muro "
            "enterrado (altura = extensión vertical; C dado o 1/ΣR de su construcción); condición "
            "GroundFCfactorMethod; temperaturas del EPW a 0,5 m (se elimina Site:GroundTemperature:FCfactorMethod) o "
            "manuales. KIVA: un Foundation:Kiva (aislamiento perimetral opcional y muro de cimentación de hormigón), "
            "Foundation:Kiva:Settings y SurfaceProperty:ExposedFoundationPerimeter (TotalExposedPerimeter calculado); "
            "los muros enterrados de las zonas con solera Kiva también pasan a Foundation; las capas Material:NoMass "
            "o AirGap se sustituyen por una capa equivalente con masa e igual resistencia. Las superficies con "
            "fuente interna (ConstructionProperty:InternalHeatSource) no admiten F/C-factor ni Kiva y quedan como "
            "Ground. Los vértices de los muros F/C o Kiva se rotan para empezar en la esquina superior izquierda "
            "(EnergyPlus calcula así su altura). Shallow/Deep solo los usan intercambiadores enterrados."
        )

    def arguments(self, workspace: openstudio.Workspace):
        """Prepares user arguments for the measure."""
        args = openstudio.measure.OSArgumentVector()

        def choice(name, keys, labels, display, default, desc=""):
            a = openstudio.measure.OSArgument.makeChoiceArgument(name, keys, labels, True)
            a.setDisplayName(display)
            a.setDefaultValue(default)
            if desc:
                a.setDescription(desc)
            args.append(a)

        def string(name, display, default, desc=""):
            a = openstudio.measure.OSArgument.makeStringArgument(name, True)
            a.setDisplayName(display)
            a.setDefaultValue(default)
            if desc:
                a.setDescription(desc)
            args.append(a)

        def double(name, display, default, desc="", lo=None, hi=None):
            a = openstudio.measure.OSArgument.makeDoubleArgument(name, True)
            a.setDisplayName(display)
            a.setDefaultValue(default)
            if desc:
                a.setDescription(desc)
            if lo is not None:
                a.setMinValue(lo)
            if hi is not None:
                a.setMaxValue(hi)
            args.append(a)

        twelve = "12 valores (ene...dic) separados por comas y con punto decimal; o separados por ';' si usa coma decimal."

        choice("metodo", [k for k, _ in METHODS], [d for _, d in METHODS],
               "MÉTODO para el contacto con el terreno", "FCfactor",
               "Solo se usan los parámetros del método elegido (los demás se ignoran).")

        string("ground_temperaturas", "[Ground] Temperaturas bajo el edificio [ºC]", "18,18,18,18,18,18,18,18,18,18,18,18",
               f"{twelve} Guía de EnergyPlus: unos 2 ºC por debajo de la temperatura interior media (edificios comerciales típicos de EE. UU.). "
               "No use las del EPW (terreno no perturbado): son demasiado extremas bajo un edificio acondicionado. "
               "Con F/C-factor o Kiva, se usan en las superficies que esos métodos no admiten (se avisa).")

        choice("fc_origen", [k for k, _ in FC_SOURCES], [d for _, d in FC_SOURCES],
               "[F/C-factor] Origen de las temperaturas del terreno", "EPW",
               "EPW: EnergyPlus toma las de la cabecera del clima a 0,5 m (no se escribe el objeto).")
        string("fc_temperaturas", "[F/C-factor] Temperaturas manuales [ºC]", "15,15,15,15,15,15,15,15,15,15,15,15",
               f"Solo si el origen es «manual». {twelve} Deberían parecerse a la temperatura exterior media mensual "
               "con unos 3 meses de retraso.")
        double("fc_f_factor", "[F/C-factor] F-factor de las soleras [W/m·K]", F_UNINSULATED,
               "Pérdida por metro de perímetro expuesto y grado. Solera sin aislar (ASHRAE 90.1, F-0,73 Btu/h·ft·ºF): "
               "1,264 W/m·K. A partir de la transmitancia de la solera del CTE (Us, que ya incluye el terreno): "
               "F = Us · A/P (la measure informa de A/P de cada solera).", lo=0.001)
        double("fc_c_factor", "[F/C-factor] C-factor de los muros enterrados [W/m²·K] (0 = de su construcción)", 0.0,
               "Transmitancia del muro sin películas superficiales ni terreno. 0: se calcula para cada muro con su "
               "construcción actual, C = 1/Σ(e/λ). Referencia: muro de bloque de hormigón de 20 cm sin aislar "
               "(ASHRAE 90.1, C-1,140 Btu/h·ft²·ºF) = 6,473 W/m²·K.", lo=0.0)

        string("kiva_terreno", "[Kiva] Terreno: conductividad [W/m·K], densidad [kg/m³], calor específico [J/kg·K]",
               "1.73, 1842, 419", "Valores por defecto de EnergyPlus (suelo genérico).")
        choice("kiva_contorno_profundo", KIVA_DEEP_BC, KIVA_DEEP_BC, "[Kiva] Condición en el fondo del dominio",
               "Autoselect", "Autoselect: nivel freático si se conoce; si no, flujo nulo.")
        double("kiva_aislante_lambda", "[Kiva] Conductividad del aislante perimetral [W/m·K]", 0.034,
               "Para todas las posiciones de aislamiento (densidad 35 kg/m³ y calor específico 1400 J/kg·K).", lo=0.001)
        string("kiva_ais_int_horizontal", "[Kiva] Aislamiento interior horizontal: espesor [m], anchura [m]", "0, 0",
               "Bajo la solera, desde el muro hacia dentro. Espesor 0 = sin este aislamiento.")
        string("kiva_ais_int_vertical", "[Kiva] Aislamiento interior vertical: espesor [m], profundidad [m]", "0, 0",
               "Por dentro del muro de cimentación, desde su coronación hacia abajo.")
        string("kiva_ais_ext_horizontal",
               "[Kiva] Aislamiento exterior horizontal: espesor [m], anchura [m], profundidad [m]", "0, 0, 0",
               "Enterrado fuera del edificio; anchura desde el muro hacia fuera; profundidad desde la rasante.")
        string("kiva_ais_ext_vertical", "[Kiva] Aislamiento exterior vertical: espesor [m], profundidad [m]", "0, 0",
               "Por fuera del muro de cimentación, desde su coronación hacia abajo.")
        string("kiva_muro", "[Kiva] Muro de cimentación: altura sobre rasante [m], profundidad bajo la solera [m], "
               "espesor [m]", "0.2, 0, 0.3",
               "Coronación del muro sobre el terreno exterior y prolongación del muro por debajo de la solera "
               "(por defecto en EnergyPlus: 0,2 y 0). Muro de hormigón (λ 1,95 W/m·K, 2240 kg/m³, 900 J/kg·K); se usa "
               "en las soleras sin muros enterrados en su zona.")

        b = openstudio.measure.OSArgument.makeBoolArgument("escribir_shallow_deep", True)
        b.setDisplayName("[Shallow/Deep] Escribir también estas temperaturas")
        b.setDescription("SOLO las usan algunos intercambiadores de calor enterrados. No afectan a soleras ni muros.")
        b.setDefaultValue(False)
        args.append(b)
        string("shallow_temperaturas", "[Shallow/Deep] Shallow (superficial) [ºC]", "13,13,13,13,13,13,13,13,13,13,13,13",
               twelve)
        string("deep_temperaturas", "[Shallow/Deep] Deep (3-4 m) [ºC]", "16,16,16,16,16,16,16,16,16,16,16,16", twelve)

        return args

    def run(self, workspace: openstudio.Workspace, runner: openstudio.measure.OSRunner,
            user_arguments: openstudio.measure.OSArgumentMap):
        """Defines what happens when the measure is run."""
        super().run(workspace, runner, user_arguments)  # Do **NOT** remove this line

        if not runner.validateUserArguments(self.arguments(workspace), user_arguments):
            return False

        a = lambda n: runner.getStringArgumentValue(n, user_arguments)  # noqa: E731
        method = a("metodo")
        try:
            ground_t = parse_values(a("ground_temperaturas"), 12, "Temperaturas [Ground]")
            fc_source = a("fc_origen")
            fc_t = parse_values(a("fc_temperaturas"), 12, "Temperaturas [F/C-factor]") if fc_source == "Manual" else None
            f_factor = runner.getDoubleArgumentValue("fc_f_factor", user_arguments)
            c_factor = runner.getDoubleArgumentValue("fc_c_factor", user_arguments)
            soil = parse_values(a("kiva_terreno"), 3, "Terreno [Kiva]")
            ins_lambda = runner.getDoubleArgumentValue("kiva_aislante_lambda", user_arguments)
            ins_ih = parse_values(a("kiva_ais_int_horizontal"), 2, "Aislamiento interior horizontal [Kiva]")
            ins_iv = parse_values(a("kiva_ais_int_vertical"), 2, "Aislamiento interior vertical [Kiva]")
            ins_eh = parse_values(a("kiva_ais_ext_horizontal"), 3, "Aislamiento exterior horizontal [Kiva]")
            ins_ev = parse_values(a("kiva_ais_ext_vertical"), 2, "Aislamiento exterior vertical [Kiva]")
            wall = parse_values(a("kiva_muro"), 3, "Muro de cimentación [Kiva]")
            write_sd = runner.getBoolArgumentValue("escribir_shallow_deep", user_arguments)
            shallow_t = parse_values(a("shallow_temperaturas"), 12, "Shallow") if write_sd else None
            deep_t = parse_values(a("deep_temperaturas"), 12, "Deep") if write_sd else None
        except ValueError as e:
            runner.registerError(str(e))
            return False
        if any(v < 0 for v in ins_ih + ins_iv + ins_eh + ins_ev + soil + wall):
            runner.registerError("Los espesores, dimensiones y propiedades del terreno no pueden ser negativos.")
            return False
        if method == "Kiva":
            if min(soil) <= 0 or wall[2] <= 0:
                runner.registerError("[Kiva] Las propiedades del terreno y el espesor del muro deben ser mayores que 0.")
                return False
            for label, vals in (("interior horizontal", ins_ih), ("interior vertical", ins_iv),
                                ("exterior vertical", ins_ev)):
                if vals[0] > 0 and vals[1] <= 0:
                    runner.registerError(f"[Kiva] Aislamiento {label}: con espesor > 0, la anchura/profundidad debe "
                                         "ser mayor que 0.")
                    return False
        if method == "FCfactor" and f_factor > 1.5 * F_UNINSULATED:
            runner.registerWarning(f"F-factor {f_factor:g} W/m·K: muy superior al de una solera sin aislar "
                                   f"({F_UNINSULATED} W/m·K). Revise el valor.")

        # --- superficies en contacto con el terreno ---
        surfaces = list(workspace.getObjectsByType(_iot(SURF)))
        ground = [s for s in surfaces if _get(s, "Outside Boundary Condition").upper() in GROUND_BCS]
        other_types = [t for t in ("Floor:Detailed", "Wall:Detailed", "RoofCeiling:Detailed", "Floor:GroundContact",
                                   "Wall:Underground") if workspace.getObjectsByType(_iot(t))]
        if other_types:
            runner.registerWarning(f"El IDF tiene superficies de tipo {', '.join(other_types)}; esta measure solo "
                                   f"modifica {SURF}.")
        floors = [s for s in ground if _get(s, "Surface Type").upper() == "FLOOR"]
        walls = [s for s in ground if _get(s, "Surface Type").upper() == "WALL"]
        others = [s for s in ground if s not in floors and s not in walls]
        runner.registerInitialCondition(
            f"{len(floors)} soleras, {len(walls)} muros y {len(others)} otras superficies en contacto con el terreno.")
        if not ground:
            runner.registerAsNotApplicable("El modelo no tiene superficies en contacto con el terreno.")
            return True
        if others:
            runner.registerWarning("Superficies de cubierta/techo en contacto con el terreno se dejan como Ground: "
                                   + ", ".join(s.nameString() for s in others))

        transforms = _zone_transforms(workspace)
        ggr = workspace.getObjectsByType(_iot("GlobalGeometryRules"))
        upper_left_ccw = not ggr or (_get(ggr[0], "Starting Vertex Position").upper() == "UPPERLEFTCORNER" and
                                     _get(ggr[0], "Vertex Entry Direction").upper() == "COUNTERCLOCKWISE")

        def fix_wall_vertices(s):
            if upper_left_ccw:
                start_upper_left(s)

        zone_of = {s.nameString(): _get(s, "Zone Name") for s in ground}
        floor_pts = {s.nameString(): surface_vertices(s, transforms) for s in floors}
        geom = exposed_perimeters(floor_pts)
        tot_a = sum(g[0] for g in geom.values())
        tot_p = sum(g[2] for g in geom.values())
        if tot_p > 0:
            runner.registerInfo(f"Soleras: área total {tot_a:.1f} m², perímetro expuesto {tot_p:.1f} m, "
                                f"A/P = {tot_a / tot_p:.2f} m (B' = 2·A/P = {2 * tot_a / tot_p:.2f} m).")
        for name, (area, per, exp) in geom.items():
            ap = f"A/P = {area / exp:.2f} m" if exp > TOL else "sin perímetro expuesto"
            runner.registerInfo(f"  {name} ({zone_of[name]}): área {area:.2f} m², perímetro {per:.2f} m, "
                                f"expuesto {exp:.2f} m, {ap}.")

        # temperaturas del EPW (solo para informar)
        epw_t = {}
        epw = runner.lastEpwFilePath()
        if epw.is_initialized():
            epw_t = epw_ground_temperatures(str(epw.get()))
        if epw_t:
            for depth, vals in sorted(epw_t.items()):
                runner.registerInfo(f"EPW, terreno a {depth:g} m: " + ", ".join(f"{v:.1f}" for v in vals))

        # quitar SurfaceProperty:ExposedFoundationPerimeter de las superficies que se van a modificar
        names = {s.nameString().upper() for s in ground}
        for efp in workspace.getObjectsByType(_iot("SurfaceProperty:ExposedFoundationPerimeter")):
            if _get(efp, "Surface Name").upper() in names:
                workspace.removeObject(efp.handle())

        source_cons = {_get(o, "Construction Name").upper()
                       for o in workspace.getObjectsByType(_iot("ConstructionProperty:InternalHeatSource"))}

        def construction_type(s):
            c = _get(s, "Construction Name")
            for t in sorted(FC_CONSTRUCTION_TYPES) + ["Construction"]:
                if workspace.getObjectByTypeAndName(_iot(t), c).is_initialized():
                    return t
            return None

        def set_ground(s):
            _set(s, "Outside Boundary Condition", "Ground")
            _set(s, "Outside Boundary Condition Object", "")
            _set(s, "Sun Exposure", "NoSun")
            _set(s, "Wind Exposure", "NoWind")

        def set_bc(s, bc, obj=""):
            _set(s, "Outside Boundary Condition", bc)
            _set(s, "Outside Boundary Condition Object", obj)
            _set(s, "Sun Exposure", "NoSun")
            _set(s, "Wind Exposure", "NoWind")

        def need_layered(group):
            empty = [s.nameString() for s in group if not _get(s, "Construction Name")]
            if empty:
                runner.registerError(
                    "Estas superficies en contacto con el terreno no tienen construcción: " + ", ".join(empty) + ". "
                    "Asígnesela en el modelo (p. ej. en el conjunto de construcciones por defecto, apartado 'Ground "
                    "Contact').")
                return False
            bad = [s.nameString() for s in group if construction_type(s) in FC_CONSTRUCTION_TYPES]
            if bad:
                runner.registerError(
                    "Estas superficies tienen construcciones F/C-factor y el método elegido necesita una construcción "
                    "por capas: " + ", ".join(bad) + ". Asígneles una construcción normal en el modelo.")
                return False
            return True

        written = {"bs": False}

        def write_building_surface():
            if written["bs"]:
                return
            written["bs"] = True
            obj = _unique(workspace, "Site:GroundTemperature:BuildingSurface")
            for i, v in enumerate(ground_t):
                obj.setString(i, _num(v))
            runner.registerInfo("Site:GroundTemperature:BuildingSurface: " + ", ".join(_num(v) for v in ground_t))
            if all(abs(v - 18.0) < 1e-9 for v in ground_t):
                runner.registerWarning("Las temperaturas [Ground] son 18 ºC constantes, igual que el valor por "
                                       "defecto de EnergyPlus.")

        def split_sources(group):
            """Las superficies con fuente de calor interna (suelo radiante) no admiten F-factor ni Kiva."""
            keep = [s for s in group if _get(s, "Construction Name").upper() not in source_cons]
            skipped = [s for s in group if s not in keep]
            if skipped:
                for s in skipped:
                    set_ground(s)
                write_building_surface()
                runner.registerWarning(
                    "Superficies con fuente de calor interna (p. ej. suelo radiante): este método no la admite y se "
                    "dejan con condición Ground y las temperaturas [Ground]: " + ", ".join(s.nameString() for s in skipped))
            return keep

        n_floors, n_walls = len(floors), len(walls)

        # ------------------------------------------------------------------
        if method == "Ground":
            if not need_layered(ground):
                return False
            for s in ground:
                set_ground(s)
            write_building_surface()

        # ------------------------------------------------------------------
        elif method == "FCfactor":
            floors, walls = split_sources(floors), split_sources(walls)
            adiabatic = []
            for s in floors:
                area, _, exp = geom[s.nameString()]
                if exp < TOL:
                    set_bc(s, "Adiabatic")
                    if construction_type(s) in FC_CONSTRUCTION_TYPES:
                        runner.registerWarning(f"{s.nameString()}: sin perímetro expuesto y con construcción F-factor; "
                                               "asígnele una construcción normal.")
                    adiabatic.append(s.nameString())
                    continue
                cname = f"{s.nameString()} F-factor"
                old = workspace.getObjectByTypeAndName(_iot("Construction:FfactorGroundFloor"), cname)
                if old.is_initialized():
                    workspace.removeObject(old.get().handle())
                _new(workspace, "Construction:FfactorGroundFloor",
                     {"Name": cname, "F-Factor": _num(f_factor), "Area": _num(area), "PerimeterExposed": _num(exp)})
                _set(s, "Construction Name", cname)
                set_bc(s, "GroundFCfactorMethod")
                runner.registerInfo(f"  {s.nameString()}: F-factor {f_factor:g} W/m·K -> U equivalente = F·P/A = "
                                    f"{f_factor * exp / area:.3f} W/m²·K.")
            if adiabatic:
                runner.registerWarning("Soleras sin perímetro expuesto (rodeadas por otras soleras): con el método "
                                       "F-factor toda la pérdida va por el perímetro y pasan a Adiabatic: "
                                       + ", ".join(adiabatic))
            for s in walls:
                zs = [p[2] for p in surface_vertices(s, transforms)]
                height = max(zs) - min(zs) if zs else 0.0
                if height < TOL:
                    runner.registerWarning(f"{s.nameString()}: altura nula; se deja como Ground.")
                    set_ground(s)
                    write_building_surface()
                    continue
                c_wall = c_factor
                if c_wall <= 0:
                    r = construction_resistance(workspace, _get(s, "Construction Name"))
                    if r is None:
                        runner.registerError(
                            f"{s.nameString()}: no se puede calcular el C-factor de su construcción "
                            f"('{_get(s, 'Construction Name')}'). Escriba un C-factor mayor que 0.")
                        return False
                    c_wall = 1.0 / r
                cname = f"{s.nameString()} C-factor"
                old = workspace.getObjectByTypeAndName(_iot("Construction:CfactorUndergroundWall"), cname)
                if old.is_initialized():
                    workspace.removeObject(old.get().handle())
                origin = (f"de '{_get(s, 'Construction Name')}'" if c_factor <= 0 else "escrito")
                _new(workspace, "Construction:CfactorUndergroundWall",
                     {"Name": cname, "C-Factor": _num(c_wall), "Height": _num(height)})
                _set(s, "Construction Name", cname)
                set_bc(s, "GroundFCfactorMethod")
                fix_wall_vertices(s)
                runner.registerInfo(f"  {s.nameString()} ({zone_of[s.nameString()]}): muro C-factor {c_wall:.3f} "
                                    f"W/m²·K ({origin}), altura enterrada {height:.2f} m.")
            if others and not need_layered(others):
                return False
            for s in others:
                set_ground(s)
            if others:
                write_building_surface()
            if fc_source == "EPW":
                for o in workspace.getObjectsByType(_iot("Site:GroundTemperature:FCfactorMethod")):
                    workspace.removeObject(o.handle())
                if epw_t and 0.5 in epw_t:
                    runner.registerInfo("F/C-factor con las temperaturas del EPW a 0,5 m: "
                                        + ", ".join(f"{v:.1f}" for v in epw_t[0.5]))
                elif epw.is_initialized():
                    runner.registerError("El EPW no tiene temperaturas del terreno a 0,5 m en su cabecera y EnergyPlus "
                                         "se detendría («no FC Ground Temperatures were input»). Use el origen "
                                         "«manual».")
                    return False
                else:
                    runner.registerInfo("F/C-factor: EnergyPlus usará las temperaturas del EPW a 0,5 m.")
            else:
                obj = _unique(workspace, "Site:GroundTemperature:FCfactorMethod")
                for i, v in enumerate(fc_t):
                    obj.setString(i, _num(v))
                runner.registerInfo("Site:GroundTemperature:FCfactorMethod: " + ", ".join(_num(v) for v in fc_t))

        # ------------------------------------------------------------------
        elif method == "Kiva":
            floors = split_sources(floors)
            kiva_zones = {zone_of[s.nameString()].upper() for s in floors}
            kiva_walls = [s for s in split_sources(walls) if zone_of[s.nameString()].upper() in kiva_zones]
            ground_walls = [s for s in walls if s not in kiva_walls and
                            _get(s, "Construction Name").upper() not in source_cons]
            if not need_layered(floors + kiva_walls + ground_walls + others):
                return False
            if not floors:
                runner.registerWarning("Ninguna solera admite Kiva.")
            settings = _unique(workspace, "Foundation:Kiva:Settings")
            _set(settings, "Soil Conductivity", _num(soil[0]))
            _set(settings, "Soil Density", _num(soil[1]))
            _set(settings, "Soil Specific Heat", _num(soil[2]))
            _set(settings, "Deep-Ground Boundary Condition", runner.getStringArgumentValue("kiva_contorno_profundo",
                                                                                            user_arguments))

            def material(name, thickness, k, rho, cp):
                if not workspace.getObjectByTypeAndName(_iot("Material"), name).is_initialized():
                    _new(workspace, "Material", {
                        "Name": name, "Roughness": "MediumRough", "Thickness": _num(thickness),
                        "Conductivity": _num(k), "Density": _num(rho), "Specific Heat": _num(cp)})
                return name

            def insulation(tag, thickness):
                if thickness <= 0:
                    return ""
                return material(f"Kiva aislante {tag} {thickness * 1000:.0f} mm", thickness, ins_lambda,
                                INSULATION_DENSITY, INSULATION_CP)

            # muro de cimentación (para las soleras sin muros enterrados en su zona)
            wall_mat = material(f"Kiva hormigon muro {wall[2] * 1000:.0f} mm", wall[2], *CONCRETE)
            wall_cons = f"Kiva muro cimentacion {wall[2] * 1000:.0f} mm"
            if not workspace.getObjectByTypeAndName(_iot("Construction"), wall_cons).is_initialized():
                obj = openstudio.IdfObject(_iot("Construction"))
                obj.setString(0, wall_cons)
                obj.setString(1, wall_mat)
                workspace.addObject(obj)

            kiva_name = "Kiva cimentacion"
            for o in workspace.getObjectsByType(_iot("Foundation:Kiva")):
                if o.nameString() == kiva_name:
                    workspace.removeObject(o.handle())
            values = {"Name": kiva_name, "Wall Height Above Grade": _num(wall[0]),
                      "Wall Depth Below Slab": _num(wall[1]), "Footing Wall Construction Name": wall_cons}
            m = insulation("int horizontal", ins_ih[0])
            if m:
                values.update({"Interior Horizontal Insulation Material Name": m,
                               "Interior Horizontal Insulation Depth": "0",
                               "Interior Horizontal Insulation Width": _num(ins_ih[1])})
            m = insulation("int vertical", ins_iv[0])
            if m:
                values.update({"Interior Vertical Insulation Material Name": m,
                               "Interior Vertical Insulation Depth": _num(ins_iv[1])})
            m = insulation("ext horizontal", ins_eh[0])
            if m:
                values.update({"Exterior Horizontal Insulation Material Name": m,
                               "Exterior Horizontal Insulation Width": _num(ins_eh[1]),
                               "Exterior Horizontal Insulation Depth": _num(ins_eh[2])})
            m = insulation("ext vertical", ins_ev[0])
            if m:
                values.update({"Exterior Vertical Insulation Material Name": m,
                               "Exterior Vertical Insulation Depth": _num(ins_ev[1])})
            _new(workspace, "Foundation:Kiva", values)

            converted = {}

            def massed_construction(cname):
                """Kiva necesita capas con masa: Material:NoMass/AirGap -> capa equivalente de igual resistencia."""
                if cname in converted:
                    return converted[cname]
                converted[cname] = cname
                cons = workspace.getObjectByTypeAndName(_iot("Construction"), cname)
                if not cons.is_initialized():
                    return cname
                cons = cons.get()
                layers = [cons.getString(i).get() for i in range(1, cons.numFields()) if cons.getString(i).get()]
                kinds = [(ln, _find(workspace, ln, ["Material"] + sorted(NO_MASS_TYPES))) for ln in layers]
                if not any(o is not None and o.iddObject().name() in NO_MASS_TYPES for _, o in kinds):
                    return cname
                new_layers = []
                for ln, o in kinds:
                    kind = o.iddObject().name() if o is not None else ""
                    if kind not in NO_MASS_TYPES:
                        new_layers.append(ln)
                        continue
                    if kind == "Material:InfraredTransparent":
                        continue
                    rv = _get(o, "Thermal Resistance")
                    r = float(rv) if rv else 0.15
                    new_layers.append(material(f"{ln} (equivalente Kiva)", r * EQUIV_LAMBDA, EQUIV_LAMBDA,
                                               EQUIV_DENSITY, EQUIV_CP))
                new_name = f"{cname} (Kiva)"
                old = workspace.getObjectByTypeAndName(_iot("Construction"), new_name)
                if old.is_initialized():
                    workspace.removeObject(old.get().handle())
                obj = openstudio.IdfObject(_iot("Construction"))
                obj.setString(0, new_name)
                for i, ln in enumerate(new_layers):
                    obj.setString(i + 1, ln)
                workspace.addObject(obj)
                converted[cname] = new_name
                runner.registerWarning(
                    f"La construcción '{cname}' tiene capas sin masa (Material:NoMass/AirGap) que Kiva no admite: se "
                    f"crea '{new_name}' con capas equivalentes de igual resistencia (λ = {EQUIV_LAMBDA} W/m·K).")
                return new_name

            for s in floors + kiva_walls:
                _set(s, "Construction Name", massed_construction(_get(s, "Construction Name")))
                set_bc(s, "Foundation", kiva_name)
            for s in kiva_walls:
                fix_wall_vertices(s)
            for s in floors:
                exp = geom[s.nameString()][2]
                _new(workspace, "SurfaceProperty:ExposedFoundationPerimeter", {
                    "Surface Name": s.nameString(), "Exposed Perimeter Calculation Method": "TotalExposedPerimeter",
                    "Total Exposed Perimeter": _num(exp)})
                if exp < TOL:
                    runner.registerInfo(f"  {s.nameString()}: sin perímetro expuesto; Kiva la calcula en 1D contra el "
                                        "terreno profundo.")
            if kiva_walls:
                runner.registerInfo("Muros enterrados calculados con Kiva (misma zona que una solera Kiva): "
                                    + ", ".join(s.nameString() for s in kiva_walls))
            for s in ground_walls + others:
                set_ground(s)
            if ground_walls or others:
                runner.registerWarning(
                    "Superficies enterradas en zonas sin solera Kiva: siguen con condición Ground y usan las "
                    "temperaturas [Ground]: " + ", ".join(s.nameString() for s in ground_walls + others))
                write_building_surface()
            ins_txt = [t for t, v in (("int. horizontal", ins_ih[0]), ("int. vertical", ins_iv[0]),
                                      ("ext. horizontal", ins_eh[0]), ("ext. vertical", ins_ev[0])) if v > 0]
            runner.registerInfo(f"Kiva: {len(floors)} soleras y {len(kiva_walls)} muros; terreno λ={soil[0]:g} W/m·K, "
                                f"ρ={soil[1]:g} kg/m³, cp={soil[2]:g} J/kg·K; aislamiento: "
                                f"{', '.join(ins_txt) if ins_txt else 'ninguno'}; muro de cimentación {wall[2]:g} m.")

        # ------------------------------------------------------------------
        if write_sd:
            for t, vals in (("Site:GroundTemperature:Shallow", shallow_t), ("Site:GroundTemperature:Deep", deep_t)):
                obj = _unique(workspace, t)
                for i, v in enumerate(vals):
                    obj.setString(i, _num(v))
            runner.registerInfo("Escritas Site:GroundTemperature:Shallow y :Deep (solo para intercambiadores enterrados).")

        label = dict(METHODS)[method].split(":")[0]
        runner.registerFinalCondition(
            f"Método {label}: {n_floors} soleras y {n_walls} muros en contacto con el terreno revisados.")
        return True


# register the measure to be used by the application
GroundCharacteristics().registerWithApplication()

"""Ground_characteristics: better ground heat transfer. EnergyPlus Measure (Python) for OpenStudio 3.11 (EnergyPlus 25.2).

Copyright (c) 2026 Germán Ramos Ruiz. SPDX-License-Identifier: Apache-2.0

Replaces the EnergyPlus default (a constant 18 °C under the building) with one of these methods, selected from a
drop-down list:

  * Ground (Site:GroundTemperature:BuildingSurface): monthly temperatures entered by the user.
  * F/C-factor (Site:GroundTemperature:FCfactorMethod): slabs with Construction:FfactorGroundFloor and below-grade
    walls with Construction:CfactorUndergroundWall. The temperatures can be those of the weather file at 0.5 m
    (what EnergyPlus does when the object is absent) or entered by the user.
  * Kiva (Foundation:Kiva + Foundation:Kiva:Settings): two-dimensional ground calculation with perimeter insulation;
    it also includes the below-grade walls of zones that have a Kiva slab (basements).

Optionally writes Site:GroundTemperature:Shallow and :Deep, which are used ONLY by some ground heat exchangers
(they do not affect floors or walls).

The area and exposed perimeter of each slab are computed from the geometry: an edge of a slab is not exposed when it
coincides with an edge of another ground-contact slab.

Tested with OpenStudio 3.11.0 (EnergyPlus 25.2.0).
"""

import math

import openstudio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

METHODS = [
    ("Ground", "Ground: monthly temperatures under the building (BuildingSurface)"),
    ("FCfactor", "F/C-factor: F-factor slabs and C-factor walls (FCfactorMethod)"),
    ("Kiva", "Kiva: 2D ground calculation (Foundation:Kiva)"),
]
FC_SOURCES = [
    ("EPW", "From the weather file (EPW) at 0.5 m depth"),
    ("Manual", "Entered manually (12 values)"),
]
KIVA_DEEP_BC = ["Autoselect", "ZeroFlux", "GroundWater"]

# EnergyPlus boundary conditions that mean "in contact with the ground"
GROUND_BCS = {
    "GROUND", "GROUNDFCFACTORMETHOD", "FOUNDATION",
    "GROUNDSLABPREPROCESSORAVERAGE", "GROUNDSLABPREPROCESSORCORE", "GROUNDSLABPREPROCESSORPERIMETER",
    "GROUNDBASEMENTPREPROCESSORAVERAGEWALL", "GROUNDBASEMENTPREPROCESSORAVERAGEFLOOR",
    "GROUNDBASEMENTPREPROCESSORUPPERWALL", "GROUNDBASEMENTPREPROCESSORLOWERWALL",
}
# Materials that Kiva does not accept in the slab construction (it needs thickness and mass)
NO_MASS_TYPES = {"Material:NoMass", "Material:AirGap", "Material:InfraredTransparent"}
FC_CONSTRUCTION_TYPES = {"Construction:FfactorGroundFloor", "Construction:CfactorUndergroundWall"}

SURF = "BuildingSurface:Detailed"
TOL = 1e-3  # m

# Default properties (written to the materials created by the measure)
INSULATION_DENSITY, INSULATION_CP = 35.0, 1400.0  # extruded polystyrene
EQUIV_LAMBDA, EQUIV_DENSITY, EQUIV_CP = 0.06, 200.0, 1300.0  # massive layer equivalent to a Material:NoMass
CONCRETE = (1.95, 2240.0, 900.0)  # foundation wall: concrete (conductivity, density, specific heat)
F_UNINSULATED = 1.264  # W/m·K, uninsulated slab (ASHRAE 90.1, F-0.73)


# ---------------------------------------------------------------------------
# Workspace helpers (fields by NAME, not by index)
# ---------------------------------------------------------------------------


def _iot(name):
    return openstudio.IddObjectType(name)


def _idx(obj, field):
    opt = obj.iddObject().getFieldIndex(field)
    if not opt.is_initialized():
        raise KeyError(f"{obj.iddObject().name()} has no field '{field}'")
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
    """Adds an object with {field: value} values and returns it."""
    obj = openstudio.IdfObject(_iot(type_name))
    for field, value in values.items():
        obj.setString(_idx(obj, field), str(value))
    return workspace.addObject(obj).get()


def _unique(workspace, type_name):
    """Existing unique object, or a new one."""
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
    """Thermal resistance (without air films) of a layered Construction, or None if it cannot be computed."""
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
    """List of n numbers. Comma-separated with a decimal point, or ';'-separated when a decimal comma is used."""
    text = (text or "").strip()
    parts = [p.strip().replace(",", ".") for p in text.split(";")] if ";" in text else [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"{label}: '{text}' are not valid numbers")
    if len(values) != n:
        raise ValueError(f"{label}: {n} values expected, {len(values)} found ('{text}')")
    return values


def epw_ground_temperatures(path):
    """{depth_m: [12 temperatures]} from the GROUND TEMPERATURES header of the EPW."""
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
# Geometry
# ---------------------------------------------------------------------------


def _zone_transforms(workspace):
    """Zone -> function that converts relative coordinates into absolute ones (according to GlobalGeometryRules)."""
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
    """Absolute vertices of a BuildingSurface:Detailed."""
    pts = []
    for g in surface.extensibleGroups():
        try:
            pts.append(tuple(float(g.getString(i).get()) for i in range(3)))
        except (ValueError, RuntimeError):
            continue
    tf = transforms.get(_get(surface, "Zone Name").upper(), lambda p: p)
    return [tf(p) for p in pts]


def start_upper_left(surface):
    """Rotates the vertices of a wall cyclically so that edge 1-2 is vertical (top to bottom).

    EnergyPlus computes the wall height from that edge (UpperLeftCorner convention) and compares it with the height
    of the Construction:CfactorUndergroundWall; OpenStudio does not always start at that corner. The polygon and its
    orientation do not change."""
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
    """Area of a 3D polygon (Newell's method)."""
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
    """Length of the segment that coincides (collinear and overlapping) with any of the other segments."""
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
            # distance from the point to the line of the segment
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
    """{name: (area, total perimeter, exposed perimeter)} of the ground-contact slabs."""
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
    """Ground temperatures and ground heat transfer method."""

    def name(self):
        """Returns the human readable name."""
        return "Ground_characteristics"

    def description(self):
        """Human readable description."""
        return (
            "Improves the calculation of the heat exchanged between the building and the ground. By default, "
            "EnergyPlus assumes a constant 18 °C under the building all year round. This measure lets you choose "
            "between your own monthly temperatures, the F/C-factor method (with the weather-file temperatures at "
            "0.5 m or your own) and Kiva (2D ground calculation with perimeter insulation), without editing the IDF."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Acts on the BuildingSurface:Detailed objects with a ground boundary condition (Ground, "
            "GroundFCfactorMethod, Foundation or preprocessors). Exposed perimeter of each slab: sum of its edges that "
            "do not coincide with edges of another ground-contact slab (absolute coordinates). "
            "GROUND: Ground boundary condition + Site:GroundTemperature:BuildingSurface. "
            "F/C-FACTOR: one Construction:FfactorGroundFloor per slab (area and exposed perimeter from the geometry; "
            "slabs without exposed perimeter become Adiabatic) and one Construction:CfactorUndergroundWall per "
            "below-grade wall (height = vertical extent; C given or 1/ΣR of its construction); GroundFCfactorMethod "
            "boundary condition; weather-file temperatures at 0.5 m (Site:GroundTemperature:FCfactorMethod is "
            "removed) or manual ones. KIVA: one Foundation:Kiva (optional perimeter insulation and concrete foundation "
            "wall), Foundation:Kiva:Settings and SurfaceProperty:ExposedFoundationPerimeter (computed "
            "TotalExposedPerimeter); below-grade walls of zones with a Kiva slab also become Foundation; "
            "Material:NoMass or AirGap layers are replaced by an equivalent massive layer with the same resistance. "
            "Surfaces with an internal source (ConstructionProperty:InternalHeatSource) cannot use F/C-factor or Kiva "
            "and are kept as Ground. The vertices of F/C and Kiva walls are rotated to start at the upper-left corner "
            "(this is how EnergyPlus computes their height). Shallow/Deep are used only by ground heat exchangers."
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

        twelve = ("12 values (Jan...Dec) separated by commas, with a decimal point; or separated by ';' if a decimal "
                  "comma is used.")

        choice("method", [k for k, _ in METHODS], [d for _, d in METHODS],
               "GROUND HEAT TRANSFER METHOD", "FCfactor",
               "Only the arguments of the selected method are used (the rest are ignored).")

        string("ground_temperatures", "[Ground] Temperatures under the building [°C]",
               "18,18,18,18,18,18,18,18,18,18,18,18",
               f"{twelve} EnergyPlus guidance: about 2 °C below the average indoor temperature (typical US commercial "
               "buildings). Do not use the weather-file (undisturbed ground) values: they are too extreme under a "
               "conditioned building. With F/C-factor or Kiva, they are used for the surfaces those methods cannot "
               "handle (with a warning).")

        choice("fc_source", [k for k, _ in FC_SOURCES], [d for _, d in FC_SOURCES],
               "[F/C-factor] Source of the ground temperatures", "EPW",
               "EPW: EnergyPlus takes those of the weather-file header at 0.5 m (the object is not written).")
        string("fc_temperatures", "[F/C-factor] Manual temperatures [°C]", "15,15,15,15,15,15,15,15,15,15,15,15",
               f"Only when the source is 'Manual'. {twelve} They should be similar to the monthly mean outdoor air "
               "temperature delayed by about 3 months.")
        double("fc_f_factor", "[F/C-factor] Slab F-factor [W/m·K]", F_UNINSULATED,
               "Heat loss per metre of exposed perimeter and per degree. Uninsulated slab (ASHRAE 90.1, F-0.73 "
               "Btu/h·ft·°F): 1.264 W/m·K. From a slab U-value that already includes the ground (e.g. the Spanish CTE "
               "Us): F = U · A/P (the measure reports A/P for each slab).", lo=0.001)
        double("fc_c_factor", "[F/C-factor] Below-grade wall C-factor [W/m²·K] (0 = from its construction)", 0.0,
               "Wall transmittance without air films or soil. 0: computed for each wall from its current construction, "
               "C = 1/Σ(e/λ). Reference: uninsulated 8-in concrete block wall (ASHRAE 90.1, C-1.140 Btu/h·ft²·°F) = "
               "6.473 W/m²·K.", lo=0.0)

        string("kiva_soil", "[Kiva] Soil: conductivity [W/m·K], density [kg/m³], specific heat [J/kg·K]",
               "1.73, 1842, 419", "EnergyPlus defaults (generic soil).")
        choice("kiva_deep_ground_boundary", KIVA_DEEP_BC, KIVA_DEEP_BC, "[Kiva] Deep-ground boundary condition",
               "Autoselect", "Autoselect: water table if known; otherwise zero flux.")
        double("kiva_insulation_conductivity", "[Kiva] Perimeter insulation conductivity [W/m·K]", 0.034,
               "For all insulation positions (density 35 kg/m³ and specific heat 1400 J/kg·K).", lo=0.001)
        string("kiva_interior_horizontal_insulation", "[Kiva] Interior horizontal insulation: thickness [m], width [m]",
               "0, 0", "Under the slab, from the wall inwards. Thickness 0 = no insulation in this position.")
        string("kiva_interior_vertical_insulation", "[Kiva] Interior vertical insulation: thickness [m], depth [m]",
               "0, 0", "Inside the foundation wall, from its top downwards.")
        string("kiva_exterior_horizontal_insulation",
               "[Kiva] Exterior horizontal insulation: thickness [m], width [m], depth [m]", "0, 0, 0",
               "Buried outside the building; width from the wall outwards; depth below grade.")
        string("kiva_exterior_vertical_insulation", "[Kiva] Exterior vertical insulation: thickness [m], depth [m]",
               "0, 0", "Outside the foundation wall, from its top downwards.")
        string("kiva_foundation_wall", "[Kiva] Foundation wall: height above grade [m], depth below slab [m], "
               "thickness [m]", "0.2, 0, 0.3",
               "Top of the wall above the outside grade, and extension of the wall below the slab (EnergyPlus "
               "defaults: 0.2 and 0). Concrete wall (1.95 W/m·K, 2240 kg/m³, 900 J/kg·K); used for slabs without "
               "below-grade walls in their zone.")

        b = openstudio.measure.OSArgument.makeBoolArgument("write_shallow_deep", True)
        b.setDisplayName("[Shallow/Deep] Also write these temperatures")
        b.setDescription("Used ONLY by some ground heat exchangers. They do not affect floors or walls.")
        b.setDefaultValue(False)
        args.append(b)
        string("shallow_temperatures", "[Shallow/Deep] Shallow (surface) [°C]", "13,13,13,13,13,13,13,13,13,13,13,13",
               twelve)
        string("deep_temperatures", "[Shallow/Deep] Deep (3-4 m) [°C]", "16,16,16,16,16,16,16,16,16,16,16,16", twelve)

        return args

    def run(self, workspace: openstudio.Workspace, runner: openstudio.measure.OSRunner,
            user_arguments: openstudio.measure.OSArgumentMap):
        """Defines what happens when the measure is run."""
        super().run(workspace, runner, user_arguments)  # Do **NOT** remove this line

        if not runner.validateUserArguments(self.arguments(workspace), user_arguments):
            return False

        a = lambda n: runner.getStringArgumentValue(n, user_arguments)  # noqa: E731
        method = a("method")
        try:
            ground_t = parse_values(a("ground_temperatures"), 12, "[Ground] temperatures")
            fc_source = a("fc_source")
            fc_t = parse_values(a("fc_temperatures"), 12, "[F/C-factor] temperatures") if fc_source == "Manual" else None
            f_factor = runner.getDoubleArgumentValue("fc_f_factor", user_arguments)
            c_factor = runner.getDoubleArgumentValue("fc_c_factor", user_arguments)
            soil = parse_values(a("kiva_soil"), 3, "[Kiva] soil")
            ins_lambda = runner.getDoubleArgumentValue("kiva_insulation_conductivity", user_arguments)
            ins_ih = parse_values(a("kiva_interior_horizontal_insulation"), 2, "[Kiva] interior horizontal insulation")
            ins_iv = parse_values(a("kiva_interior_vertical_insulation"), 2, "[Kiva] interior vertical insulation")
            ins_eh = parse_values(a("kiva_exterior_horizontal_insulation"), 3, "[Kiva] exterior horizontal insulation")
            ins_ev = parse_values(a("kiva_exterior_vertical_insulation"), 2, "[Kiva] exterior vertical insulation")
            wall = parse_values(a("kiva_foundation_wall"), 3, "[Kiva] foundation wall")
            write_sd = runner.getBoolArgumentValue("write_shallow_deep", user_arguments)
            shallow_t = parse_values(a("shallow_temperatures"), 12, "Shallow") if write_sd else None
            deep_t = parse_values(a("deep_temperatures"), 12, "Deep") if write_sd else None
        except ValueError as e:
            runner.registerError(str(e))
            return False
        if any(v < 0 for v in ins_ih + ins_iv + ins_eh + ins_ev + soil + wall):
            runner.registerError("Thicknesses, dimensions and soil properties cannot be negative.")
            return False
        if method == "Kiva":
            if min(soil) <= 0 or wall[2] <= 0:
                runner.registerError("[Kiva] The soil properties and the wall thickness must be greater than 0.")
                return False
            for label, vals in (("interior horizontal", ins_ih), ("interior vertical", ins_iv),
                                ("exterior vertical", ins_ev)):
                if vals[0] > 0 and vals[1] <= 0:
                    runner.registerError(f"[Kiva] {label.capitalize()} insulation: with a thickness > 0, the "
                                         "width/depth must be greater than 0.")
                    return False
        if method == "FCfactor" and f_factor > 1.5 * F_UNINSULATED:
            runner.registerWarning(f"F-factor {f_factor:g} W/m·K: much higher than that of an uninsulated slab "
                                   f"({F_UNINSULATED} W/m·K). Check the value.")

        # --- ground-contact surfaces ---
        surfaces = list(workspace.getObjectsByType(_iot(SURF)))
        ground = [s for s in surfaces if _get(s, "Outside Boundary Condition").upper() in GROUND_BCS]
        other_types = [t for t in ("Floor:Detailed", "Wall:Detailed", "RoofCeiling:Detailed", "Floor:GroundContact",
                                   "Wall:Underground") if workspace.getObjectsByType(_iot(t))]
        if other_types:
            runner.registerWarning(f"The IDF has surfaces of type {', '.join(other_types)}; this measure only "
                                   f"modifies {SURF}.")
        floors = [s for s in ground if _get(s, "Surface Type").upper() == "FLOOR"]
        walls = [s for s in ground if _get(s, "Surface Type").upper() == "WALL"]
        others = [s for s in ground if s not in floors and s not in walls]
        runner.registerInitialCondition(
            f"{len(floors)} slabs, {len(walls)} walls and {len(others)} other surfaces in contact with the ground.")
        if not ground:
            runner.registerAsNotApplicable("The model has no surfaces in contact with the ground.")
            return True
        if others:
            runner.registerWarning("Roof/ceiling surfaces in contact with the ground are kept as Ground: "
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
            runner.registerInfo(f"Slabs: total area {tot_a:.1f} m², exposed perimeter {tot_p:.1f} m, "
                                f"A/P = {tot_a / tot_p:.2f} m (B' = 2·A/P = {2 * tot_a / tot_p:.2f} m).")
        for name, (area, per, exp) in geom.items():
            ap = f"A/P = {area / exp:.2f} m" if exp > TOL else "no exposed perimeter"
            runner.registerInfo(f"  {name} ({zone_of[name]}): area {area:.2f} m², perimeter {per:.2f} m, "
                                f"exposed {exp:.2f} m, {ap}.")

        # weather-file ground temperatures (for information only)
        epw_t = {}
        epw = runner.lastEpwFilePath()
        if epw.is_initialized():
            epw_t = epw_ground_temperatures(str(epw.get()))
        if epw_t:
            for depth, vals in sorted(epw_t.items()):
                runner.registerInfo(f"EPW, ground at {depth:g} m: " + ", ".join(f"{v:.1f}" for v in vals))

        # remove SurfaceProperty:ExposedFoundationPerimeter of the surfaces that will be modified
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
                    "These ground-contact surfaces have no construction: " + ", ".join(empty) + ". Assign one in the "
                    "model (e.g. in the default construction set, 'Ground Contact' section).")
                return False
            bad = [s.nameString() for s in group if construction_type(s) in FC_CONSTRUCTION_TYPES]
            if bad:
                runner.registerError(
                    "These surfaces have F/C-factor constructions and the selected method needs a layered "
                    "construction: " + ", ".join(bad) + ". Assign them a regular construction in the model.")
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
                runner.registerWarning("The [Ground] temperatures are a constant 18 °C, the same as the EnergyPlus "
                                       "default.")

        def split_sources(group):
            """Surfaces with an internal heat source (radiant floor) cannot use F-factor or Kiva."""
            keep = [s for s in group if _get(s, "Construction Name").upper() not in source_cons]
            skipped = [s for s in group if s not in keep]
            if skipped:
                for s in skipped:
                    set_ground(s)
                write_building_surface()
                runner.registerWarning(
                    "Surfaces with an internal heat source (e.g. radiant floor): this method does not support them, so "
                    "they keep the Ground boundary condition and the [Ground] temperatures: "
                    + ", ".join(s.nameString() for s in skipped))
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
                        runner.registerWarning(f"{s.nameString()}: no exposed perimeter and an F-factor construction; "
                                               "assign it a regular construction.")
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
                runner.registerInfo(f"  {s.nameString()}: F-factor {f_factor:g} W/m·K -> equivalent U = F·P/A = "
                                    f"{f_factor * exp / area:.3f} W/m²·K.")
            if adiabatic:
                runner.registerWarning("Slabs without exposed perimeter (surrounded by other slabs): with the F-factor "
                                       "method all the heat loss goes through the perimeter, so they become Adiabatic: "
                                       + ", ".join(adiabatic))
            for s in walls:
                zs = [p[2] for p in surface_vertices(s, transforms)]
                height = max(zs) - min(zs) if zs else 0.0
                if height < TOL:
                    runner.registerWarning(f"{s.nameString()}: zero height; kept as Ground.")
                    set_ground(s)
                    write_building_surface()
                    continue
                c_wall = c_factor
                if c_wall <= 0:
                    r = construction_resistance(workspace, _get(s, "Construction Name"))
                    if r is None:
                        runner.registerError(
                            f"{s.nameString()}: the C-factor of its construction cannot be computed "
                            f"('{_get(s, 'Construction Name')}'). Enter a C-factor greater than 0.")
                        return False
                    c_wall = 1.0 / r
                cname = f"{s.nameString()} C-factor"
                old = workspace.getObjectByTypeAndName(_iot("Construction:CfactorUndergroundWall"), cname)
                if old.is_initialized():
                    workspace.removeObject(old.get().handle())
                origin = (f"from '{_get(s, 'Construction Name')}'" if c_factor <= 0 else "entered")
                _new(workspace, "Construction:CfactorUndergroundWall",
                     {"Name": cname, "C-Factor": _num(c_wall), "Height": _num(height)})
                _set(s, "Construction Name", cname)
                set_bc(s, "GroundFCfactorMethod")
                fix_wall_vertices(s)
                runner.registerInfo(f"  {s.nameString()} ({zone_of[s.nameString()]}): C-factor wall {c_wall:.3f} "
                                    f"W/m²·K ({origin}), below-grade height {height:.2f} m.")
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
                    runner.registerInfo("F/C-factor with the weather-file temperatures at 0.5 m: "
                                        + ", ".join(f"{v:.1f}" for v in epw_t[0.5]))
                elif epw.is_initialized():
                    runner.registerError("The EPW header has no ground temperatures at 0.5 m, so EnergyPlus would stop "
                                         "('no FC Ground Temperatures were input'). Use the 'Manual' source.")
                    return False
                else:
                    runner.registerInfo("F/C-factor: EnergyPlus will use the weather-file temperatures at 0.5 m.")
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
                runner.registerWarning("No slab can use Kiva.")
            settings = _unique(workspace, "Foundation:Kiva:Settings")
            _set(settings, "Soil Conductivity", _num(soil[0]))
            _set(settings, "Soil Density", _num(soil[1]))
            _set(settings, "Soil Specific Heat", _num(soil[2]))
            _set(settings, "Deep-Ground Boundary Condition", runner.getStringArgumentValue("kiva_deep_ground_boundary",
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
                return material(f"Kiva insulation {tag} {thickness * 1000:.0f} mm", thickness, ins_lambda,
                                INSULATION_DENSITY, INSULATION_CP)

            # foundation wall (for slabs without below-grade walls in their zone)
            wall_mat = material(f"Kiva foundation wall concrete {wall[2] * 1000:.0f} mm", wall[2], *CONCRETE)
            wall_cons = f"Kiva foundation wall {wall[2] * 1000:.0f} mm"
            if not workspace.getObjectByTypeAndName(_iot("Construction"), wall_cons).is_initialized():
                obj = openstudio.IdfObject(_iot("Construction"))
                obj.setString(0, wall_cons)
                obj.setString(1, wall_mat)
                workspace.addObject(obj)

            kiva_name = "Kiva foundation"
            for o in workspace.getObjectsByType(_iot("Foundation:Kiva")):
                if o.nameString() == kiva_name:
                    workspace.removeObject(o.handle())
            values = {"Name": kiva_name, "Wall Height Above Grade": _num(wall[0]),
                      "Wall Depth Below Slab": _num(wall[1]), "Footing Wall Construction Name": wall_cons}
            m = insulation("interior horizontal", ins_ih[0])
            if m:
                values.update({"Interior Horizontal Insulation Material Name": m,
                               "Interior Horizontal Insulation Depth": "0",
                               "Interior Horizontal Insulation Width": _num(ins_ih[1])})
            m = insulation("interior vertical", ins_iv[0])
            if m:
                values.update({"Interior Vertical Insulation Material Name": m,
                               "Interior Vertical Insulation Depth": _num(ins_iv[1])})
            m = insulation("exterior horizontal", ins_eh[0])
            if m:
                values.update({"Exterior Horizontal Insulation Material Name": m,
                               "Exterior Horizontal Insulation Width": _num(ins_eh[1]),
                               "Exterior Horizontal Insulation Depth": _num(ins_eh[2])})
            m = insulation("exterior vertical", ins_ev[0])
            if m:
                values.update({"Exterior Vertical Insulation Material Name": m,
                               "Exterior Vertical Insulation Depth": _num(ins_ev[1])})
            _new(workspace, "Foundation:Kiva", values)

            converted = {}

            def massed_construction(cname):
                """Kiva needs massive layers: Material:NoMass/AirGap -> equivalent layer with the same resistance."""
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
                    new_layers.append(material(f"{ln} (Kiva equivalent)", r * EQUIV_LAMBDA, EQUIV_LAMBDA,
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
                    f"Construction '{cname}' has massless layers (Material:NoMass/AirGap) that Kiva does not accept: "
                    f"'{new_name}' is created with equivalent layers of the same resistance "
                    f"(conductivity {EQUIV_LAMBDA} W/m·K).")
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
                    runner.registerInfo(f"  {s.nameString()}: no exposed perimeter; Kiva computes it in 1D against the "
                                        "deep ground.")
            if kiva_walls:
                runner.registerInfo("Below-grade walls computed with Kiva (same zone as a Kiva slab): "
                                    + ", ".join(s.nameString() for s in kiva_walls))
            for s in ground_walls + others:
                set_ground(s)
            if ground_walls or others:
                runner.registerWarning(
                    "Below-grade surfaces in zones without a Kiva slab keep the Ground boundary condition and use the "
                    "[Ground] temperatures: " + ", ".join(s.nameString() for s in ground_walls + others))
                write_building_surface()
            ins_txt = [t for t, v in (("int. horizontal", ins_ih[0]), ("int. vertical", ins_iv[0]),
                                      ("ext. horizontal", ins_eh[0]), ("ext. vertical", ins_ev[0])) if v > 0]
            runner.registerInfo(f"Kiva: {len(floors)} slabs and {len(kiva_walls)} walls; soil conductivity "
                                f"{soil[0]:g} W/m·K, density {soil[1]:g} kg/m³, specific heat {soil[2]:g} J/kg·K; "
                                f"insulation: {', '.join(ins_txt) if ins_txt else 'none'}; foundation wall "
                                f"{wall[2]:g} m.")

        # ------------------------------------------------------------------
        if write_sd:
            for t, vals in (("Site:GroundTemperature:Shallow", shallow_t), ("Site:GroundTemperature:Deep", deep_t)):
                obj = _unique(workspace, t)
                for i, v in enumerate(vals):
                    obj.setString(i, _num(v))
            runner.registerInfo("Site:GroundTemperature:Shallow and :Deep written (ground heat exchangers only).")

        label = dict(METHODS)[method].split(":")[0]
        runner.registerFinalCondition(
            f"{label} method: {n_floors} slabs and {n_walls} walls in contact with the ground processed.")
        return True


# register the measure to be used by the application
GroundCharacteristics().registerWithApplication()

"""Ideal_Loads_All_Params: EnergyPlus Measure (Python) for OpenStudio 3.11.

Copyright (c) 2026 Germán Ramos Ruiz. SPDX-License-Identifier: Apache-2.0

Edits the ideal loads air systems that OpenStudio writes when it translates the model, to enable
heat recovery, an outdoor air economizer and demand-controlled ventilation without modelling a
real HVAC system.

Tested with OpenStudio 3.11.0 (EnergyPlus 25.2.0).

# see the URL below for information on how to write OpenStudio measures
# http://nrel.github.io/OpenStudio-user-documentation/reference/measure_writing_guide/
"""

import openstudio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# With the "Ideal Air Loads" checkbox (Thermal Zones tab), OpenStudio writes this object.
# EnergyPlus then converts it (ExpandObjects) into a ZoneHVAC:IdealLoadsAirSystem.
TEMPLATE_TYPE = "HVACTemplate:Zone:IdealLoadsAirSystem"
# If the equipment was added to the zone from the library (HVAC tab), this object is written instead.
ZONEHVAC_TYPE = "ZoneHVAC:IdealLoadsAirSystem"

# With the "Ideal Air Loads" checkbox, OpenStudio does NOT connect the DesignSpecification:OutdoorAir
# to the ideal loads system: it creates one ZoneVentilation:DesignFlowRate for each non-zero
# component of the DSOA, with these names ("<zone> Ventilation per Person", etc.).
# Checked with the OpenStudio 3.11.0 ForwardTranslator.
OS_VENTILATION_SUFFIXES = (
    " Ventilation per Person",
    " Ventilation per Floor Area",
    " Ventilation Rate",
    " Ventilation Air Changes per Hour",
)

# Output variables that help to explain the heat recovery (names from the I/O Reference).
OUTPUT_VARIABLES = (
    "Zone Ideal Loads Supply Air Total Heating Energy",
    "Zone Ideal Loads Supply Air Total Cooling Energy",
    "Zone Ideal Loads Outdoor Air Total Heating Energy",
    "Zone Ideal Loads Outdoor Air Total Cooling Energy",
    "Zone Ideal Loads Heat Recovery Total Heating Energy",
    "Zone Ideal Loads Heat Recovery Total Cooling Energy",
    "Zone Ideal Loads Heat Recovery Active Time",
    "Zone Ideal Loads Economizer Active Time",
    "Zone Ideal Loads Outdoor Air Standard Density Volume Flow Rate",
    "Zone Ideal Loads Mixed Air Temperature",
)

# EnergyPlus IDD field names (the same in both objects except "Outdoor Air Method").
F_DSOA = "Design Specification Outdoor Air Object Name"
F_DCV = "Demand Controlled Ventilation Type"
F_ECON = "Outdoor Air Economizer Type"
F_HR = "Heat Recovery Type"
F_SENS = "Sensible Heat Recovery Effectiveness"
F_LAT = "Latent Heat Recovery Effectiveness"
F_COOL_LIMIT = "Cooling Limit"
F_MAX_COOL_FLOW = "Maximum Cooling Air Flow Rate"
F_OA_METHOD = "Outdoor Air Method"  # HVACTemplate only


# ---------------------------------------------------------------------------
# Helpers to read and write fields by NAME (not by index), so the measure does not break
# if the field order changes between EnergyPlus versions.
# ---------------------------------------------------------------------------


def _iot(type_name):
    return openstudio.IddObjectType(type_name)


def _field_index(obj, field_name):
    opt = obj.iddObject().getFieldIndex(field_name)
    if not opt.is_initialized():
        raise KeyError(f"Object {obj.iddObject().name()} has no field '{field_name}'")
    return opt.get()


def _get_idx(obj, idx):
    """Text value of field idx ('' if empty or missing)."""
    if idx >= obj.numFields():
        return ""
    val = obj.getString(idx)
    return val.get().strip() if val.is_initialized() else ""


def _get(obj, field_name):
    return _get_idx(obj, _field_index(obj, field_name))


def _set(obj, field_name, value):
    """Writes the field (OpenStudio extends the object if it is truncated)."""
    return obj.setString(_field_index(obj, field_name), str(value))


def _find(workspace, type_name, name):
    opt = workspace.getObjectByTypeAndName(_iot(type_name), name)
    return opt.get() if opt.is_initialized() else None


def _dsoa_has_flow(workspace, dsoa_name):
    """True if the DSOA (or the DSOA:SpaceList) defines any outdoor air flow > 0."""
    if not dsoa_name:
        return False
    dsoa = _find(workspace, "DesignSpecification:OutdoorAir", dsoa_name)
    if dsoa is not None:
        fields = (
            "Outdoor Air Flow per Person",
            "Outdoor Air Flow per Zone Floor Area",
            "Outdoor Air Flow per Zone",
            "Outdoor Air Flow Air Changes per Hour",
        )
        for f in fields:
            v = dsoa.getDouble(_field_index(dsoa, f), True)  # True: use the IDD default if empty
            if v.is_initialized() and v.get() > 0:
                return True
        return False
    space_list = _find(workspace, "DesignSpecification:OutdoorAir:SpaceList", dsoa_name)
    if space_list is not None:
        # Fields: Name, (Space Name i, DSOA Name i), ...
        for i in range(2, space_list.numFields(), 2):
            if _dsoa_has_flow(workspace, _get_idx(space_list, i)):
                return True
    return False


def _zone_dsoa_map(workspace):
    """Zone -> DSOA that OpenStudio assigns to the zone (written in Sizing:Zone)."""
    result = {}
    for sz in workspace.getObjectsByType(_iot("Sizing:Zone")):
        zone = _get_idx(sz, 0)
        dsoa = _get(sz, F_DSOA)
        if zone and dsoa:
            result[zone.upper()] = dsoa
    return result


def _zonehvac_zone_map(workspace):
    """ZoneHVAC:IdealLoadsAirSystem name -> zone it serves (through EquipmentList/Connections)."""
    eqlist_to_zone = {}
    for ec in workspace.getObjectsByType(_iot("ZoneHVAC:EquipmentConnections")):
        eqlist_to_zone[_get_idx(ec, 1).upper()] = _get_idx(ec, 0)
    result = {}
    for el in workspace.getObjectsByType(_iot("ZoneHVAC:EquipmentList")):
        zone = eqlist_to_zone.get(el.nameString().upper(), "")
        for i in range(el.numFields() - 1):
            if _get_idx(el, i).upper() == ZONEHVAC_TYPE.upper():
                result[_get_idx(el, i + 1).upper()] = zone
    return result


def _remove_os_ventilation(workspace, zone_name):
    """Removes the ZoneVentilation:DesignFlowRate objects that OpenStudio generated from the DSOA.

    Only objects with exactly the names OpenStudio uses are removed; those created by the user
    (e.g. natural ventilation) are kept and returned separately.
    """
    targets = {(zone_name + s).upper() for s in OS_VENTILATION_SUFFIXES}
    removed, others = [], []
    for zv in workspace.getObjectsByType(_iot("ZoneVentilation:DesignFlowRate")):
        # Field 1: "Zone or ZoneList or Space or SpaceList Name" (the name has changed between versions)
        if _get_idx(zv, 1).upper() != zone_name.upper():
            continue
        if zv.nameString().upper() in targets:
            removed.append(zv.nameString())
            workspace.removeObject(zv.handle())
        else:
            others.append(zv.nameString())
    return removed, others


def _add_output_variables(workspace, frequency):
    existing = set()
    for ov in workspace.getObjectsByType(_iot("Output:Variable")):
        existing.add((_get_idx(ov, 0).upper(), _get_idx(ov, 1).upper(), _get_idx(ov, 2).upper()))
    added = 0
    for var in OUTPUT_VARIABLES:
        if ("*", var.upper(), frequency.upper()) in existing:
            continue
        ov = openstudio.IdfObject(_iot("Output:Variable"))
        ov.setString(0, "*")
        ov.setString(1, var)
        ov.setString(2, frequency)
        workspace.addObject(ov)
        added += 1
    return added


class IdealLoadsAllParams(openstudio.measure.EnergyPlusMeasure):
    """Heat recovery, economizer and demand-controlled ventilation for ideal loads air systems."""

    def name(self):
        """Returns the human readable name."""
        return "Ideal_Loads_All_Params"

    def description(self):
        """Human readable description."""
        return (
            "Adds to the ideal loads air systems (Ideal Air Loads) a heat recovery unit on the "
            "ventilation air, an outdoor air economizer (free cooling) and occupancy-based ventilation. "
            "It quantifies the savings of heat recovery without modelling a real HVAC system. The heat "
            "recovery is ideal: no fan energy, pressure drop or frost control, and constant effectiveness."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Edits the HVACTemplate:Zone:IdealLoadsAirSystem (Ideal Air Loads checkbox) and "
            "ZoneHVAC:IdealLoadsAirSystem (added from the library) objects of the translated IDF. "
            "IMPORTANT: with the Ideal Air Loads checkbox, OpenStudio 3.11 leaves the DSOA of the ideal "
            "loads empty and translates the DesignSpecification:OutdoorAir into ZoneVentilation:DesignFlowRate "
            "objects (per person with the occupancy schedule; the rest always on). That air does not go "
            "through the system, so heat recovery and economizer would have no effect. With 'Route the "
            "ventilation through the ideal loads' enabled, the measure assigns to each system the DSOA of "
            "its zone (the one in Sizing:Zone, which may be a DesignSpecification:OutdoorAir:SpaceList), "
            "sets Outdoor Air Method = DetailedSpecification and removes the ZoneVentilation objects "
            "generated by OpenStudio (only those with its names; user objects are kept). To compare cases, "
            "also simulate the base case with this measure and heat recovery = None, so that the outdoor "
            "air is identical. With DCV = OccupancySchedule the per-person part follows occupancy (as "
            "OpenStudio does by default); with None the design occupancy is used at all times. The DSOA "
            "schedule, if any, is applied. The economizer requires a maximum cooling air flow: if Cooling "
            "Limit is NoLimit it is changed to LimitFlowRate with Maximum Cooling Air Flow Rate = autosize "
            "(design days are required)."
        )

    def arguments(self, workspace: openstudio.Workspace):
        """Prepares user arguments for the measure."""
        args = openstudio.measure.OSArgumentVector()

        hr_type = openstudio.measure.OSArgument.makeChoiceArgument(
            "heat_recovery_type",
            ["None", "Sensible", "Enthalpy"],
            ["No heat recovery (None)", "Sensible", "Enthalpy: sensible + latent (Enthalpy)"],
            True,
        )
        hr_type.setDisplayName("Heat Recovery Type")
        hr_type.setDescription(
            "Acts only on the outdoor air that goes through the ideal loads system, and only when the "
            "exhaust air is more favourable than the outdoor air."
        )
        hr_type.setDefaultValue("Sensible")
        args.append(hr_type)

        sens_eff = openstudio.measure.OSArgument.makeDoubleArgument("sensible_effectiveness", True)
        sens_eff.setDisplayName("Sensible heat recovery effectiveness (0-1)")
        sens_eff.setDescription("Sensible Heat Recovery Effectiveness. EnergyPlus default: 0.70.")
        sens_eff.setDefaultValue(0.70)
        sens_eff.setMinValue(0.0)
        sens_eff.setMaxValue(1.0)
        args.append(sens_eff)

        lat_eff = openstudio.measure.OSArgument.makeDoubleArgument("latent_effectiveness", True)
        lat_eff.setDisplayName("Latent heat recovery effectiveness (0-1)")
        lat_eff.setDescription(
            "Latent Heat Recovery Effectiveness. Used only with enthalpy heat recovery. "
            "EnergyPlus default: 0.65."
        )
        lat_eff.setDefaultValue(0.65)
        lat_eff.setMinValue(0.0)
        lat_eff.setMaxValue(1.0)
        args.append(lat_eff)

        econ = openstudio.measure.OSArgument.makeChoiceArgument(
            "economizer_type",
            ["NoEconomizer", "DifferentialDryBulb", "DifferentialEnthalpy"],
            [
                "No economizer (NoEconomizer)",
                "Differential dry-bulb (DifferentialDryBulb)",
                "Differential enthalpy (DifferentialEnthalpy)",
            ],
            True,
        )
        econ.setDisplayName("Outdoor Air Economizer Type (free cooling)")
        econ.setDescription(
            "Increases the outdoor air when there is a cooling demand and the outdoor air is more "
            "favourable. Requires a cooling air flow limit (LimitFlowRate, autosize)."
        )
        econ.setDefaultValue("NoEconomizer")
        args.append(econ)

        route_oa = openstudio.measure.OSArgument.makeBoolArgument("route_outdoor_air", True)
        route_oa.setDisplayName("Route the ventilation (DSOA) through the ideal loads")
        route_oa.setDescription(
            "Required for heat recovery and the economizer to work with the Ideal Air Loads checkbox: "
            "connects the zone DSOA and removes the ZoneVentilation objects generated by OpenStudio."
        )
        route_oa.setDefaultValue(True)
        args.append(route_oa)

        dcv = openstudio.measure.OSArgument.makeChoiceArgument(
            "dcv_type",
            ["OccupancySchedule", "None"],
            ["Follows occupancy (OccupancySchedule)", "Constant design flow (None)"],
            True,
        )
        dcv.setDisplayName("Demand Controlled Ventilation Type")
        dcv.setDescription(
            "OccupancySchedule reproduces OpenStudio's default behaviour (the per-person part follows "
            "the occupancy schedule)."
        )
        dcv.setDefaultValue("OccupancySchedule")
        args.append(dcv)

        zone_filter = openstudio.measure.OSArgument.makeStringArgument("zone_name_filter", False)
        zone_filter.setDisplayName("Apply only to zones whose name contains (empty = all)")
        zone_filter.setDefaultValue("")
        args.append(zone_filter)

        out_freq = openstudio.measure.OSArgument.makeChoiceArgument(
            "output_frequency",
            ["None", "Hourly", "Daily", "Monthly", "RunPeriod"],
            ["Do not add", "Hourly", "Daily", "Monthly", "Run period"],
            True,
        )
        out_freq.setDisplayName("Add ideal loads output variables")
        out_freq.setDescription("Heat recovered, outdoor air, economizer hours, etc.")
        out_freq.setDefaultValue("Hourly")
        args.append(out_freq)

        return args

    def run(
        self,
        workspace: openstudio.Workspace,
        runner: openstudio.measure.OSRunner,
        user_arguments: openstudio.measure.OSArgumentMap,
    ):
        """Defines what happens when the measure is run."""
        super().run(workspace, runner, user_arguments)  # Do **NOT** remove this line

        if not (runner.validateUserArguments(self.arguments(workspace), user_arguments)):
            return False

        # ------------------------------------------------------------------
        # 1. Read arguments
        # ------------------------------------------------------------------
        hr_type = runner.getStringArgumentValue("heat_recovery_type", user_arguments)
        sens_eff = runner.getDoubleArgumentValue("sensible_effectiveness", user_arguments)
        lat_eff = runner.getDoubleArgumentValue("latent_effectiveness", user_arguments)
        econ_type = runner.getStringArgumentValue("economizer_type", user_arguments)
        route_oa = runner.getBoolArgumentValue("route_outdoor_air", user_arguments)
        dcv_type = runner.getStringArgumentValue("dcv_type", user_arguments)
        zone_filter = runner.getStringArgumentValue("zone_name_filter", user_arguments).strip()
        out_freq = runner.getStringArgumentValue("output_frequency", user_arguments)

        for label, value in (("sensible", sens_eff), ("latent", lat_eff)):
            if not 0.0 <= value <= 1.0:
                runner.registerError(f"The {label} effectiveness must be between 0 and 1 (value: {value}).")
                return False

        # ------------------------------------------------------------------
        # 2. Find the ideal loads systems and the zone of each one
        # ------------------------------------------------------------------
        templates = list(workspace.getObjectsByType(_iot(TEMPLATE_TYPE)))
        zonehvacs = list(workspace.getObjectsByType(_iot(ZONEHVAC_TYPE)))
        runner.registerInitialCondition(
            f"The IDF has {len(templates)} {TEMPLATE_TYPE} (Ideal Air Loads checkbox) and "
            f"{len(zonehvacs)} {ZONEHVAC_TYPE}."
        )
        if not templates and not zonehvacs:
            runner.registerAsNotApplicable(
                "The model has no ideal loads (tick Ideal Air Loads in the Thermal Zones tab)."
            )
            return True

        zonehvac_zone = _zonehvac_zone_map(workspace)
        systems = [(o, "template", _get_idx(o, 0)) for o in templates]  # field 0 = Zone Name
        systems += [(o, "zonehvac", zonehvac_zone.get(o.nameString().upper(), "")) for o in zonehvacs]

        if zone_filter:
            systems = [s for s in systems if zone_filter.upper() in s[2].upper()]
            if not systems:
                runner.registerAsNotApplicable(f"No zone with ideal loads contains '{zone_filter}'.")
                return True

        # ------------------------------------------------------------------
        # 3. Economizer pre-checks
        # ------------------------------------------------------------------
        use_econ = econ_type != "NoEconomizer"
        if use_econ:
            n_sizing_periods = sum(
                len(workspace.getObjectsByType(_iot(t)))
                for t in (
                    "SizingPeriod:DesignDay",
                    "SizingPeriod:WeatherFileDays",
                    "SizingPeriod:WeatherFileConditionType",
                )
            )
            if n_sizing_periods == 0:
                runner.registerError(
                    "The economizer needs to autosize the maximum cooling air flow and the model has no "
                    "design days. Import a .ddy file in OpenStudio (Site > Design Days) or choose "
                    "NoEconomizer."
                )
                return False

        # ------------------------------------------------------------------
        # 4. Edit each system
        # ------------------------------------------------------------------
        zone_dsoa = _zone_dsoa_map(workspace)
        autosized_any = False
        n_without_oa = 0
        n_vent_removed = 0

        for obj, kind, zone in systems:
            label = zone if zone else obj.nameString()

            # 4a. Outdoor air through the ideal loads
            removed = []
            if route_oa:
                dsoa = zone_dsoa.get(zone.upper(), "") if zone else ""
                if kind == "template":
                    if dsoa:
                        _set(obj, F_OA_METHOD, "DetailedSpecification")
                        _set(obj, F_DSOA, dsoa)
                    else:
                        runner.registerWarning(f"{label}: the zone DSOA was not found in Sizing:Zone.")
                elif not _get(obj, F_DSOA) and dsoa:
                    # OpenStudio usually fills it in for a ZoneHVAC added from the library.
                    _set(obj, F_DSOA, dsoa)
                if zone:
                    removed, others = _remove_os_ventilation(workspace, zone)
                    n_vent_removed += len(removed)
                    if others:
                        runner.registerWarning(
                            f"{label}: user-defined ZoneVentilation objects are kept "
                            f"({', '.join(others)}); that air does not go through the heat recovery."
                        )

            # Does outdoor air actually go through this system?
            if kind == "template":
                method = _get(obj, F_OA_METHOD) or "None"
                if method == "DetailedSpecification":
                    has_oa = _dsoa_has_flow(workspace, _get(obj, F_DSOA))
                else:
                    has_oa = method != "None"
            else:
                has_oa = _dsoa_has_flow(workspace, _get(obj, F_DSOA))

            _set(obj, F_DCV, dcv_type)

            # 4b. Heat recovery
            _set(obj, F_HR, hr_type)
            _set(obj, F_SENS, f"{sens_eff:.4g}")
            _set(obj, F_LAT, f"{lat_eff:.4g}")

            # 4c. Economizer (requires a cooling air flow limit)
            _set(obj, F_ECON, econ_type)
            if use_econ:
                limit = _get(obj, F_COOL_LIMIT) or "NoLimit"
                if limit == "NoLimit":
                    _set(obj, F_COOL_LIMIT, "LimitFlowRate")
                elif limit == "LimitCapacity":
                    _set(obj, F_COOL_LIMIT, "LimitFlowRateAndCapacity")
                if not _get(obj, F_MAX_COOL_FLOW):
                    _set(obj, F_MAX_COOL_FLOW, "autosize")
                    autosized_any = True

            if not has_oa:
                n_without_oa += 1
                if hr_type != "None" or use_econ:
                    runner.registerWarning(
                        f"{label}: no outdoor air goes through the ideal loads, so heat recovery / "
                        "economizer will have no effect. Check the Design Specification Outdoor Air of the "
                        "space type or enable 'Route the ventilation through the ideal loads'."
                    )

            runner.registerInfo(
                f"{label}: HR={hr_type} (sens={sens_eff:.2f}, lat={lat_eff:.2f}), economizer={econ_type}, "
                f"DCV={dcv_type}, DSOA='{_get(obj, F_DSOA)}', ZoneVentilation removed={len(removed)}."
            )

        # Autosizing the cooling air flow requires the zone sizing calculation.
        if autosized_any:
            for sc in workspace.getObjectsByType(_iot("SimulationControl")):
                if _get(sc, "Do Zone Sizing Calculation").upper() != "YES":
                    _set(sc, "Do Zone Sizing Calculation", "Yes")
                    runner.registerInfo("SimulationControl: 'Do Zone Sizing Calculation' enabled.")
        if use_econ:
            runner.registerWarning(
                "With an economizer, the ideal loads cooling is limited to the autosized air flow. Check "
                "the unmet cooling hours (Time Setpoint Not Met); if there are any, increase the Cooling "
                "Sizing Factor in Sizing:Parameters."
            )

        # ------------------------------------------------------------------
        # 5. Output variables
        # ------------------------------------------------------------------
        if out_freq != "None":
            n_added = _add_output_variables(workspace, out_freq)
            runner.registerInfo(f"Added {n_added} Output:Variable objects ({out_freq}).")

        runner.registerFinalCondition(
            f"Modified {len(systems)} ideal loads systems: heat recovery {hr_type}, economizer "
            f"{econ_type}, DCV {dcv_type}. OpenStudio ZoneVentilation objects removed: {n_vent_removed}. "
            f"Systems without outdoor air: {n_without_oa}."
        )
        return True


# register the measure to be used by the application
IdealLoadsAllParams().registerWithApplication()

"""Copyright 2026 Germán Ramos Ruiz y SPDX-License-Identifier: Apache-2.0.

Measure de EnergyPlus (Python) para OpenStudio 3.11.

Edita los sistemas de cargas ideales que escribe OpenStudio al traducir el modelo
para activar el recuperador de calor, el economizador y la ventilación controlada
por ocupación, sin tener que modelar una instalación real.

Probada con OpenStudio 3.11.0 (EnergyPlus 25.2.0).

# see the URL below for information on how to write OpenStudio measures
# http://nrel.github.io/OpenStudio-user-documentation/reference/measure_writing_guide/
"""

import openstudio

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Con la casilla "Ideal Air Loads" (pestaña Thermal Zones), OpenStudio escribe este objeto.
# EnergyPlus lo convierte después (ExpandObjects) en un ZoneHVAC:IdealLoadsAirSystem.
TEMPLATE_TYPE = "HVACTemplate:Zone:IdealLoadsAirSystem"
# Si el equipo se ha añadido a la zona desde la librería (pestaña HVAC), se escribe este otro.
ZONEHVAC_TYPE = "ZoneHVAC:IdealLoadsAirSystem"

# Con la casilla "Ideal Air Loads", OpenStudio NO conecta el DesignSpecification:OutdoorAir
# a las cargas ideales: crea un ZoneVentilation:DesignFlowRate por cada componente no nulo
# del DSOA, con estos nombres ("<zona> Ventilation per Person", etc.).
# Comprobado con el ForwardTranslator de OpenStudio 3.11.0.
OS_VENTILATION_SUFFIXES = (
    " Ventilation per Person",
    " Ventilation per Floor Area",
    " Ventilation Rate",
    " Ventilation Air Changes per Hour",
)

# Variables de salida útiles para explicar el recuperador (nombres del I/O Reference).
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

# Nombres de campo del IDD de EnergyPlus (iguales en los dos objetos salvo "Outdoor Air Method").
F_DSOA = "Design Specification Outdoor Air Object Name"
F_DCV = "Demand Controlled Ventilation Type"
F_ECON = "Outdoor Air Economizer Type"
F_HR = "Heat Recovery Type"
F_SENS = "Sensible Heat Recovery Effectiveness"
F_LAT = "Latent Heat Recovery Effectiveness"
F_COOL_LIMIT = "Cooling Limit"
F_MAX_COOL_FLOW = "Maximum Cooling Air Flow Rate"
F_OA_METHOD = "Outdoor Air Method"  # solo en el HVACTemplate


# ---------------------------------------------------------------------------
# Funciones auxiliares para leer y escribir campos por NOMBRE (no por índice),
# así la measure no se rompe si cambia el orden de campos entre versiones de EnergyPlus.
# ---------------------------------------------------------------------------


def _iot(type_name):
    return openstudio.IddObjectType(type_name)


def _field_index(obj, field_name):
    opt = obj.iddObject().getFieldIndex(field_name)
    if not opt.is_initialized():
        raise KeyError(f"El objeto {obj.iddObject().name()} no tiene el campo '{field_name}'")
    return opt.get()


def _get_idx(obj, idx):
    """Valor de texto del campo idx ('' si está vacío o no existe)."""
    if idx >= obj.numFields():
        return ""
    val = obj.getString(idx)
    return val.get().strip() if val.is_initialized() else ""


def _get(obj, field_name):
    return _get_idx(obj, _field_index(obj, field_name))


def _set(obj, field_name, value):
    """Escribe el campo (si el objeto viene truncado, OpenStudio lo alarga)."""
    return obj.setString(_field_index(obj, field_name), str(value))


def _find(workspace, type_name, name):
    opt = workspace.getObjectByTypeAndName(_iot(type_name), name)
    return opt.get() if opt.is_initialized() else None


def _dsoa_has_flow(workspace, dsoa_name):
    """True si el DSOA (o la DSOA:SpaceList) define algún caudal de aire exterior > 0."""
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
            v = dsoa.getDouble(_field_index(dsoa, f), True)  # True: usa el valor por defecto del IDD si está vacío
            if v.is_initialized() and v.get() > 0:
                return True
        return False
    space_list = _find(workspace, "DesignSpecification:OutdoorAir:SpaceList", dsoa_name)
    if space_list is not None:
        # Campos: Name, (Space Name i, DSOA Name i), ...
        for i in range(2, space_list.numFields(), 2):
            if _dsoa_has_flow(workspace, _get_idx(space_list, i)):
                return True
    return False


def _zone_dsoa_map(workspace):
    """Zona -> DSOA que OpenStudio asigna a esa zona (lo escribe en Sizing:Zone)."""
    result = {}
    for sz in workspace.getObjectsByType(_iot("Sizing:Zone")):
        zone = _get_idx(sz, 0)
        dsoa = _get(sz, F_DSOA)
        if zone and dsoa:
            result[zone.upper()] = dsoa
    return result


def _zonehvac_zone_map(workspace):
    """Nombre del ZoneHVAC:IdealLoadsAirSystem -> zona que sirve (vía EquipmentList/Connections)."""
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
    """Elimina los ZoneVentilation:DesignFlowRate que OpenStudio generó a partir del DSOA.

    Solo borra los que tienen exactamente los nombres que pone OpenStudio; los que haya
    creado el usuario (p. ej. ventilación natural) se mantienen y se devuelven aparte.
    """
    targets = {(zone_name + s).upper() for s in OS_VENTILATION_SUFFIXES}
    removed, others = [], []
    for zv in workspace.getObjectsByType(_iot("ZoneVentilation:DesignFlowRate")):
        # Campo 1: "Zone or ZoneList or Space or SpaceList Name" (el nombre ha cambiado entre versiones)
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
    """An EnergyPlusMeasure."""

    def name(self):
        """Returns the human readable name."""
        return "Ideal_Loads_All_Params"

    def description(self):
        """Human readable description."""
        return (
            "Añade a las cargas ideales (Ideal Air Loads) un recuperador de calor del aire de "
            "ventilación, un economizador (free-cooling) y ventilación según ocupación. Sirve para "
            "cuantificar el ahorro de un recuperador sin modelar una instalación real. Es un "
            "recuperador ideal: no tiene consumo de ventiladores, pérdidas de carga ni control de "
            "escarcha, y su eficiencia es constante."
        )

    def modeler_description(self):
        """Human readable description of modeling approach."""
        return (
            "Edita los objetos HVACTemplate:Zone:IdealLoadsAirSystem (casilla Ideal Air Loads) y "
            "ZoneHVAC:IdealLoadsAirSystem (equipo añadido desde la librería) del IDF traducido. "
            "IMPORTANTE: con la casilla Ideal Air Loads, OpenStudio 3.11 deja vacío el DSOA de las "
            "cargas ideales y traduce el DesignSpecification:OutdoorAir a ZoneVentilation:DesignFlowRate "
            "(per Person con el horario de ocupación; el resto, siempre activos). Ese aire no pasa por "
            "el sistema, así que el recuperador y el economizador no tendrían ningún efecto. Con "
            "'Pasar la ventilación por las cargas ideales' activado, la measure asigna a cada sistema "
            "el DSOA de su zona (el de Sizing:Zone, que puede ser una DesignSpecification:OutdoorAir:"
            "SpaceList), pone Outdoor Air Method = DetailedSpecification y elimina los ZoneVentilation "
            "generados por OpenStudio (solo los que llevan sus nombres; los del usuario se conservan). "
            "Para comparar casos, simule también el caso base con esta measure y recuperador = None, "
            "de modo que el aire exterior sea idéntico. Con DCV = OccupancySchedule la parte por persona "
            "sigue la ocupación (equivalente a lo que hace OpenStudio por defecto); con None se usa la "
            "ocupación de diseño todo el tiempo. El horario del DSOA, si lo tiene, pasa a aplicarse. "
            "El economizador exige un caudal máximo de refrigeración: si Cooling Limit es NoLimit se "
            "cambia a LimitFlowRate con Maximum Cooling Air Flow Rate = autosize (necesita días de diseño)."
        )

    def arguments(self, workspace: openstudio.Workspace):
        """Prepares user arguments for the measure."""
        args = openstudio.measure.OSArgumentVector()

        hr_type = openstudio.measure.OSArgument.makeChoiceArgument(
            "heat_recovery_type",
            ["None", "Sensible", "Enthalpy"],
            ["Sin recuperador (None)", "Sensible", "Entálpico: sensible + latente (Enthalpy)"],
            True,
        )
        hr_type.setDisplayName("Tipo de recuperador de calor (Heat Recovery Type)")
        hr_type.setDescription(
            "Solo actúa sobre el aire exterior que pasa por las cargas ideales y cuando el aire "
            "extraído es más favorable que el exterior."
        )
        hr_type.setDefaultValue("Sensible")
        args.append(hr_type)

        sens_eff = openstudio.measure.OSArgument.makeDoubleArgument("sensible_effectiveness", True)
        sens_eff.setDisplayName("Eficiencia sensible del recuperador (0-1)")
        sens_eff.setDescription("Sensible Heat Recovery Effectiveness. Valor por defecto de EnergyPlus: 0.70.")
        sens_eff.setDefaultValue(0.70)
        sens_eff.setMinValue(0.0)
        sens_eff.setMaxValue(1.0)
        args.append(sens_eff)

        lat_eff = openstudio.measure.OSArgument.makeDoubleArgument("latent_effectiveness", True)
        lat_eff.setDisplayName("Eficiencia latente del recuperador (0-1)")
        lat_eff.setDescription(
            "Latent Heat Recovery Effectiveness. Solo se usa con recuperador entálpico. "
            "Valor por defecto de EnergyPlus: 0.65."
        )
        lat_eff.setDefaultValue(0.65)
        lat_eff.setMinValue(0.0)
        lat_eff.setMaxValue(1.0)
        args.append(lat_eff)

        econ = openstudio.measure.OSArgument.makeChoiceArgument(
            "economizer_type",
            ["NoEconomizer", "DifferentialDryBulb", "DifferentialEnthalpy"],
            [
                "Sin economizador (NoEconomizer)",
                "Diferencial de temperatura seca (DifferentialDryBulb)",
                "Diferencial de entalpía (DifferentialEnthalpy)",
            ],
            True,
        )
        econ.setDisplayName("Economizador / free-cooling (Outdoor Air Economizer Type)")
        econ.setDescription(
            "Aumenta el aire exterior cuando hay demanda de refrigeración y el exterior es más "
            "favorable. Obliga a limitar el caudal de refrigeración (LimitFlowRate, autosize)."
        )
        econ.setDefaultValue("NoEconomizer")
        args.append(econ)

        route_oa = openstudio.measure.OSArgument.makeBoolArgument("route_outdoor_air", True)
        route_oa.setDisplayName("Pasar la ventilación (DSOA) por las cargas ideales")
        route_oa.setDescription(
            "Necesario para que el recuperador y el economizador funcionen con la casilla Ideal Air "
            "Loads: conecta el DSOA de la zona y elimina los ZoneVentilation que genera OpenStudio."
        )
        route_oa.setDefaultValue(True)
        args.append(route_oa)

        dcv = openstudio.measure.OSArgument.makeChoiceArgument(
            "dcv_type",
            ["OccupancySchedule", "None"],
            ["Según ocupación (OccupancySchedule)", "Caudal de diseño constante (None)"],
            True,
        )
        dcv.setDisplayName("Ventilación controlada por demanda (Demand Controlled Ventilation Type)")
        dcv.setDescription(
            "OccupancySchedule reproduce lo que hace OpenStudio por defecto (la parte por persona "
            "sigue al horario de ocupación)."
        )
        dcv.setDefaultValue("OccupancySchedule")
        args.append(dcv)

        zone_filter = openstudio.measure.OSArgument.makeStringArgument("zone_name_filter", False)
        zone_filter.setDisplayName("Aplicar solo a zonas cuyo nombre contenga (vacío = todas)")
        zone_filter.setDefaultValue("")
        args.append(zone_filter)

        out_freq = openstudio.measure.OSArgument.makeChoiceArgument(
            "output_frequency",
            ["None", "Hourly", "Daily", "Monthly", "RunPeriod"],
            ["No añadir", "Horaria", "Diaria", "Mensual", "Periodo completo"],
            True,
        )
        out_freq.setDisplayName("Añadir variables de salida de las cargas ideales")
        out_freq.setDescription("Energía recuperada, aire exterior, horas de economizador, etc.")
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
        # 1. Leer argumentos
        # ------------------------------------------------------------------
        hr_type = runner.getStringArgumentValue("heat_recovery_type", user_arguments)
        sens_eff = runner.getDoubleArgumentValue("sensible_effectiveness", user_arguments)
        lat_eff = runner.getDoubleArgumentValue("latent_effectiveness", user_arguments)
        econ_type = runner.getStringArgumentValue("economizer_type", user_arguments)
        route_oa = runner.getBoolArgumentValue("route_outdoor_air", user_arguments)
        dcv_type = runner.getStringArgumentValue("dcv_type", user_arguments)
        zone_filter = runner.getStringArgumentValue("zone_name_filter", user_arguments).strip()
        out_freq = runner.getStringArgumentValue("output_frequency", user_arguments)

        for label, value in (("sensible", sens_eff), ("latente", lat_eff)):
            if not 0.0 <= value <= 1.0:
                runner.registerError(f"La eficiencia {label} debe estar entre 0 y 1 (valor: {value}).")
                return False

        # ------------------------------------------------------------------
        # 2. Localizar los sistemas de cargas ideales y la zona de cada uno
        # ------------------------------------------------------------------
        templates = list(workspace.getObjectsByType(_iot(TEMPLATE_TYPE)))
        zonehvacs = list(workspace.getObjectsByType(_iot(ZONEHVAC_TYPE)))
        runner.registerInitialCondition(
            f"El IDF tiene {len(templates)} {TEMPLATE_TYPE} (casilla Ideal Air Loads) y "
            f"{len(zonehvacs)} {ZONEHVAC_TYPE}."
        )
        if not templates and not zonehvacs:
            runner.registerAsNotApplicable(
                "No hay cargas ideales en el modelo (marque Ideal Air Loads en Thermal Zones)."
            )
            return True

        zonehvac_zone = _zonehvac_zone_map(workspace)
        systems = [(o, "template", _get_idx(o, 0)) for o in templates]  # campo 0 = Zone Name
        systems += [(o, "zonehvac", zonehvac_zone.get(o.nameString().upper(), "")) for o in zonehvacs]

        if zone_filter:
            systems = [s for s in systems if zone_filter.upper() in s[2].upper()]
            if not systems:
                runner.registerAsNotApplicable(f"Ninguna zona con cargas ideales contiene '{zone_filter}'.")
                return True

        # ------------------------------------------------------------------
        # 3. Comprobaciones previas para el economizador
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
                    "El economizador necesita autodimensionar el caudal máximo de refrigeración y el "
                    "modelo no tiene días de diseño. Importe un archivo .ddy en OpenStudio "
                    "(Site > Design Days) o elija NoEconomizer."
                )
                return False

        # ------------------------------------------------------------------
        # 4. Editar cada sistema
        # ------------------------------------------------------------------
        zone_dsoa = _zone_dsoa_map(workspace)
        autosized_any = False
        n_without_oa = 0
        n_vent_removed = 0

        for obj, kind, zone in systems:
            label = zone if zone else obj.nameString()

            # 4a. Aire exterior a través de las cargas ideales
            removed = []
            if route_oa:
                dsoa = zone_dsoa.get(zone.upper(), "") if zone else ""
                if kind == "template":
                    if dsoa:
                        _set(obj, F_OA_METHOD, "DetailedSpecification")
                        _set(obj, F_DSOA, dsoa)
                    else:
                        runner.registerWarning(f"{label}: no se encontró el DSOA de la zona en Sizing:Zone.")
                elif not _get(obj, F_DSOA) and dsoa:
                    # En un ZoneHVAC añadido desde la librería OpenStudio ya suele rellenarlo.
                    _set(obj, F_DSOA, dsoa)
                if zone:
                    removed, others = _remove_os_ventilation(workspace, zone)
                    n_vent_removed += len(removed)
                    if others:
                        runner.registerWarning(
                            f"{label}: se mantienen ZoneVentilation definidos por el usuario "
                            f"({', '.join(others)}); ese aire no pasa por el recuperador."
                        )

            # ¿Pasa realmente aire exterior por este sistema?
            if kind == "template":
                method = _get(obj, F_OA_METHOD) or "None"
                if method == "DetailedSpecification":
                    has_oa = _dsoa_has_flow(workspace, _get(obj, F_DSOA))
                else:
                    has_oa = method != "None"
            else:
                has_oa = _dsoa_has_flow(workspace, _get(obj, F_DSOA))

            _set(obj, F_DCV, dcv_type)

            # 4b. Recuperador de calor
            _set(obj, F_HR, hr_type)
            _set(obj, F_SENS, f"{sens_eff:.4g}")
            _set(obj, F_LAT, f"{lat_eff:.4g}")

            # 4c. Economizador (necesita un límite de caudal de refrigeración)
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
                        f"{label}: no pasa aire exterior por las cargas ideales, así que el "
                        "recuperador/economizador no tendrá efecto. Revise el Design Specification "
                        "Outdoor Air del space type o active 'Pasar la ventilación por las cargas ideales'."
                    )

            runner.registerInfo(
                f"{label}: HR={hr_type} (sens={sens_eff:.2f}, lat={lat_eff:.2f}), economizador={econ_type}, "
                f"DCV={dcv_type}, DSOA='{_get(obj, F_DSOA)}', ZoneVentilation eliminados={len(removed)}."
            )

        # El autosize del caudal de refrigeración necesita el cálculo de dimensionado de zonas.
        if autosized_any:
            for sc in workspace.getObjectsByType(_iot("SimulationControl")):
                if _get(sc, "Do Zone Sizing Calculation").upper() != "YES":
                    _set(sc, "Do Zone Sizing Calculation", "Yes")
                    runner.registerInfo("SimulationControl: activado 'Do Zone Sizing Calculation'.")
        if use_econ:
            runner.registerWarning(
                "Con economizador, la refrigeración de las cargas ideales queda limitada al caudal "
                "autodimensionado. Revise las horas de refrigeración no satisfechas (Time Setpoint Not "
                "Met); si aparecen, aumente el Cooling Sizing Factor de Sizing:Parameters."
            )

        # ------------------------------------------------------------------
        # 5. Variables de salida
        # ------------------------------------------------------------------
        if out_freq != "None":
            n_added = _add_output_variables(workspace, out_freq)
            runner.registerInfo(f"Añadidas {n_added} Output:Variable ({out_freq}).")

        runner.registerFinalCondition(
            f"Modificados {len(systems)} sistemas de cargas ideales: recuperador {hr_type}, "
            f"economizador {econ_type}, DCV {dcv_type}. ZoneVentilation de OpenStudio eliminados: "
            f"{n_vent_removed}. Sistemas sin aire exterior: {n_without_oa}."
        )
        return True


# register the measure to be used by the application
IdealLoadsAllParams().registerWithApplication()

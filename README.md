# energyplus-tools

Python **OpenStudio Measures** that add EnergyPlus modelling options which the OpenStudio Application does not offer, or offers only in part. They were developed at the University of Navarra (School of Architecture) for teaching and research in building energy simulation.

| Measure | Type | What it adds |
|---|---|---|
| [`ground_characteristics`](ground_characteristics/) | EnergyPlus Measure | Realistic ground heat transfer: monthly ground temperatures, the F/C-factor method or Kiva, configured from the measure without editing the IDF |
| [`ideal_loads_all_params`](ideal_loads_all_params/) | EnergyPlus Measure | Heat recovery (sensible/enthalpy), outdoor-air economizer and demand-controlled ventilation for ideal loads air systems |

**Tested with:** OpenStudio 3.11.0 (EnergyPlus 25.2.0).
**Language:** argument names, descriptions and run messages are currently in **Spanish**.

---

## Installation and use

1. Copy the measure folder (for example `ground_characteristics/`) into your *My Measures* directory. In the OpenStudio Application, you can check or change it under *Preferences → Change My Measures Directory*.
2. In the OpenStudio Application, open the **Measures** tab and drag the measure into the **EnergyPlus Measures** section of the workflow.
3. Set the arguments and run the simulation.

These are **EnergyPlus Measures**: they edit the IDF that OpenStudio generates at run time, not the `.osm` model. Their changes therefore do not appear in the OpenStudio Application. To see what each measure did, check the measure messages in the run log or the `in.idf` of the run.

---

## Ground_characteristics

### Why

Ground contact strongly affects the heating and cooling demand of low-rise buildings, but default settings often handle it poorly:

- If a surface has the boundary condition `Ground` and the model does not define `Site:GroundTemperature:BuildingSurface`, EnergyPlus uses **18 °C all year**. A new OpenStudio model does not write this object, so many models silently run with that constant value.
- The undisturbed ground temperatures in the weather file are not appropriate for `Site:GroundTemperature:BuildingSurface`, as noted in the EnergyPlus Input/Output Reference.
- The better methods are tedious and error-prone to set up by hand:
  - The **F/C-factor method** needs one construction per slab with its area and exposed perimeter, and one per below-grade wall with its height.
  - **Kiva** needs several linked objects plus the exposed perimeter of every slab.

  The OpenStudio SDK includes these objects, but the OpenStudio Application offers limited support for them, especially for Kiva.

This measure lets the user choose the ground model from a drop-down list and computes all geometry-dependent inputs automatically. Students and practitioners can then compare methods without editing the IDF.

### What it does

The **`metodo`** drop-down selects one of three methods:

- **Ground** (`Site:GroundTemperature:BuildingSurface`): every ground-contact surface uses 12 monthly temperatures entered in the measure.
- **F/C-factor** (`Site:GroundTemperature:FCfactorMethod`):
  - **Slabs:** each slab gets its own `Construction:FfactorGroundFloor`, with its area and exposed perimeter computed from the geometry.
  - **Below-grade walls:** each wall gets a `Construction:CfactorUndergroundWall`, with its height computed from the geometry. The C-factor is either given by the user or calculated from the wall's own layered construction as C = 1/Σ(e/λ).
  - **Ground temperatures:** either taken from the **weather file at 0.5 m depth**, which is what EnergyPlus does when the `FCfactorMethod` object is absent, or entered manually.
  - **Slabs with no exposed perimeter** become adiabatic, because the F-factor method assigns all slab losses to the perimeter.
- **Kiva** (`Foundation:Kiva` + `Foundation:Kiva:Settings` + `SurfaceProperty:ExposedFoundationPerimeter`): a two-dimensional finite-difference calculation of the ground. Settings available from the measure:
  - soil properties and the deep-ground boundary condition;
  - perimeter insulation in four positions (interior/exterior, horizontal/vertical), defined by thickness and width or depth;
  - the foundation wall (height above grade, depth below the slab, thickness).

  Below-grade walls in zones that have a Kiva slab, such as basements, are included in the Kiva domain.

Common to all methods:

- **Exposed perimeter, computed automatically.** An edge of a ground-contact floor counts as interior when it is collinear with, and overlaps, an edge of another ground-contact floor. All other edges count as exposed. Zone origins and rotations in the IDF are taken into account.
- **Checks and automatic fixes:**
  - `Material:NoMass` and `Material:AirGap` layers, which Kiva does not accept, are replaced by an equivalent massive layer with the same thermal resistance.
  - Constructions with an internal heat source, such as radiant floors, are not accepted by Kiva or the F/C-factor method; those surfaces are kept with the `Ground` boundary condition and a warning.
  - Surfaces without a construction stop the measure with an explicit error.
  - If the weather file lacks 0.5 m ground temperatures and the F/C-factor method is set to use them, the measure stops with a clear error instead of letting EnergyPlus fail.
  - Wall vertices are re-ordered so that EnergyPlus computes their height correctly. This removes spurious "underground wall height" warnings.
- **Teaching information in the run log:**
  - area, exposed perimeter and A/P of every slab;
  - the equivalent U-value of each F-factor slab (U = F·P/A);
  - the ground temperatures found in the weather file.
- **Optional `Site:GroundTemperature:Shallow` and `:Deep`.** These objects are used only by some ground heat exchanger models. They do **not** affect floors or walls.

### Arguments

| Argument | Default | Description |
|---|---|---|
| `metodo` | `FCfactor` | `Ground`, `FCfactor` or `Kiva`. Only the arguments of the selected method are used. |
| `ground_temperaturas` | 18 ×12 | Monthly temperatures for `Ground` (°C). Also used for any surface that the F/C-factor or Kiva method cannot handle. |
| `fc_origen` | `EPW` | Source of the F/C-factor ground temperatures: weather file at 0.5 m, or `Manual`. |
| `fc_temperaturas` | 15 ×12 | Manual F/C-factor ground temperatures (°C). |
| `fc_f_factor` | 1.264 W/m·K | Slab F-factor. The default is an uninsulated slab (ASHRAE 90.1, F-0.73). From a slab U-value that already includes the ground effect: F = U·A/P. |
| `fc_c_factor` | 0 | Below-grade wall C-factor in W/m²·K, without air films or soil. 0 means it is calculated from each wall's construction. |
| `kiva_terreno` | 1.73, 1842, 419 | Soil conductivity (W/m·K), density (kg/m³) and specific heat (J/kg·K). The defaults are those of EnergyPlus. |
| `kiva_contorno_profundo` | `Autoselect` | Deep-ground boundary: `Autoselect`, `ZeroFlux` or `GroundWater`. |
| `kiva_aislante_lambda` | 0.034 W/m·K | Conductivity of the perimeter insulation. |
| `kiva_ais_int_horizontal` | 0, 0 | Interior horizontal insulation: thickness, width (m). A thickness of 0 means no insulation. |
| `kiva_ais_int_vertical` | 0, 0 | Interior vertical insulation: thickness, depth (m). |
| `kiva_ais_ext_horizontal` | 0, 0, 0 | Exterior horizontal insulation: thickness, width, depth (m). |
| `kiva_ais_ext_vertical` | 0, 0 | Exterior vertical insulation: thickness, depth (m). |
| `kiva_muro` | 0.2, 0, 0.3 | Foundation wall: height above grade, depth below the slab, thickness (m). |
| `escribir_shallow_deep` | false | Also write `Site:GroundTemperature:Shallow` and `:Deep`. |
| `shallow_temperaturas`, `deep_temperaturas` | 13 ×12, 16 ×12 | Monthly values for those objects. |

Monthly values are entered as 12 comma-separated numbers with a decimal point (`2.5,3,...`), or separated by semicolons when a decimal comma is used (`2,5;3;...`).

### Example

The test model has 320 m² over two storeys, uninsulated slabs, ideal loads and the Chicago TMY3 weather file. All results come from EnergyPlus 25.2.

| Ground model | Annual heating demand (vs. 18 °C) | Mean slab heat flux, Jan / Jul (W/m², + into the zone) |
|---|---|---|
| `Ground`, 18 °C constant | 39.3 MWh (reference) | +3.7 / −17.2 |
| F/C-factor, weather file at 0.5 m | 43.8 MWh (+11 %) | −8.2 / −2.2 |
| Kiva, no insulation | 44.0 MWh (+12 %) | −9.1 / −1.1 |
| Kiva, 8 cm exterior vertical (1 m) + 5 cm interior horizontal (1.2 m) | 42.1 MWh (+7 %) | — |

With a constant 18 °C, the slab even supplies heat to the zone in winter and acts as a large heat sink in summer. The F/C-factor and Kiva methods reverse both effects. The magnitude depends on climate, geometry and insulation, so these figures are only an illustration.

### Limitations

- Only `BuildingSurface:Detailed` surfaces are modified. The OpenStudio translator always writes this object type.
- A single `Foundation:Kiva` object is applied to all Kiva slabs, so every slab gets the same insulation and foundation wall.
- The F-factor does not depend on the slab construction. It must be chosen to represent the actual slab, for example from a national-code U-value through F = U·A/P.
- Below-grade walls are handled by Kiva only in zones that have a Kiva slab. Other below-grade walls keep the `Ground` boundary condition and the `ground_temperaturas` values.
- The exposed-perimeter rule assumes that slab edges shared with other ground-contact floors are interior. A slab bordering a crawl space or another non-ground floor at the same level is counted as exposed along that edge.

### Related work

The [Thermal Bridging & Derating (TBD)](https://rd2.github.io/tbd/pages/kiva.html) measure can also generate Kiva objects as part of its thermal-bridging workflow. `Ground_characteristics` is focused on ground modelling only. It switches among the BuildingSurface, F/C-factor and Kiva approaches, so the methods can be compared directly.

---

## Ideal_Loads_All_Params

### Why

Ideal loads air systems are widely used in early design and in teaching, because they give heating and cooling demand without modelling a real HVAC system. However, when ideal loads are enabled with the **"Ideal Air Loads"** checkbox in the *Thermal Zones* tab (checked with OpenStudio 3.11.0):

- OpenStudio writes an `HVACTemplate:Zone:IdealLoadsAirSystem` with **no heat recovery and no economizer**, and these fields cannot be edited from the interface.
- The zone outdoor-air requirement (`DesignSpecification:OutdoorAir`) is **not connected to the ideal loads system**. OpenStudio translates it into separate `ZoneVentilation:DesignFlowRate` objects, so even if heat recovery were enabled, it would have no ventilation air to act on.

As a result, the effect of a heat recovery unit or free cooling on demand cannot be studied with the standard ideal-loads workflow.

### What it does

- Sets the **heat recovery type** (`None`, `Sensible`, `Enthalpy`) and the **sensible and latent effectiveness**.
- Sets the **outdoor air economizer** (`NoEconomizer`, `DifferentialDryBulb`, `DifferentialEnthalpy`). When an economizer is used, the cooling air flow is limited and autosized, as EnergyPlus requires, and zone sizing is enabled.
- **Routes the outdoor air through the ideal loads system.** It assigns the zone's `DesignSpecification:OutdoorAir` and removes the `ZoneVentilation:DesignFlowRate` objects that OpenStudio generated from it. Without this step, heat recovery and the economizer have no effect.
- Sets **demand-controlled ventilation**. `OccupancySchedule` reproduces OpenStudio's default behaviour.
- Can be restricted to zones whose name contains a given text.
- Adds the relevant output variables at the chosen frequency: heat recovered, outdoor-air heating and cooling, and economizer and heat-recovery active time.
- Works both with the checkbox workflow (`HVACTemplate:Zone:IdealLoadsAirSystem`) and with `ZoneHVAC:IdealLoadsAirSystem` objects added from the HVAC library.

### Arguments

| Argument | Default | Description |
|---|---|---|
| `heat_recovery_type` | `Sensible` | `None`, `Sensible` or `Enthalpy`. |
| `sensible_effectiveness` | 0.70 | Sensible heat recovery effectiveness (0–1). |
| `latent_effectiveness` | 0.65 | Latent effectiveness, used only with `Enthalpy` (0–1). |
| `economizer_type` | `NoEconomizer` | `NoEconomizer`, `DifferentialDryBulb` or `DifferentialEnthalpy`. |
| `route_outdoor_air` | true | Connect the zone outdoor air to the ideal loads system and remove the OpenStudio-generated `ZoneVentilation` objects. |
| `dcv_type` | `OccupancySchedule` | `OccupancySchedule` or `None` (constant design flow). |
| `zone_name_filter` | empty | Apply only to zones whose name contains this text. Empty means all zones. |
| `output_frequency` | `Hourly` | Frequency of the added output variables, or `None`. |

### Notes and limitations

- Ideal loads give **thermal demand**, not equipment energy. The heat recovery model uses a constant effectiveness, with no fan energy, part-load or defrost effects.
- With an economizer, the cooling capacity of the ideal loads system is limited to the autosized flow. Check the unmet hours, and increase the cooling sizing factor if needed.
- The `route_outdoor_air` step relies on the names OpenStudio 3.11 gives to the generated `ZoneVentilation:DesignFlowRate` objects (for example, `<zone> Ventilation per Person`). Future OpenStudio versions may change this.

---

## Tests

Each measure includes unit tests that run without a simulation:

```bash
pip install openstudio==3.11.0 pytest
pytest ground_characteristics/tests ideal_loads_all_params/tests
```

## Compatibility with other versions

Both measures read and write EnergyPlus fields **by name**, through the IDD that OpenStudio loads, so they do not depend on field order. They should keep working in later OpenStudio/EnergyPlus versions unless an object, field or output variable is renamed, or the OpenStudio translation changes. The unit tests are the quickest way to check a new version.

## Citation

If you use these measures in research or teaching, please cite them. Use **"Cite this repository"** on the GitHub page, or see [`CITATION.cff`](CITATION.cff).

## License

[Apache License 2.0](LICENSE).

## Author

Germán Ramos Ruiz, Universidad de Navarra, School of Architecture, Department of Building Construction, Services and Structures. ORCID: [0000-0002-6585-2197](https://orcid.org/0000-0002-6585-2197)

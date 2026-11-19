# Changelog

## Unreleased

- Forecast-aware planner: projects each zone six hours ahead with the AC off and starts at
  the latest moment that keeps it in band, so rooms are left to cool or warm naturally when
  the forecast allows. Small, short excursions (≤ 0.3 °C) are tolerated rather than starting
  the whole ducted system for them.
- Short-cycling guard: minimum run and off times, and a target set below the band edge. On a
  replayed hot afternoon this halves compressor starts against simple threshold automations.
- Tiered zones: tier 1 held tightly overnight and set as MyZone, tier 2 next, tier 3 allowed
  to drift; a minimum number of zones stays open whenever the system runs.
- Overnight window (default 17:30–07:00) with pre-conditioning, so tier 1 is in band at the
  window start, and a daytime guard band for every zone outside it.
- Online thermal model per zone (recursive least squares with forgetting), persisted across
  restarts.
- Solar pre-cool on hot afternoons with spare export.
- AC power learnt from the meter-phase step at each start and stop; energy split into solar
  and grid, with sensors usable in the Energy dashboard.
- Nightly review and tuning suggestions over the last seven nights. Suggestions are advice
  only; nothing is changed automatically.
- Sleepwear TOG mode: choosing a TOG sets the tier 1 band from an editable chart, a sensor
  recommends tonight's TOG from the room's natural overnight temperatures, and an optional
  notification sends it at 17:00.
- Shadow mode (plans without acting), a two-hour pause after any manual change, and handover
  that switches listed automations off while Drift is in control and back on when it isn't.

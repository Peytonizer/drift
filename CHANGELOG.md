# Changelog

## Unreleased

- Optional outdoor temperature sensor. A real observation replaces the weather entity's
  model estimate while it's fresh (reported within 90 min), and the gap between them is
  carried into the forecast, fading out over three hours, because a model running warm now
  is usually warm for the next few hours too. Falls back to the weather entity when the
  sensor goes quiet. Prompted by met.no reading 24 °C on a rainy 19 °C evening.
- Relearning from history uses the outdoor sensor's readings where it has them, and the
  weather entity's values only for the time before the sensor started recording.
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
- Natural recovery: a room already outside its band by up to 0.7 °C is left to come back on
  its own when the forecast has it back within 45 minutes (both adjustable). Before this,
  any room over its band by more than 0.3 °C started the AC at once, even on a cooling
  evening. It applies only to the excursion the room is in now; future breaches are still
  planned against, so the room is ready for bedtime.
- TOG bands are the chart ranges themselves, not trimmed by 0.5 °C. Trimming made the AC
  start earlier than the chart needs; the cool and heat depths already set where it aims.
  The default chart follows Love to Dream's Australian ratings and adds 1.5 TOG (18–22 °C).
- Room models are trained from the last week of recorder history on first start, so the
  first night's predictions don't rest on a single evening (which can mislead: a house still
  giving off a hot day's heat looks like a room that never cools). A button retrains them.
- Outage handling for the AC: commands are checked every minute and resent up to three
  times; an unavailable AC holds its last known state; a mismatch during or just after an
  outage, or while a command is being retried, is no longer taken as a manual change (which
  used to pause Drift for two hours). Persistent trouble raises a notification with the tier
  1 room's temperature, and the tier 1 room leaving its band triggers an immediate check.
- Constant zones: zones listed as the AC's pressure-relief zones are never switched off
  while Drift is running, so the AC's controller can always open them. Before this they
  were closed with the other tier 3 zones when they didn't need air.
- Daytime comfort rules, added as sub-entries on the integration page: a schedule helper's
  weekly blocks or presence sensors (with a dwell so walking through doesn't count, and a
  hold after the last detection), giving the listed zones a fixed range or tier 1's TOG
  range. Nights are unchanged. Schedules are read ahead so rooms are ready when a block
  starts. The planner now asks for each zone's band by zone rather than by tier.
- Shadow mode (plans without acting), a two-hour pause after any manual change, and handover
  that switches listed automations off while Drift is in control and back on when it isn't.

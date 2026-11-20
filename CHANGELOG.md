# Changelog

## Unreleased

- Two restart gaps closed. If the AC isn't loaded when Drift starts, then once it reports Drift
  now recognises its own run (keeping the run's true start, who began it, and not counting a
  new start), so the minimum-run hold and the start statistics survive a full Home Assistant
  restart. And while the AC hasn't reported since a restart, Drift sends it nothing
  (status `waiting_for_ac`) instead of acting on a placeholder mode.
- Forecast-aware target. When Drift runs the AC for a bedroom (tier 1 or 2) it now aims for the
  shallowest target from which the room's own drift stays in its band for the next 12 hours,
  instead of always cooling (or heating) a fixed depth inside the band edge. On a warm night
  that is the same deep target as before; on a cold night it is shallower, so the room is not
  cooled and then left to fall below its band and need heat. Heat is the mirror. When no target
  is clean it avoids the opposite mode where it can. Rooms with fewer than 50 learned samples,
  tier 3 zones and solar pre-cool keep the fixed depth.
- Whole-degree setpoints. The AC steps in whole degrees, so Drift now sends a whole number (cool
  rounds down, heat up, within the unit's own range) and keeps its own 0.1 °C stop temperature,
  checking each room against that every cycle. This also stops a fractional value being resent
  to the AC every cycle. `sensor.drift_status` zone commands now include both `setpoint` and `target`.
- Restart and handover safety, after a restart-heavy evening where the AC started six times in
  30 minutes:
  - The minimum-run hold only applies to a run Drift started. A run inherited across a restart,
    or begun by a person, is never held, so the planner can stop it when it isn't needed.
  - A restart is no longer treated as an AC outage. While the AC entity hasn't loaded yet Drift
    waits, and when it first reports there is no "recently back" grace, so a person's change in
    the first ten minutes is respected rather than resent. Real outages keep the grace.
  - Once Drift's command has been seen to land, any later departure from it counts as a person,
    even inside the three-minute allowance for a slow echo (which still covers a command in flight).
  - Times in reasons ("minimum run until …") are local, not UTC.
  - The saved state gains `run_is_ours` and `expected_mode`; saves from older versions load safely.
- Slight excursions (within tolerance) are now left alone however long they last, provided
  the room comes back on its own within the look-ahead; previously only short ones were. On
  the first live evening Drift started the whole system to shave 0.3 °C off a room that was
  already falling. Applies to cooling and heating alike.
- A manual pause, the last stop time and the current run's start are saved, so a restart or
  an options reload no longer lets Drift undo someone stopping the AC or restart the
  compressor inside its minimum off time.
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

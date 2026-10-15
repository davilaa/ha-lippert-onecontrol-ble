"""Constants for the Compass Console (Lippert OneControl) integration."""

DOMAIN = "compass_console"

CONF_ADDRESS = "address"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 60

# Nice per-function Material Design icons (by IDS-CAN FUNCTION_NAME id).
FUNC_ICONS = {
    3: "mdi:water-boiler",    # Gas Water Heater
    4: "mdi:water-boiler",    # Electric Water Heater
    5: "mdi:water-pump",      # Water Pump
    7: "mdi:lightbulb",       # Light (generic)
    61: "mdi:ceiling-light",  # Ceiling Light
    143: "mdi:coach-lamp",    # Exterior Light
}
SWITCH_DEFAULT_ICON = "mdi:toggle-switch-variant"

TANK_ICONS = {
    67: "mdi:water",          # Fresh Tank
    68: "mdi:cup-water",      # Grey Tank
    69: "mdi:toilet",         # Black Tank
}
TANK_DEFAULT_ICON = "mdi:gauge"

# Covers (momentary H-bridges): slides / awnings.
COVER_ICONS = {
    96: "mdi:arrow-split-vertical",  # Slide (out/in)
    97: "mdi:arrow-split-vertical",  # Main Slide
    105: "mdi:awning-outline",       # Awning
}
COVER_DEFAULT_ICON = "mdi:cog-transfer"
# Max seconds to drive a slide/awning per open/close press before auto-stopping (safety cap).
COVER_MAX_MOVE_SECONDS = 120
# Default seconds of motor run for a full travel. These H-bridges report no position feedback,
# so percentage/position is ESTIMATED from run time. Open and close are timed separately
# (awnings extend/retract at different speeds); each is editable as a Number entity on the
# device page. Legacy single values are treated as both directions.
COVER_FULL_TRAVEL_SECONDS = 30.0
# Options key holding per-cover travel overrides: {cover_key: {"open": secs, "close": secs}}.
COVER_TRAVEL_OPT = "cover_travel"
# Dispatcher signal prefix a cover fires (as f"{SIGNAL_COVER_POS}_{key}") when its
# estimated position changes, so the matching Position number entity can refresh live.
SIGNAL_COVER_POS = "compass_cover_pos"

# End-stop (stall) detection. When an awning motor reaches a physical limit the current jumps
# well above its running draw (measured: ~6A running -> ~14A stall when closing; ~1.5A running
# -> ~8.5A at the open limit). Closing drives until this stall (fully retract); opening uses it
# as an over-travel safeguard. The threshold is editable per cover on the device page.
# Awning func ids only (slides can draw more while moving; not yet characterized).
COVER_STALL_FUNCS = {105, 138, 139, 140}
COVER_STALL_AMPS = 7.0          # default: above running draw (open ~1.5A, close ~6A), below limits
COVER_STALL_HOLD_SECONDS = 0.6  # must stay above threshold this long to confirm
COVER_STALL_ARM_SECONDS = 4.0   # ignore the first few seconds (startup / early running current)
# Options key holding per-cover stall thresholds (amps): {cover_key: amps}.
COVER_STALL_OPT = "cover_stall"
# Options key: True while the console is released for the phone app (persists across restarts).
RELEASED_OPT = "released"

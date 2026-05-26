"""Constants for the Terma Smart integration."""
from datetime import timedelta

DOMAIN = "terma"
CONF_SERIAL = "serial"
CONF_DEVICES = "devices"
DEFAULT_PORT = 5005

UPDATE_INTERVAL = timedelta(seconds=30)

# Climate bounds — match the Terma app's slider range and step.
MIN_TEMP_C = 5.0
MAX_TEMP_C = 30.0
TEMP_STEP_C = 0.5

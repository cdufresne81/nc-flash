"""
Diagnostic Trouble Code (DTC) and Negative Response Code (NRC) Tables

Mazda NC Miata DTC lookup and UDS NRC descriptions.
DTC codes use standard OBD-II encoding: high 2 bits select category
(P=0b00, C=0b01, B=0b10, U=0b11), remaining 14 bits are the numeric code.
"""

# --- UDS Negative Response Codes (ISO 14229) ---

NRC_TABLE: dict[int, str] = {
    0x10: "General reject",
    0x11: "Service not supported",
    0x12: "Sub-function not supported",
    0x13: "Incorrect message length or invalid format",
    0x14: "Response too long",
    0x21: "Busy - repeat request",
    0x22: "Conditions not correct",
    0x24: "Request sequence error",
    0x25: "No response from sub-net component",
    0x26: "Failure prevents execution of requested action",
    0x31: "Request out of range",
    0x33: "Security access denied",
    0x35: "Invalid key",
    0x36: "Exceeded number of attempts",
    0x37: "Required time delay not expired",
    0x70: "Upload/download not accepted",
    0x71: "Transfer data suspended",
    0x72: "General programming failure",
    0x73: "Wrong block sequence counter",
    0x78: "Request correctly received - response pending",
    0x7E: "Sub-function not supported in active session",
    0x7F: "Service not supported in active session",
}

# --- Mazda NC Miata Diagnostic Trouble Codes ---
# P-codes: category bits 0b00 (powertrain), raw value = numeric part
# C-codes: category bits 0b01 (chassis), raw value = 0x4000 | numeric part

DTC_TABLE: dict[int, str] = {
    # CMP/CKP timing
    0x0011: "P0011 - Camshaft position timing over-advanced (Bank 1)",
    0x0012: "P0012 - Camshaft position timing over-retarded (Bank 1)",
    0x0013: "P0013 - Exhaust camshaft position actuator circuit (Bank 1)",
    0x0014: "P0014 - Exhaust camshaft position timing over-advanced (Bank 1)",
    0x0015: "P0015 - Exhaust camshaft position timing over-retarded (Bank 1)",
    0x0016: "P0016 - Crankshaft/camshaft position correlation (Bank 1 Sensor A)",
    # O2/A-F sensor heater circuits
    0x0030: "P0030 - HO2S heater control circuit (Bank 1 Sensor 1)",
    0x0031: "P0031 - HO2S heater control circuit low (Bank 1 Sensor 1)",
    0x0032: "P0032 - HO2S heater control circuit high (Bank 1 Sensor 1)",
    0x0036: "P0036 - HO2S heater control circuit (Bank 1 Sensor 2)",
    0x0037: "P0037 - HO2S heater control circuit low (Bank 1 Sensor 2)",
    0x0038: "P0038 - HO2S heater control circuit high (Bank 1 Sensor 2)",
    0x0040: "P0040 - O2 sensor signals swapped (Bank 1 Sensor 1 / Bank 2 Sensor 1)",
    0x0041: "P0041 - O2 sensor signals swapped (Bank 1 Sensor 2 / Bank 2 Sensor 2)",
    0x0050: "P0050 - HO2S heater control circuit (Bank 2 Sensor 1)",
    0x0051: "P0051 - HO2S heater control circuit low (Bank 2 Sensor 1)",
    0x0052: "P0052 - HO2S heater control circuit high (Bank 2 Sensor 1)",
    0x0053: "P0053 - HO2S heater resistance (Bank 1 Sensor 1)",
    0x0054: "P0054 - HO2S heater resistance (Bank 1 Sensor 2)",
    # MAF sensor
    0x0101: "P0101 - Mass air flow sensor range/performance",
    0x0102: "P0102 - Mass air flow sensor circuit low",
    0x0103: "P0103 - Mass air flow sensor circuit high",
    # MAP sensor
    0x0107: "P0107 - Manifold absolute pressure sensor circuit low",
    0x0108: "P0108 - Manifold absolute pressure sensor circuit high",
    # IAT sensor
    0x0112: "P0112 - Intake air temperature sensor circuit low",
    0x0113: "P0113 - Intake air temperature sensor circuit high",
    # ECT sensor
    0x0117: "P0117 - Engine coolant temperature sensor circuit low",
    0x0118: "P0118 - Engine coolant temperature sensor circuit high",
    # TP sensor A
    0x0122: "P0122 - Throttle position sensor A circuit low",
    0x0123: "P0123 - Throttle position sensor A circuit high",
    # Coolant temperature
    0x0125: "P0125 - Insufficient coolant temperature for closed loop fuel control",
    # O2 sensor circuits (Bank 1)
    0x0131: "P0131 - O2 sensor circuit low voltage (Bank 1 Sensor 1)",
    0x0132: "P0132 - O2 sensor circuit high voltage (Bank 1 Sensor 1)",
    0x0133: "P0133 - O2 sensor slow response (Bank 1 Sensor 1)",
    0x0134: "P0134 - O2 sensor no activity detected (Bank 1 Sensor 1)",
    0x0135: "P0135 - O2 sensor heater circuit malfunction (Bank 1 Sensor 1)",
    0x0136: "P0136 - O2 sensor circuit malfunction (Bank 1 Sensor 2)",
    0x0137: "P0137 - O2 sensor circuit low voltage (Bank 1 Sensor 2)",
    0x0138: "P0138 - O2 sensor circuit high voltage (Bank 1 Sensor 2)",
    # Fuel trim
    0x0171: "P0171 - System too lean (Bank 1)",
    0x0172: "P0172 - System too rich (Bank 1)",
    # TP sensor B
    0x0222: "P0222 - Throttle position sensor B circuit low",
    0x0223: "P0223 - Throttle position sensor B circuit high",
    # Misfires
    0x0300: "P0300 - Random/multiple cylinder misfire detected",
    0x0301: "P0301 - Cylinder 1 misfire detected",
    0x0302: "P0302 - Cylinder 2 misfire detected",
    0x0303: "P0303 - Cylinder 3 misfire detected",
    0x0304: "P0304 - Cylinder 4 misfire detected",
    # Knock sensor
    0x0327: "P0327 - Knock sensor 1 circuit low (Bank 1)",
    0x0328: "P0328 - Knock sensor 1 circuit high (Bank 1)",
    # CKP/CMP sensor circuits
    0x0335: "P0335 - Crankshaft position sensor A circuit malfunction",
    0x0336: "P0336 - Crankshaft position sensor A circuit range/performance",
    0x0337: "P0337 - Crankshaft position sensor A circuit low",
    0x0338: "P0338 - Crankshaft position sensor A circuit high",
    0x0339: "P0339 - Crankshaft position sensor A circuit intermittent",
    0x0340: "P0340 - Camshaft position sensor circuit malfunction (Bank 1 Sensor A)",
    # Ignition coil circuits
    0x0351: "P0351 - Ignition coil A primary/secondary circuit",
    0x0352: "P0352 - Ignition coil B primary/secondary circuit",
    0x0353: "P0353 - Ignition coil C primary/secondary circuit",
    0x0354: "P0354 - Ignition coil D primary/secondary circuit",
    # EGR
    0x0401: "P0401 - Exhaust gas recirculation flow insufficient",
    0x0402: "P0402 - Exhaust gas recirculation flow excessive",
    # Catalyst
    0x0420: "P0420 - Catalyst system efficiency below threshold (Bank 1)",
    # EVAP system
    0x0443: "P0443 - EVAP emission control system purge control valve circuit",
    0x0451: "P0451 - EVAP emission control system pressure sensor range/performance",
    0x0452: "P0452 - EVAP emission control system pressure sensor low",
    0x0453: "P0453 - EVAP emission control system pressure sensor high",
    0x0455: "P0455 - EVAP emission control system leak detected (large leak)",
    0x0456: "P0456 - EVAP emission control system leak detected (small leak)",
    # Fuel level sensor
    0x0460: "P0460 - Fuel level sensor circuit malfunction",
    0x0461: "P0461 - Fuel level sensor circuit range/performance",
    0x0462: "P0462 - Fuel level sensor circuit low",
    0x0463: "P0463 - Fuel level sensor circuit high",
    # Cooling fan relays
    0x0480: "P0480 - Cooling fan relay 1 control circuit",
    0x0481: "P0481 - Cooling fan relay 2 control circuit",
    # Vehicle speed sensor
    0x0500: "P0500 - Vehicle speed sensor malfunction",
    # Idle speed control
    0x0506: "P0506 - Idle control system RPM lower than expected",
    0x0507: "P0507 - Idle control system RPM higher than expected",
    # Power steering pressure sensor
    0x0550: "P0550 - Power steering pressure sensor circuit malfunction",
    # Brake switch
    0x0571: "P0571 - Brake switch A circuit malfunction",
    # PCM internal errors
    0x0601: "P0601 - Internal control module memory check sum error (ROM)",
    0x0602: "P0602 - Control module programming error",
    0x0603: "P0603 - Internal control module keep alive memory (KAM) error (RAM)",
    0x0604: "P0604 - Internal control module random access memory (RAM) error",
    # Throttle actuator control range
    0x0638: "P0638 - Throttle actuator control range/performance (Bank 1)",
    # Intake manifold tuning valve
    0x0660: "P0660 - Intake manifold tuning valve control circuit (Bank 1)",
    # Intake manifold runner (P2xxx range)
    0x2004: "P2004 - Intake manifold runner control stuck open (Bank 1)",
    0x2005: "P2005 - Intake manifold runner control stuck open (Bank 2)",
    0x2006: "P2006 - Intake manifold runner control stuck closed (Bank 1)",
    0x2007: "P2007 - Intake manifold runner control stuck closed (Bank 2)",
    0x2008: "P2008 - Intake manifold runner control circuit open (Bank 1)",
    0x2009: "P2009 - Intake manifold runner control circuit low (Bank 1)",
    # Intake manifold tuning valve stuck
    0x2070: "P2070 - Intake manifold tuning valve stuck open",
    # Post catalyst fuel trim
    0x2096: "P2096 - Post catalyst fuel trim system too lean (Bank 1)",
    0x2097: "P2097 - Post catalyst fuel trim system too rich (Bank 1)",
    # Throttle actuator control
    0x2101: "P2101 - Throttle actuator control motor circuit range/performance",
    0x2102: "P2102 - Throttle actuator control motor circuit low",
    0x2103: "P2103 - Throttle actuator control motor circuit high",
    0x2104: "P2104 - Throttle actuator control system forced idle",
    0x2105: "P2105 - Throttle actuator control system forced engine shutdown",
    0x2106: "P2106 - Throttle actuator control system forced limited power",
    0x2118: "P2118 - Throttle actuator control motor current range/performance",
    0x2119: "P2119 - Throttle actuator control throttle body range/performance",
    # TP sensor D
    0x2122: "P2122 - Throttle position sensor D circuit low",
    0x2123: "P2123 - Throttle position sensor D circuit high",
    # TP sensor E
    0x2127: "P2127 - Throttle position sensor E circuit low",
    0x2128: "P2128 - Throttle position sensor E circuit high",
    # TP sensor correlations
    0x2135: "P2135 - Throttle position sensor A/B voltage correlation",
    0x2138: "P2138 - Throttle position sensor D/E voltage correlation",
    # Throttle actuator idle position
    0x2176: "P2176 - Throttle actuator control system idle position not learned",
    # Fuel trim system lean/rich
    0x2177: "P2177 - System too lean off idle (Bank 1)",
    0x2178: "P2178 - System too rich off idle (Bank 1)",
    # Fuel trim system lean/rich at idle
    0x2187: "P2187 - System too lean at idle (Bank 1)",
    0x2188: "P2188 - System too rich at idle (Bank 1)",
    # O2 sensor signal biased
    0x2195: "P2195 - O2 sensor signal biased/stuck lean (Bank 1 Sensor 1)",
    0x2196: "P2196 - O2 sensor signal biased/stuck rich (Bank 1 Sensor 1)",
    # Barometric pressure sensor
    0x2227: "P2227 - Barometric pressure sensor circuit range/performance",
    # ECM/PCM internal engine off timer
    0x2610: "P2610 - ECM/PCM internal engine off timer performance",
    # --- Chassis codes (C-codes, category bits 0b01 = 0x4000) ---
    0x4073: "C0073 - Control module communication bus off",
    0x4101: "C0101 - CAN communication - signal from brake module lost",
    0x4121: "C0121 - CAN communication - signal from ABS module lost",
    0x4155: "C0155 - CAN communication - signal from stability control module lost",
}


def get_dtc_prefix(code: int) -> str:
    """Map DTC high bits to category letter (P/C/B/U)."""
    category = (code >> 14) & 0x03
    return {0: "P", 1: "C", 2: "B", 3: "U"}.get(category, "?")


def format_dtc(code: int) -> str:
    """Format a raw DTC code as standard OBD-II string (e.g., 'P0011')."""
    prefix = get_dtc_prefix(code)
    numeric = code & 0x3FFF
    return f"{prefix}{numeric:04X}"


def get_dtc_description(code: int) -> str:
    """Get human-readable description for a DTC code."""
    return DTC_TABLE.get(code, f"Unknown DTC ({format_dtc(code)})")


def get_nrc_description(nrc: int) -> str:
    """Get description for a UDS Negative Response Code."""
    return NRC_TABLE.get(nrc, f"Unknown NRC (0x{nrc:02X})")

# keysight-echem

A Python library for running electrochemical experiments from the Keysight B2901A / B2901BL Source-Measure Unit (SMU). Provides a clean interface for common galvanostatic and potentiostatic techniques, with built-in CSV logging, live plotting, and hardware safety limits.

Designed for 4-wire (Kelvin) sensing with the counter/sense terminals connected to a reference electrode, as is typical in 3-electrode electrochemical cells.

## Installation

```bash
pip install keysight-echem
```

## Supported methods

- `constant_current` — galvanostatic hold for a fixed duration
- `constant_voltage` — potentiostatic hold for a fixed duration
- `constant_current_until_cutoff` — galvanostatic hold until a voltage limit is reached
- `constant_voltage_until_cutoff` — potentiostatic hold until a current limit is reached
- `charge_discharge_cycle` — automated galvanostatic charge/discharge cycling (GCD)
- `cyclic_voltammetry` — software-stepped CV sweep at a specified scan rate

All methods accept an optional `LivePlotter` for real-time V and I strip-charts, and write to a thread-safe CSV via `DataLogger`.

---

## Usage

### Finding your instrument address

```python
import pyvisa
rm = pyvisa.ResourceManager()
print(rm.list_resources())
# e.g. 'USB0::0x2A8D::0x9101::MY63320360::INSTR'
```

---

### Constant current hold

```python
from keysight_echem import KeysightB2901, DataLogger, LivePlotter, SafetyLimits

ADDR = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'

safety  = SafetyLimits(max_voltage=4.5, min_voltage=-0.5,
                       max_current=1.0, min_current=-1.0)
plotter = LivePlotter(title='CC Hold', current_unit='mA')

with KeysightB2901(ADDR, safety=safety) as smu:
    with DataLogger('cc_hold.csv') as log:
        smu.constant_current(
            current=0.5,          # A
            duration=120,         # s
            sample_rate=10,       # Hz
            voltage_compliance=5.0,
            logger=log,
            plotter=plotter,
        )

plotter.save('cc_hold.png')
plotter.keep_open()
```

---

### GITT (Galvanostatic Intermittent Titration Technique)

GITT consists of a series of current pulses each followed by a relaxation period. It can be built directly from `constant_current` and `constant_current_until_cutoff` in a loop:

```python
from keysight_echem import KeysightB2901, DataLogger, LivePlotter
import time

ADDR   = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'
PULSES = 20

plotter = LivePlotter(title='GITT', current_unit='mA')

with KeysightB2901(ADDR) as smu:
    with DataLogger('gitt.csv') as log:
        for i in range(PULSES):
            # --- current pulse ---
            smu.constant_current(
                current=0.1,            # A
                duration=60,            # s per pulse
                sample_rate=10,
                voltage_compliance=5.0,
                logger=log,
                plotter=plotter,
                status=f'pulse_{i+1}',
            )

            # --- relaxation (output already off; just wait and measure at OCV) ---
            smu.constant_current(
                current=0.0,            # zero current = open circuit measurement
                duration=300,           # s relaxation
                sample_rate=2,
                voltage_compliance=5.0,
                logger=log,
                plotter=plotter,
                status=f'relax_{i+1}',
            )

plotter.save('gitt.png')
plotter.keep_open()
```

Each pulse/relax pair is labelled in the CSV and plotted in a distinct colour, so the titration steps are immediately visible.

---

### Conductance-based MPPT (Maximum Power Point Tracking)

The incremental conductance algorithm tracks the maximum power point of a source (e.g. a photoelectrochemical cell) by comparing the incremental conductance `dI/dV` to the instantaneous conductance `I/V` and stepping the current accordingly:

- If `dI/dV + I/V > 0` → increase current (moving toward MPP)
- If `dI/dV + I/V < 0` → decrease current (overshot MPP)
- If `dI/dV + I/V ≈ 0` → at MPP, hold

```python
from keysight_echem import KeysightB2901, DataLogger, LivePlotter
import time

ADDR       = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'
I_MIN      = 0.0    # A — lower current bound
I_MAX      = 0.5    # A — upper current bound
I_STEP     = 0.005  # A — perturbation step size
TOLERANCE  = 1e-4   # conductance tolerance for "at MPP"
INTERVAL   = 0.5    # s between MPPT updates
DURATION   = 600    # s total run time

plotter = LivePlotter(title='MPPT', current_unit='mA')

with KeysightB2901(ADDR) as smu:
    with DataLogger('mppt.csv') as log:

        current_setpoint = 0.05   # A — initial guess
        v_prev, i_prev   = None, None
        t_end = time.time() + DURATION

        while time.time() < t_end:
            # Apply current and take a single timed sample
            smu.constant_current(
                current=current_setpoint,
                duration=INTERVAL,
                sample_rate=1,
                voltage_compliance=5.0,
                logger=log,
                plotter=plotter,
                status='mppt',
            )

            # Read the last logged values back from the plotter's buffer
            seg = plotter._segments.get('mppt')
            if seg is None or len(seg['v']) < 2:
                continue

            v_now = seg['v'][-1]
            i_now = seg['i'][-1] / 1e3   # plotter stores in mA; convert back to A

            if v_prev is not None:
                dV = v_now - v_prev
                dI = i_now - i_prev

                if abs(dV) > 1e-6:   # avoid division by zero
                    inc_conductance  = dI / dV
                    inst_conductance = i_now / v_now if abs(v_now) > 1e-6 else 0.0
                    error = inc_conductance + inst_conductance

                    if error > TOLERANCE:
                        current_setpoint = min(current_setpoint + I_STEP, I_MAX)
                    elif error < -TOLERANCE:
                        current_setpoint = max(current_setpoint - I_STEP, I_MIN)
                    # else: within tolerance, hold current

            v_prev, i_prev = v_now, i_now

plotter.save('mppt.png')
plotter.keep_open()
```

> **Note:** For a real MPPT deployment you would tune `I_STEP`, `INTERVAL`, and `TOLERANCE` to match the time constants of your cell. Smaller steps give finer tracking at the cost of convergence speed.

---

## Safety limits

All methods respect a `SafetyLimits` object that acts as a software-level emergency stop independent of the instrument's own compliance settings:

```python
from keysight_echem import SafetyLimits

safety = SafetyLimits(
    max_voltage =  4.5,   # V
    min_voltage = -0.5,   # V
    max_current =  1.0,   # A
    min_current = -1.0,   # A
)

smu = KeysightB2901(ADDR, safety=safety)
```

If either boundary is exceeded during any measurement the output is switched off immediately and the loop exits.

---

## License

MIT. Not affiliated with or endorsed by Keysight Technologies.

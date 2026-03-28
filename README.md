\# keysight-b2901



A Python library for running electrochemical experiments from the Keysight B2901A / B2901BL Source-Measure Unit (SMU). Provides a clean interface for common galvanostatic and potentiostatic techniques, with built-in CSV logging, live plotting, and hardware safety limits.



Designed for 4-wire (Kelvin) sensing with the counter/sense terminals connected to a reference electrode, as is typical in 3-electrode electrochemical cells.



\## Installation



```bash

pip install keysight-b2901

```



\## Supported methods



\- `constant\_current` — galvanostatic hold for a fixed duration

\- `constant\_voltage` — potentiostatic hold for a fixed duration

\- `constant\_current\_until\_cutoff` — galvanostatic hold until a voltage limit is reached

\- `constant\_voltage\_until\_cutoff` — potentiostatic hold until a current limit is reached

\- `charge\_discharge\_cycle` — automated galvanostatic charge/discharge cycling (GCD)

\- `cyclic\_voltammetry` — software-stepped CV sweep at a specified scan rate



All methods accept an optional `LivePlotter` for real-time V and I strip-charts, and write to a thread-safe CSV via `DataLogger`.



\---



\## Usage



\### Finding your instrument address



```python

import pyvisa

rm = pyvisa.ResourceManager()

print(rm.list\_resources())

\# e.g. 'USB0::0x2A8D::0x9101::MY63320360::INSTR'

```



\---



\### Constant current hold



```python

from keysight\_echem import KeysightB2901, DataLogger, LivePlotter, SafetyLimits



ADDR = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'



safety  = SafetyLimits(max\_voltage=4.5, min\_voltage=-0.5,

&#x20;                      max\_current=1.0, min\_current=-1.0)

plotter = LivePlotter(title='CC Hold', current\_unit='mA')



with KeysightB2901(ADDR, safety=safety) as smu:

&#x20;   with DataLogger('cc\_hold.csv') as log:

&#x20;       smu.constant\_current(

&#x20;           current=0.5,          # A

&#x20;           duration=120,         # s

&#x20;           sample\_rate=10,       # Hz

&#x20;           voltage\_compliance=5.0,

&#x20;           logger=log,

&#x20;           plotter=plotter,

&#x20;       )



plotter.save('cc\_hold.png')

plotter.keep\_open()

```



\---



\### GITT (Galvanostatic Intermittent Titration Technique)



GITT consists of a series of current pulses each followed by a relaxation period. It can be built directly from `constant\_current` and `constant\_current\_until\_cutoff` in a loop:



```python

from keysight\_echem import KeysightB2901, DataLogger, LivePlotter

import time



ADDR   = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'

PULSES = 20



plotter = LivePlotter(title='GITT', current\_unit='mA')



with KeysightB2901(ADDR) as smu:

&#x20;   with DataLogger('gitt.csv') as log:

&#x20;       for i in range(PULSES):

&#x20;           # --- current pulse ---

&#x20;           smu.constant\_current(

&#x20;               current=0.1,            # A

&#x20;               duration=60,            # s per pulse

&#x20;               sample\_rate=10,

&#x20;               voltage\_compliance=5.0,

&#x20;               logger=log,

&#x20;               plotter=plotter,

&#x20;               status=f'pulse\_{i+1}',

&#x20;           )



&#x20;           # --- relaxation (output already off; just wait and measure at OCV) ---

&#x20;           smu.constant\_current(

&#x20;               current=0.0,            # zero current = open circuit measurement

&#x20;               duration=300,           # s relaxation

&#x20;               sample\_rate=2,

&#x20;               voltage\_compliance=5.0,

&#x20;               logger=log,

&#x20;               plotter=plotter,

&#x20;               status=f'relax\_{i+1}',

&#x20;           )



plotter.save('gitt.png')

plotter.keep\_open()

```



Each pulse/relax pair is labelled in the CSV and plotted in a distinct colour, so the titration steps are immediately visible.



\---



\### Conductance-based MPPT (Maximum Power Point Tracking)



The incremental conductance algorithm tracks the maximum power point of a source (e.g. a photoelectrochemical cell) by comparing the incremental conductance `dI/dV` to the instantaneous conductance `I/V` and stepping the current accordingly:



\- If `dI/dV + I/V > 0` → increase current (moving toward MPP)

\- If `dI/dV + I/V < 0` → decrease current (overshot MPP)

\- If `dI/dV + I/V ≈ 0` → at MPP, hold



```python

from keysight\_echem import KeysightB2901, DataLogger, LivePlotter

import time



ADDR       = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'

I\_MIN      = 0.0    # A — lower current bound

I\_MAX      = 0.5    # A — upper current bound

I\_STEP     = 0.005  # A — perturbation step size

TOLERANCE  = 1e-4   # conductance tolerance for "at MPP"

INTERVAL   = 0.5    # s between MPPT updates

DURATION   = 600    # s total run time



plotter = LivePlotter(title='MPPT', current\_unit='mA')



with KeysightB2901(ADDR) as smu:

&#x20;   with DataLogger('mppt.csv') as log:



&#x20;       current\_setpoint = 0.05   # A — initial guess

&#x20;       v\_prev, i\_prev   = None, None

&#x20;       t\_end = time.time() + DURATION



&#x20;       while time.time() < t\_end:

&#x20;           # Apply current and take a single timed sample

&#x20;           smu.constant\_current(

&#x20;               current=current\_setpoint,

&#x20;               duration=INTERVAL,

&#x20;               sample\_rate=1,

&#x20;               voltage\_compliance=5.0,

&#x20;               logger=log,

&#x20;               plotter=plotter,

&#x20;               status='mppt',

&#x20;           )



&#x20;           # Read the last logged values back from the plotter's buffer

&#x20;           seg = plotter.\_segments.get('mppt')

&#x20;           if seg is None or len(seg\['v']) < 2:

&#x20;               continue



&#x20;           v\_now = seg\['v']\[-1]

&#x20;           i\_now = seg\['i']\[-1] / 1e3   # plotter stores in mA; convert back to A



&#x20;           if v\_prev is not None:

&#x20;               dV = v\_now - v\_prev

&#x20;               dI = i\_now - i\_prev



&#x20;               if abs(dV) > 1e-6:   # avoid division by zero

&#x20;                   inc\_conductance  = dI / dV

&#x20;                   inst\_conductance = i\_now / v\_now if abs(v\_now) > 1e-6 else 0.0

&#x20;                   error = inc\_conductance + inst\_conductance



&#x20;                   if error > TOLERANCE:

&#x20;                       current\_setpoint = min(current\_setpoint + I\_STEP, I\_MAX)

&#x20;                   elif error < -TOLERANCE:

&#x20;                       current\_setpoint = max(current\_setpoint - I\_STEP, I\_MIN)

&#x20;                   # else: within tolerance, hold current



&#x20;           v\_prev, i\_prev = v\_now, i\_now



plotter.save('mppt.png')

plotter.keep\_open()

```



> \*\*Note:\*\* For a real MPPT deployment you would tune `I\_STEP`, `INTERVAL`, and `TOLERANCE` to match the time constants of your cell. Smaller steps give finer tracking at the cost of convergence speed.



\---



\## Safety limits



All methods respect a `SafetyLimits` object that acts as a software-level emergency stop independent of the instrument's own compliance settings:



```python

from keysight\_echem import SafetyLimits



safety = SafetyLimits(

&#x20;   max\_voltage =  4.5,   # V

&#x20;   min\_voltage = -0.5,   # V

&#x20;   max\_current =  1.0,   # A

&#x20;   min\_current = -1.0,   # A

)



smu = KeysightB2901(ADDR, safety=safety)

```



If either boundary is exceeded during any measurement the output is switched off immediately and the loop exits.



\---



\## License



MIT. Not affiliated with or endorsed by Keysight Technologies.


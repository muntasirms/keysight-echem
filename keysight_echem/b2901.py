"""
keysight_echem_b2901.py
=================
Production-ready Python library for electrochemical measurements using the
Keysight B2901A / B2901BL Source-Measure Unit (SMU).

Designed for 4-wire (Kelvin) sensing with the counter-sense terminals
connected to a reference electrode, as is typical in 3-electrode cells.

Quickstart
----------
    smu = KeysightB2901('USB0::0x2A8D::0x9101::MY63320360::INSTR')
    with smu:
        logger  = DataLogger('experiment.csv')
        plotter = LivePlotter()
        smu.constant_current(
            current=0.5, duration=60, sample_rate=10,
            voltage_compliance=5.0, logger=logger, plotter=plotter,
        )
        logger.close()
        plotter.keep_open()   # blocks until the plot window is closed

Instrument addresses
--------------------
    B2901BL (lab):  'USB0::0x2A8D::0x9101::MY63320360::INSTR'
    B2901A  (mine): 'USB0::0x0957::0x8B18::MY51142768::INSTR'

    List all connected VISA resources:
        import pyvisa
        print(pyvisa.ResourceManager().list_resources())
"""

import csv
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Literal, Optional

import pyvisa

# matplotlib is imported lazily inside LivePlotter so that the rest of the
# library works on headless systems where a display is absent.


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Measurement:
    """A single timestamped sample from the SMU."""
    timestamp: float   # seconds since session start
    voltage:   float   # Volts, as measured at sense terminals
    current:   float   # Amperes, as measured
    status:    str     # experiment label (e.g. 'CC', 'charge_c1')


@dataclass
class SafetyLimits:
    """
    Hard software-level limits enforced at every measurement loop iteration.

    If either boundary is exceeded the output is switched off immediately,
    independently of the instrument's own compliance settings.

    Parameters
    ----------
    max_voltage : float   Upper voltage trip point (V).   Default +10 V.
    min_voltage : float   Lower voltage trip point (V).   Default -10 V.
    max_current : float   Upper current trip point (A).   Default  +1 A.
    min_current : float   Lower current trip point (A).   Default  -1 A.
    """
    max_voltage: float =  10.0
    min_voltage: float = -10.0
    max_current: float =   1.0
    min_current: float =  -1.0


# ---------------------------------------------------------------------------
# Thread-safe CSV logger
# ---------------------------------------------------------------------------

class DataLogger:
    """
    Thread-safe, asynchronous CSV data logger.

    A dedicated writer thread drains a queue so measurement loops are never
    blocked by disk I/O and rows are always written in acquisition order.

    Usage
    -----
        with DataLogger('experiment.csv') as log:
            smu.constant_current(..., logger=log)
        # file is flushed and closed on context exit
    """

    HEADER    = ['Time (s)', 'Voltage (V)', 'Current (A)', 'Status']
    _SENTINEL = object()

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def _writer_loop(self):
        with open(self.filepath, mode='w', newline='', buffering=1) as f:
            writer = csv.writer(f)
            writer.writerow(self.HEADER)
            while True:
                row = self._queue.get()
                if row is self._SENTINEL:
                    break
                writer.writerow(row)

    def log(self, m: Measurement):
        """Enqueue a measurement for writing (non-blocking)."""
        self._queue.put([m.timestamp, m.voltage, m.current, m.status])

    def close(self):
        """Flush the queue and wait for all rows to be written."""
        self._queue.put(self._SENTINEL)
        self._thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Live plotter
# ---------------------------------------------------------------------------

# Colour palette — each distinct status label gets its own colour so that
# different experimental phases (e.g. charge vs discharge cycles) are
# immediately distinguishable on both subplots.
_PALETTE = [
    '#2196F3',  # blue
    '#F44336',  # red
    '#4CAF50',  # green
    '#FF9800',  # orange
    '#9C27B0',  # purple
    '#00BCD4',  # cyan
    '#795548',  # brown
    '#607D8B',  # blue-grey
]


class LivePlotter:
    """
    Real-time voltage and current strip-chart updated as data arrive.

    Displays two vertically-stacked subplots with a shared time axis:

      * **Top**    — Voltage (V) vs time (s)
      * **Bottom** — Current (scaled unit) vs time (s)

    Each distinct ``status`` label is drawn as its own line segment with a
    unique colour, so different experimental phases are immediately
    distinguishable.  A legend on the voltage panel identifies each segment.

    The plot is refreshed at most ``refresh_rate`` times per second
    (default 10 Hz) regardless of the acquisition sample rate, so the GUI
    never becomes a bottleneck for the measurement loop.

    Parameters
    ----------
    title : str
        Window / figure title.  Default ``'Live Measurement'``.
    max_points : int, optional
        Rolling window: only the most recent ``max_points`` samples are kept
        in memory (useful for very long experiments).  Default: unlimited.
    refresh_rate : float
        Maximum redraw frequency in Hz.  Default 10.
    figsize : tuple[float, float]
        Matplotlib figure size in inches.  Default ``(10, 6)``.
    current_unit : {'A', 'mA', 'uA'}
        Display unit for the current axis.  Raw Ampere values are scaled
        automatically.  Default ``'mA'``.

    Usage
    -----
        plotter = LivePlotter(title='CC hold', current_unit='mA')

        with KeysightB2901(ADDR) as smu:
            with DataLogger('out.csv') as log:
                smu.constant_current(..., logger=log, plotter=plotter)

        plotter.save('result.png')   # optional snapshot
        plotter.keep_open()          # blocks until window is closed

    Notes
    -----
    matplotlib's GUI must run on the **main thread** (OS-level restriction).
    Because all SMU methods block the calling thread, passing ``plotter``
    into each method and letting ``_run_loop`` call ``.update()`` inline
    keeps everything on the main thread — no background thread is needed.
    """

    _UNIT_SCALE = {'A': 1.0, 'mA': 1e3, 'uA': 1e6}

    def __init__(
        self,
        title:        str            = 'Live Measurement',
        max_points:   Optional[int]  = None,
        refresh_rate: float          = 10.0,
        figsize:      tuple          = (10, 6),
        current_unit: str            = 'mA',
    ):
        if current_unit not in self._UNIT_SCALE:
            raise ValueError(
                f"current_unit must be one of {list(self._UNIT_SCALE)}, "
                f"got {current_unit!r}"
            )

        self.title        = title
        self.max_points   = max_points
        self.refresh_rate = refresh_rate
        self.figsize      = figsize
        self.current_unit = current_unit
        self._i_scale     = self._UNIT_SCALE[current_unit]

        # Per-segment buffers  {status: {'t': deque, 'v': deque, 'i': deque}}
        self._segments:   dict = {}
        self._colour_map: dict = {}
        self._colour_idx: int  = 0

        # Line artist handles  {status: Line2D}
        self._lines_v: dict = {}
        self._lines_i: dict = {}

        self._last_refresh: float = 0.0
        self._min_interval: float = 1.0 / refresh_rate

        self._fig  = None
        self._ax_v = None
        self._ax_i = None
        self._plt  = None
        self._ready = False

        self._init_figure()

    # -- setup ----------------------------------------------------------------

    def _init_figure(self):
        import matplotlib.pyplot as plt
        self._plt = plt

        plt.ion()  # non-blocking interactive mode

        fig, (ax_v, ax_i) = plt.subplots(
            2, 1,
            sharex=True,
            figsize=self.figsize,
            gridspec_kw={'hspace': 0.08},
        )
        self._fig, self._ax_v, self._ax_i = fig, ax_v, ax_i

        fig.suptitle(self.title, fontsize=13, fontweight='bold')

        ax_v.set_ylabel('Voltage (V)', fontsize=11)
        ax_v.grid(True, linestyle='--', alpha=0.45)
        ax_v.tick_params(labelbottom=False)

        ax_i.set_ylabel(f'Current ({self.current_unit})', fontsize=11)
        ax_i.set_xlabel('Time (s)', fontsize=11)
        ax_i.grid(True, linestyle='--', alpha=0.45)

        fig.tight_layout()
        fig.canvas.draw()
        plt.pause(0.001)
        self._ready = True

    # -- colour allocation ----------------------------------------------------

    def _colour_for(self, status: str) -> str:
        if status not in self._colour_map:
            self._colour_map[status] = _PALETTE[self._colour_idx % len(_PALETTE)]
            self._colour_idx += 1
        return self._colour_map[status]

    # -- segment management ---------------------------------------------------

    def _ensure_segment(self, status: str):
        """Create data buffers and Line2D artists for a new status label."""
        if status in self._segments:
            return
        colour = self._colour_for(status)
        kw = dict(label=status, color=colour, linewidth=1.6)
        ml = self.max_points
        self._segments[status] = {
            't': deque(maxlen=ml),
            'v': deque(maxlen=ml),
            'i': deque(maxlen=ml),
        }
        self._lines_v[status], = self._ax_v.plot([], [], **kw)
        self._lines_i[status], = self._ax_i.plot([], [], **kw)
        # Rebuild legend on the voltage panel (keeps the current panel clean)
        self._ax_v.legend(
            loc='upper left',
            fontsize=8,
            framealpha=0.6,
            ncol=max(1, len(self._segments) // 6),
        )

    # -- public interface -----------------------------------------------------

    def update(self, m: Measurement):
        """
        Ingest one measurement and redraw if the refresh interval has elapsed.

        Called automatically by ``_run_loop``; you do not need to call this
        manually unless you are driving the plotter outside an SMU method.
        """
        if not self._ready:
            return

        self._ensure_segment(m.status)
        seg = self._segments[m.status]
        seg['t'].append(m.timestamp)
        seg['v'].append(m.voltage)
        seg['i'].append(m.current * self._i_scale)

        now = time.time()
        if (now - self._last_refresh) >= self._min_interval:
            self._redraw()
            self._last_refresh = now

    def _redraw(self):
        for status, seg in self._segments.items():
            if not seg['t']:
                continue
            t = list(seg['t'])
            self._lines_v[status].set_data(t, list(seg['v']))
            self._lines_i[status].set_data(t, list(seg['i']))

        for ax in (self._ax_v, self._ax_i):
            ax.relim()
            ax.autoscale_view()

        self._fig.canvas.draw_idle()
        self._plt.pause(0.001)   # yield to the GUI event loop

    def save(self, filepath: str, dpi: int = 150):
        """
        Save the current figure to disk.

        Parameters
        ----------
        filepath : str
            Output path, e.g. ``'result.png'`` or ``'result.pdf'``.
        dpi : int
            Resolution for raster formats.  Default 150.
        """
        if self._fig is not None:
            self._fig.savefig(filepath, dpi=dpi, bbox_inches='tight')
            print(f'[Plotter] Figure saved → {filepath}')

    def keep_open(self):
        """
        Block until the plot window is manually closed by the user.

        Call this after all SMU methods have returned so that the figure
        stays interactive rather than disappearing when the script exits.
        """
        if not self._ready:
            return
        self._plt.ioff()
        print('[Plotter] Experiment finished — close the plot window to exit.')
        self._plt.show()

    def close(self):
        """Close the figure programmatically."""
        if self._ready and self._fig is not None:
            self._plt.close(self._fig)
        self._ready = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Main instrument class
# ---------------------------------------------------------------------------

class KeysightB2901:
    """
    High-level driver for the Keysight B2901A / B2901BL SMU.

    All electrochemical methods share a common measurement loop
    (``_run_loop``) that handles timing, safety cutoffs, CSV logging, and
    optional live plotting.

    Parameters
    ----------
    address : str
        VISA resource string (USB, GPIB, or TCP/IP).
    four_wire : bool
        Enable 4-wire Kelvin remote sensing.  Default True.
    safety : SafetyLimits, optional
        Software-level trip points.  Defaults to ±10 V / ±1 A.
    visa_timeout_ms : int
        VISA query timeout in milliseconds.  Default 5000.

    Context manager
    ---------------
        with KeysightB2901('USB0::...') as smu:
            ...  # auto connect / disconnect

    Measurement accuracy note
    -------------------------
    The original scripts read ``:SOUR:VOLT?`` / ``:SOUR:CURR?`` which return
    the *programmed set-point*, not a real terminal measurement.  This library
    uses ``:MEAS?`` which triggers an actual ADC conversion and returns both V
    and I in a single VISA round-trip, halving latency versus two separate
    measurement queries.
    """

    _MIN_INTERVAL_S: float = 1e-3   # ~1 kHz USB-VISA ceiling

    def __init__(
        self,
        address:         str,
        four_wire:       bool                  = True,
        safety:          Optional[SafetyLimits] = None,
        visa_timeout_ms: int                   = 5000,
    ):
        self.address         = address
        self.four_wire       = four_wire
        self.safety          = safety or SafetyLimits()
        self.visa_timeout_ms = visa_timeout_ms

        self._rm:        Optional[pyvisa.ResourceManager] = None
        self._inst       = None
        self._connected: bool  = False
        self._t0:        float = 0.0

    # -- connection -----------------------------------------------------------

    def connect(self) -> str:
        """Open VISA session; return the ``*IDN?`` string."""
        self._rm   = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.address)
        self._inst.timeout = self.visa_timeout_ms
        idn = self._inst.query('*IDN?').strip()
        self._connected = True
        self._t0 = time.time()
        print(f'[SMU] Connected → {idn}')
        return idn

    def disconnect(self):
        """Switch output off and close the VISA session."""
        if self._inst:
            try:
                self._inst.write(':OUTP OFF')
            except Exception:
                pass
            self._inst.close()
        if self._rm:
            self._rm.close()
        self._connected = False
        print('[SMU] Disconnected.')

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # -- SCPI helpers ---------------------------------------------------------

    def _assert_connected(self):
        if not self._connected:
            raise RuntimeError(
                'Not connected — call .connect() or use a `with` block.'
            )

    def _elapsed(self) -> float:
        """Seconds since session start (monotonic across method calls)."""
        return time.time() - self._t0

    def _configure_sense(self):
        self._inst.write(':SENS:REM ' + ('ON' if self.four_wire else 'OFF'))

    def _setup_current_source(self, current: float, voltage_compliance: float):
        self._inst.write(':SOUR:FUNC:MODE CURR')
        self._inst.write(f':SOUR:CURR:LEV {current:.6g}')
        self._inst.write(f':SENS:VOLT:PROT {abs(voltage_compliance):.6g}')
        self._configure_sense()

    def _setup_voltage_source(self, voltage: float, current_compliance: float):
        self._inst.write(':SOUR:FUNC:MODE VOLT')
        self._inst.write(f':SOUR:VOLT:LEV {voltage:.6g}')
        self._inst.write(f':SENS:CURR:PROT {abs(current_compliance):.6g}')
        self._configure_sense()

    def _measure(self) -> tuple:
        """
        Trigger one ADC conversion; return ``(voltage_V, current_A)``.

        ``:MEAS?`` returns V, I, R, timestamp, status — we take the first two.
        """
        # parts = self._inst.query(':MEAS?').strip().split(',')
        # return float(parts[0]), float(parts[1])

        voltage = float(self._inst.query(':MEAS:VOLT?').strip())
        current = float(self._inst.query(':MEAS:CURR?').strip())
        return voltage, current

    def _output_on(self):
        self._inst.write(':OUTP ON')

    def _output_off(self):
        self._inst.write(':OUTP OFF')

    def _within_safety(self, voltage: float, current: float) -> bool:
        s = self.safety
        return (
            s.min_voltage <= voltage <= s.max_voltage and
            s.min_current <= current <= s.max_current
        )

    # -- core loop ------------------------------------------------------------

    def _run_loop(
        self,
        logger:      DataLogger,
        status:      str,
        sample_rate: float,
        *,
        plotter:             Optional[LivePlotter] = None,
        duration:            Optional[float]       = None,
        voltage_cutoff_high: Optional[float]       = None,
        voltage_cutoff_low:  Optional[float]       = None,
        current_cutoff_high: Optional[float]       = None,
        current_cutoff_low:  Optional[float]       = None,
    ) -> Optional[Measurement]:
        """
        Shared timing and acquisition core used by all public methods.

        Exits on the first condition met:

        * ``duration`` seconds elapsed              (if provided)
        * voltage >= ``voltage_cutoff_high``        (if provided)
        * voltage <= ``voltage_cutoff_low``         (if provided)
        * current >= ``current_cutoff_high``        (if provided)
        * current <= ``current_cutoff_low``         (if provided)
        * safety limit breached                    (always active)

        Each sample is passed to ``logger`` (CSV) and, when supplied,
        ``plotter`` (live strip-chart) before the sleep remainder of the
        interval.  Returns the last ``Measurement`` recorded.
        """
        interval   = 1.0 / sample_rate
        loop_start = time.time()
        last: Optional[Measurement] = None

        while True:
            tick = time.time()

            # -- measure ------------------------------------------------------
            try:
                voltage, current = self._measure()
            except pyvisa.errors.VisaIOError as exc:
                print(f'[VISA error] {exc}')
                time.sleep(interval)
                continue

            last = Measurement(
                timestamp = self._elapsed(),
                voltage   = voltage,
                current   = current,
                status    = status,
            )

            logger.log(last)

            if plotter is not None:
                plotter.update(last)

            print(
                f'  t={last.timestamp:8.3f}s  '
                f'V={voltage:+.6f} V  '
                f'I={current:+.4e} A  '
                f'[{status}]'
            )

            # -- safety -------------------------------------------------------
            if not self._within_safety(voltage, current):
                print(
                    f'[SAFETY] Trip  V={voltage:.4f} V  '
                    f'I={current:.4e} A — output OFF.'
                )
                self._output_off()
                break

            # -- cutoffs ------------------------------------------------------
            if voltage_cutoff_high is not None and voltage >= voltage_cutoff_high:
                print(f'[CUTOFF] V={voltage:.4f} >= {voltage_cutoff_high:.4f} V')
                break
            if voltage_cutoff_low is not None and voltage <= voltage_cutoff_low:
                print(f'[CUTOFF] V={voltage:.4f} <= {voltage_cutoff_low:.4f} V')
                break
            if current_cutoff_high is not None and current >= current_cutoff_high:
                print(f'[CUTOFF] I={current:.4e} >= {current_cutoff_high:.4e} A')
                break
            if current_cutoff_low is not None and current <= current_cutoff_low:
                print(f'[CUTOFF] I={current:.4e} <= {current_cutoff_low:.4e} A')
                break

            # -- duration -----------------------------------------------------
            if duration is not None and (time.time() - loop_start) >= duration:
                break

            # -- pace ---------------------------------------------------------
            remaining = interval - (time.time() - tick)
            if remaining > 0:
                time.sleep(remaining)

        return last

    # =========================================================================
    # Public electrochemical methods
    # =========================================================================

    def constant_current(
        self,
        current:            float,
        duration:           float,
        sample_rate:        float,
        voltage_compliance: float,
        logger:             DataLogger,
        plotter:            Optional[LivePlotter] = None,
        status:             str                   = 'CC',
    ):
        """
        Apply a constant current for a fixed duration.

        Parameters
        ----------
        current : float
            Source current in Amperes (signed; positive = anodic).
        duration : float
            Experiment duration in seconds.
        sample_rate : float
            Target measurement frequency in Hz.
        voltage_compliance : float
            Voltage compliance limit in Volts (instrument protection).
        logger : DataLogger
        plotter : LivePlotter, optional
            Pass an open plotter for live V/I strip-charts.
        status : str
            CSV Status label.  Default ``'CC'``.
        """
        self._assert_connected()
        self._setup_current_source(current, voltage_compliance)
        self._output_on()
        print(f'\n[CC] I={current:+.4g} A  duration={duration} s  SR={sample_rate} Hz')
        self._run_loop(logger, status, sample_rate, plotter=plotter, duration=duration)
        self._output_off()
        print('[CC] Done.')

    # -------------------------------------------------------------------------

    def constant_voltage(
        self,
        voltage:            float,
        duration:           float,
        sample_rate:        float,
        current_compliance: float,
        logger:             DataLogger,
        plotter:            Optional[LivePlotter] = None,
        status:             str                   = 'CV',
    ):
        """
        Apply a constant voltage for a fixed duration.

        Parameters
        ----------
        voltage : float
            Source voltage in Volts.
        duration : float
            Experiment duration in seconds.
        sample_rate : float
            Target measurement frequency in Hz.
        current_compliance : float
            Current compliance limit in Amperes.
        logger : DataLogger
        plotter : LivePlotter, optional
        status : str
            CSV Status label.  Default ``'CV'``.
        """
        self._assert_connected()
        self._setup_voltage_source(voltage, current_compliance)
        self._output_on()
        print(f'\n[CV] V={voltage:+.4g} V  duration={duration} s  SR={sample_rate} Hz')
        self._run_loop(logger, status, sample_rate, plotter=plotter, duration=duration)
        self._output_off()
        print('[CV] Done.')

    # -------------------------------------------------------------------------

    def constant_current_until_cutoff(
        self,
        current:             float,
        voltage_cutoff_high: float,
        voltage_cutoff_low:  float,
        sample_rate:         float,
        voltage_compliance:  float,
        logger:              DataLogger,
        plotter:             Optional[LivePlotter] = None,
        status:              str                   = 'CC',
        max_duration:        Optional[float]       = None,
    ):
        """
        Apply a constant current until a voltage boundary is reached.

        The loop exits as soon as the terminal voltage leaves the window
        [``voltage_cutoff_low``, ``voltage_cutoff_high``], or when
        ``max_duration`` seconds have elapsed.

        Parameters
        ----------
        current : float
            Source current in Amperes (signed).
        voltage_cutoff_high : float
            Upper voltage trip point (V) — typically end-of-charge voltage.
        voltage_cutoff_low : float
            Lower voltage trip point (V) — typically end-of-discharge voltage.
        sample_rate : float
        voltage_compliance : float
        logger : DataLogger
        plotter : LivePlotter, optional
        status : str
            CSV Status label.  Default ``'CC'``.
        max_duration : float, optional
            Hard time-limit failsafe in seconds.
        """
        self._assert_connected()
        self._setup_current_source(current, voltage_compliance)
        self._output_on()
        print(
            f'\n[CC->cutoff] I={current:+.4g} A  '
            f'Vhi={voltage_cutoff_high} V  Vlo={voltage_cutoff_low} V'
            + (f'  max={max_duration} s' if max_duration else '')
        )
        self._run_loop(
            logger, status, sample_rate,
            plotter=plotter,
            duration=max_duration,
            voltage_cutoff_high=voltage_cutoff_high,
            voltage_cutoff_low=voltage_cutoff_low,
        )
        self._output_off()
        print('[CC->cutoff] Done.')

    # -------------------------------------------------------------------------

    def constant_voltage_until_cutoff(
        self,
        voltage:             float,
        current_cutoff_high: float,
        current_cutoff_low:  float,
        sample_rate:         float,
        current_compliance:  float,
        logger:              DataLogger,
        plotter:             Optional[LivePlotter] = None,
        status:              str                   = 'CV',
        max_duration:        Optional[float]       = None,
    ):
        """
        Apply a constant voltage until a current boundary is reached.

        Useful for potentiostatic holds terminated on current decay
        (e.g. CC-CV charging termination).

        Parameters
        ----------
        voltage : float
            Source voltage in Volts.
        current_cutoff_high : float
            Upper current trip point (A).
        current_cutoff_low : float
            Lower current trip point (A).
        sample_rate : float
        current_compliance : float
        logger : DataLogger
        plotter : LivePlotter, optional
        status : str
            CSV Status label.  Default ``'CV'``.
        max_duration : float, optional
            Hard time-limit failsafe in seconds.
        """
        self._assert_connected()
        self._setup_voltage_source(voltage, current_compliance)
        self._output_on()
        print(
            f'\n[CV->cutoff] V={voltage:+.4g} V  '
            f'Ihi={current_cutoff_high} A  Ilo={current_cutoff_low} A'
            + (f'  max={max_duration} s' if max_duration else '')
        )
        self._run_loop(
            logger, status, sample_rate,
            plotter=plotter,
            duration=max_duration,
            current_cutoff_high=current_cutoff_high,
            current_cutoff_low=current_cutoff_low,
        )
        self._output_off()
        print('[CV->cutoff] Done.')

    # -------------------------------------------------------------------------

    def charge_discharge_cycle(
        self,
        current:                  float,
        charge_cutoff_voltage:    float,
        discharge_cutoff_voltage: float,
        num_cycles:               int,
        sample_rate:              float,
        voltage_compliance:       float,
        logger:                   DataLogger,
        plotter:                  Optional[LivePlotter]           = None,
        initial_direction:        Literal['positive', 'negative'] = 'positive',
        rest_duration:            float                           = 0.0,
        max_half_cycle_duration:  Optional[float]                 = None,
    ):
        """
        Galvanostatic charge/discharge cycling (GCD).

        Each full cycle consists of two half-cycles:

        1. **Charge** — current flows until ``charge_cutoff_voltage`` is hit.
        2. **Discharge** — reversed current until ``discharge_cutoff_voltage``.

        The Status column in the CSV encodes cycle number and phase, e.g.
        ``'charge_c3'`` / ``'discharge_c3'``, so every half-cycle appears as
        its own colour in the live plotter.

        Parameters
        ----------
        current : float
            Magnitude of source current in Amperes (always pass positive).
        charge_cutoff_voltage : float
            Upper voltage limit ending the charge half-cycle (V).
        discharge_cutoff_voltage : float
            Lower voltage limit ending the discharge half-cycle (V).
        num_cycles : int
        sample_rate : float
        voltage_compliance : float
        logger : DataLogger
        plotter : LivePlotter, optional
        initial_direction : {'positive', 'negative'}
            Polarity of the first half-cycle.  Default ``'positive'`` (charge).
        rest_duration : float
            Seconds to pause (output OFF) between half-cycles.  Default 0.
        max_half_cycle_duration : float, optional
            Hard per-half-cycle time-limit failsafe.
        """
        self._assert_connected()

        abs_current = abs(current)
        sign        = 1 if initial_direction == 'positive' else -1

        print(
            f'\n[GCD] {num_cycles} cycle(s)  |I|={abs_current:.4g} A  '
            f'Vhi={charge_cutoff_voltage} V  Vlo={discharge_cutoff_voltage} V  '
            f'initial_dir={initial_direction}'
        )

        for cycle_idx in range(num_cycles):
            cycle_num = cycle_idx + 1
            for half_idx in range(2):
                direction       = sign * ((-1) ** half_idx)
                applied_current = direction * abs_current

                if direction > 0:
                    label = f'charge_c{cycle_num}'
                    v_hi, v_lo = charge_cutoff_voltage, None
                else:
                    label = f'discharge_c{cycle_num}'
                    v_hi, v_lo = None, discharge_cutoff_voltage

                print(
                    f'\n  -- Cycle {cycle_num}/{num_cycles}  {label}  '
                    f'I={applied_current:+.4g} A --'
                )
                self._setup_current_source(applied_current, voltage_compliance)
                self._output_on()
                self._run_loop(
                    logger, label, sample_rate,
                    plotter=plotter,
                    duration=max_half_cycle_duration,
                    voltage_cutoff_high=v_hi,
                    voltage_cutoff_low=v_lo,
                )
                self._output_off()

                is_last = (cycle_idx == num_cycles - 1 and half_idx == 1)
                if rest_duration > 0 and not is_last:
                    print(f'  [rest] {rest_duration} s ...')
                    time.sleep(rest_duration)

        print(f'\n[GCD] All {num_cycles} cycle(s) complete.')

    # =========================================================================
    # Stretch goal: software-stepped cyclic voltammetry
    # =========================================================================

    def cyclic_voltammetry(
        self,
        v_start:            float,
        v_high:             float,
        v_low:              float,
        scan_rate:          float,
        current_compliance: float,
        logger:             DataLogger,
        plotter:            Optional[LivePlotter] = None,
        num_cycles:         int                   = 1,
        status_prefix:      str                   = 'CV',
    ):
        """
        Software-stepped cyclic voltammetry (CV sweep).

        The voltage setpoint is updated at the minimum reliable VISA interval
        (~1 ms).  The step size is derived from the *actual* elapsed time
        since the last update so that scan-rate accuracy is preserved even
        under scheduling jitter:

            delta_v = scan_rate [V/s] x delta_t_actual [s]

        Sweep sequence (per cycle)
        --------------------------
        v_start -> v_high (anodic) -> v_low (cathodic) -> [repeat] -> v_start

        Parameters
        ----------
        v_start : float
            Initial and final potential (V).
        v_high : float
            Upper vertex potential (V).
        v_low : float
            Lower vertex potential (V).
        scan_rate : float
            Desired scan rate in V/s.
        current_compliance : float
            Current compliance limit in Amperes.
        logger : DataLogger
        plotter : LivePlotter, optional
            Each sweep segment (anodic/cathodic per cycle) gets its own colour.
        num_cycles : int
            Number of complete (v_high -> v_low) cycles.  Default 1.
        status_prefix : str
            Prefix for Status labels; segment names are appended
            (e.g. ``'CV_anodic_c1'``, ``'CV_cathodic_c1'``).

        Notes
        -----
        Timing fidelity is bounded by USB-VISA round-trip latency (~1-5 ms).
        For scan rates <= 0.1 V/s this is negligible.  Above ~1 V/s consider
        the instrument's internal sweep source (``SOUR:VOLT:MODE SWE``) which
        requires a separate implementation using the trigger system.
        """
        self._assert_connected()

        segments: list = []
        for i in range(1, num_cycles + 1):
            segments.append((v_high, f'{status_prefix}_anodic_c{i}'))
            segments.append((v_low,  f'{status_prefix}_cathodic_c{i}'))
        segments.append((v_start, f'{status_prefix}_return'))

        self._inst.write(':SOUR:FUNC:MODE VOLT')
        self._inst.write(f':SOUR:VOLT:LEV {v_start:.6g}')
        self._inst.write(f':SENS:CURR:PROT {abs(current_compliance):.6g}')
        self._configure_sense()
        self._output_on()

        print(
            f'\n[CV sweep] SR={scan_rate} V/s  '
            f'v_low={v_low} V  v_high={v_high} V  '
            f'{num_cycles} cycle(s)'
        )

        current_setpoint = v_start

        for target, label in segments:
            if abs(target - current_setpoint) < 1e-9:
                continue

            direction = 1 if target > current_setpoint else -1
            print(f'\n  -> {label}  ({current_setpoint:+.4f} V -> {target:+.4f} V)')

            while True:
                tick = time.time()

                self._inst.write(f':SOUR:VOLT:LEV {current_setpoint:.6g}')

                try:
                    voltage, current = self._measure()
                except pyvisa.errors.VisaIOError as exc:
                    print(f'[VISA error] {exc}')
                    time.sleep(self._MIN_INTERVAL_S)
                    continue

                m = Measurement(
                    timestamp = self._elapsed(),
                    voltage   = voltage,
                    current   = current,
                    status    = label,
                )
                logger.log(m)

                if plotter is not None:
                    plotter.update(m)

                print(
                    f'  t={m.timestamp:8.3f}s  '
                    f'Vset={current_setpoint:+.5f}  '
                    f'Vmeas={voltage:+.5f} V  '
                    f'I={current:+.4e} A'
                )

                if not self._within_safety(voltage, current):
                    print(
                        f'[SAFETY] Trip  V={voltage:.4f} V  '
                        f'I={current:.4e} A — output OFF.'
                    )
                    self._output_off()
                    return

                elapsed = time.time() - tick
                step    = scan_rate * max(elapsed, self._MIN_INTERVAL_S)
                current_setpoint += direction * step

                if direction > 0:
                    current_setpoint = min(current_setpoint, target)
                    if current_setpoint >= target:
                        break
                else:
                    current_setpoint = max(current_setpoint, target)
                    if current_setpoint <= target:
                        break

                remaining = self._MIN_INTERVAL_S - (time.time() - tick)
                if remaining > 0:
                    time.sleep(remaining)

        self._output_off()
        print('\n[CV sweep] Complete.')


# ---------------------------------------------------------------------------
# Usage examples (run this file directly for a quick reference)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    # Example 1 — constant current with live plot
    # -------------------------------------------
    ADDR = 'USB0::0x2A8D::0x9101::MY63320360::INSTR'

    plotter = LivePlotter(title='CC Hold', current_unit='mA')
    with KeysightB2901(ADDR) as smu:
        with DataLogger('cc_hold.csv') as log:
            smu.constant_current(
                current=0.1, duration=60, sample_rate=10,
                voltage_compliance=5.0, logger=log, plotter=plotter,
            )
            # smu.constant_voltage(4,40,10,.1,log,plotter=plotter)
    # plotter.save('cc_hold.png')
    # plotter.keep_open()

    # Example 2 — GCD cycling with live plot
    # ---------------------------------------
    # plotter = LivePlotter(title='GCD Cycling', current_unit='mA')
    # with KeysightB2901(ADDR) as smu:
    #     with DataLogger('gcd.csv') as log:
    #         smu.charge_discharge_cycle(
    #             current=0.001,
    #             charge_cutoff_voltage=10.2,
    #             discharge_cutoff_voltage=0.0,
    #             num_cycles=10,
    #             sample_rate=5,
    #             voltage_compliance=5.0,
    #             logger=log, plotter=plotter,
    #             rest_duration=5.0,
    #         )
    # plotter.save('gcd.png')
    # plotter.keep_open()

    # Example 3 — cyclic voltammetry with live plot
    # -----------------------------------------------
    # plotter = LivePlotter(title='CV Sweep', current_unit='uA')
    # with KeysightB2901(ADDR) as smu:
    #     with DataLogger('cv_sweep.csv') as log:
    #         smu.cyclic_voltammetry(
    #             v_start=0.0, v_high=0.8, v_low=-0.2,
    #             scan_rate=0.05,
    #             current_compliance=0.01,
    #             logger=log, plotter=plotter,
    #             num_cycles=3,
    #         )
    # plotter.save('cv_sweep.png')
    # plotter.keep_open()

    print(__doc__)
    sys.exit(0)
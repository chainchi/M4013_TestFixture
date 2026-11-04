"""GPIO (libgpiod) and hardware PWM (sysfs). Outputs start low, PWM starts off."""

import math
import threading
from dataclasses import dataclass, asdict

import gpiod
from gpiod.line import Bias, Direction, Value

from . import config

OUT_COUNT = len(config.GPIO_OUT)
IN_COUNT = len(config.GPIO_IN)
PWM_COUNT = len(config.PWM_CHANNELS)


@dataclass
class PwmState:
    enable: bool = False
    freq: int = config.PWM_DEFAULT_FREQ_HZ   # frequency actually programmed
    duty: float = 0.0
    res_bits: int = 0                        # duty resolution available at this frequency


_lock = threading.Lock()
_req = None
_pwm = [PwmState() for _ in range(PWM_COUNT)]


def _res_bits(freq):
    return max(0, int(math.floor(math.log2(config.PWM_CLK_HZ / freq))))


def _pwm_path(ch, name):
    return f"{config.PWM_CHIP}/pwm{config.PWM_CHANNELS[ch]}/{name}"


def _pwm_write(ch, name, value):
    with open(_pwm_path(ch, name), "w") as f:
        f.write(str(value))


def _pwm_apply(ch):
    """Programs period and duty in ns. Disabled = duty 0, so the pin sits low
    (the RP1 output idles low when the channel is also disabled)."""
    p = _pwm[ch]
    period = round(1e9 / p.freq)
    duty_ns = round(period * p.duty / 100) if p.enable else 0
    _pwm_write(ch, "duty_cycle", 0)          # duty must never exceed the period
    _pwm_write(ch, "period", period)
    _pwm_write(ch, "duty_cycle", duty_ns)
    _pwm_write(ch, "enable", 1 if p.enable else 0)


def init():
    global _req
    settings = {
        config.GPIO_OUT: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
        config.GPIO_IN: gpiod.LineSettings(
            direction=Direction.INPUT,
            bias=Bias.PULL_DOWN if config.GPIO_IN_PULL_DOWN else Bias.DISABLED),
    }
    _req = gpiod.request_lines(config.GPIO_CHIP, consumer="pi-tester", config=settings)

    for ch, n in enumerate(config.PWM_CHANNELS):
        try:
            with open(f"{config.PWM_CHIP}/export", "w") as f:
                f.write(str(n))
        except OSError:
            pass                             # already exported
        _pwm[ch].res_bits = _res_bits(_pwm[ch].freq)
        _pwm_apply(ch)
    print(f"io: gpio out {config.GPIO_OUT} in {config.GPIO_IN} pwm gpio {config.PWM_PINS}", flush=True)


def gpio_out_set(ch, level):
    if not 0 <= ch < OUT_COUNT:
        raise ValueError("ch must be 0-1")
    with _lock:
        _req.set_value(config.GPIO_OUT[ch], Value.ACTIVE if level else Value.INACTIVE)


def gpio_out_get(ch):
    return 1 if _req.get_value(config.GPIO_OUT[ch]) == Value.ACTIVE else 0


def gpio_in_get(ch):
    return 1 if _req.get_value(config.GPIO_IN[ch]) == Value.ACTIVE else 0


def pwm_set(ch, enable, freq, duty):
    if not 0 <= ch < PWM_COUNT or not 0 <= duty <= 100:
        raise ValueError("bad channel or duty")
    freq = int(freq)
    if not config.PWM_MIN_HZ <= freq <= config.PWM_MAX_HZ:
        raise ValueError(f"freq must be {config.PWM_MIN_HZ}-{config.PWM_MAX_HZ} Hz")
    with _lock:
        p = _pwm[ch]
        p.enable, p.freq, p.duty = bool(enable), freq, float(duty)
        p.res_bits = _res_bits(freq)
        _pwm_apply(ch)


def pwm_get(ch):
    with _lock:
        return asdict(_pwm[ch])

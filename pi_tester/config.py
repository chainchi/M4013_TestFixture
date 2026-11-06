"""Pin map and limits. Raspberry Pi 5 equivalent of the P4's Kconfig.projbuild.

GPIO numbers are BCM / RP1 numbers (gpiochip0), not header pin numbers;
the header pin is given in the comment.
"""

GPIO_CHIP = "/dev/gpiochip0"           # RP1, the 40-pin header

GPIO_OUT = (17, 27)                    # OUT0 header 11, OUT1 header 13
GPIO_IN = (22, 23)                     # IN0 header 15, IN1 header 16
# Inputs get the RP1 pull-down, so an unconnected input reads 0 instead of floating
GPIO_IN_PULL_DOWN = True

# Hardware PWM, RP1 pwm0: dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2
PWM_CHIP = "/sys/class/pwm/pwmchip0"
PWM_CHANNELS = (2, 3)                  # PWM0 = GPIO 18 header 12, PWM1 = GPIO 19 header 35
PWM_PINS = (18, 19)
PWM_CLK_HZ = 50_000_000                # clk_pwm0, sets the duty resolution
PWM_DEFAULT_FREQ_HZ = 25_000
PWM_MIN_HZ, PWM_MAX_HZ = 1, 1_000_000

# I2C bus index (API "bus") -> Linux adapter and its SDA/SCL GPIOs
I2C_BUSES = (
    {"dev": 1, "sda": 2, "scl": 3},    # bus 0: dtparam=i2c_arm=on, header 3/5, 1.8k pull-ups on board
    {"dev": 2, "sda": 4, "scl": 5},    # bus 1: dtoverlay=i2c2-pi5,pins_4_5, header 7/29, no pull-ups
)
I2C_DEFAULT_HZ = 100_000
I2C_PROBE_CONFIRM = 3                  # an address must answer this many times in a row

# DUT UARTs. Empty = auto: USB-serial adapters in /dev/serial/by-id, ordered by
# adapter then interface, so a CP2108 gives ports 0-3; adapters sharing a serial
# number are added after them by USB socket (by-path). List device paths to pin
# ports instead, e.g. ("/dev/ttyAMA2", "/dev/ttyUSB0").
UART_DEVICES = ()
UART_PORTS = 4
UART_DEFAULT_BAUD = 115_200
UART_RING_SIZE = 256 * 1024
UART_RESCAN_S = 1.0

# USB webcam (UVC, MJPEG). Empty = first one in /dev/v4l/by-id; or pin a path.
CAMERA_DEVICE = ""
CAMERA_SIZE = (1280, 720)              # the driver picks the nearest size it has
CAMERA_FPS = 30
CAMERA_JPEG_QUALITY = 80               # YUYV-only cameras are JPEG-encoded on the Pi
CAMERA_IDLE_S = 5                      # stop capturing this long after the last viewer
CAMERA_MAX_VIEWERS = 4

# Agent shell tool runs commands as this login user (it has passwordless sudo)
AGENT_SHELL_USER = "m4013pi"

REDFISH_TIMEOUT_S = 15
REDFISH_MAX_BODY = 256 * 1024

HTTP_PORT = 80
STATE_DIR = "/var/lib/pi-tester"
ETH_IF = "eth0"
WIFI_IF = "wlan0"
WIFI_COUNTRY = "TW"
NETPLAN_FILE = "/etc/netplan/60-pi-tester.yaml"

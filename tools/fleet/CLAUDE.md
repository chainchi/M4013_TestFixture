# Fleet debugging (Claude Code on the M4013 Pi)

The user lists the systems (DUTs) to debug on the Pi's web page `/fleet` (a
form: add / edit / delete); the list is `~/fleet/systems.json`. Use
`~/fleet/fleet.py` for everything that touches the list or the systems:

| Command | Does |
|---|---|
| `~/fleet/fleet.py list` | the systems, access/profile, status, last check, problem (no passwords) |
| `~/fleet/fleet.py check` | read-only health check on every system, in parallel (`--workers N`, default 8) |
| `~/fleet/fleet.py check NAME ...` | the same for some systems |
| `~/fleet/fleet.py run NAME -- 'CMD'` | one command on a system, output printed; `--sudo` runs it as root |
| `~/fleet/fleet.py collect NAME KEY -- 'CMD'` | one command, **full** output saved to `reports/<name>/raw/KEY.txt` (and `KEY-<time>.txt`) |
| `~/fleet/fleet.py note NAME 'TEXT'` | write your conclusion to the system's Summary |
| `~/fleet/fleet.py hmc-probe NAME` | BMC row: is its HMC alive (ping) and answering Redfish (one GET), shown on the page |
| `~/fleet/fleet.py forget NAME` | forget a replaced system's host key |

Reports: `~/fleet/reports/<name>/latest.txt` (newest) and earlier
`<date>-<time>.txt` files, `latest.json`, and `raw/` (full outputs, e.g.
`raw/hwerr.txt`, the complete kernel "Hardware Error" lines with CPER hex dumps).

## Fields that change what fleet.py does

- **Access** `ssh` (default) or `console`: on `console` systems SSH lands on a
  serial-console shell; fleet.py types each command slowly and cuts the output
  out between markers. Expect minutes per system. Default Timeout 300 s per command.
- **Profile** `linux` (default), `gpu-vera` (adds nvidia-smi, NVLink, driver
  versions, Xid/GSP/AER/hardware-error kernel logs, PCIe link, NIC, MCTP, boot
  clock, and saves `raw/hwerr.txt` / `raw/hwerr_prev.txt`), `bmc-openbmc`
  (for now the linux set; the Redfish/HMC set is not written yet).
- **Type** host/bmc and **Server** (e.g. `tray-07`) link the host and BMC rows
  of one tray: look at both rows together.
- **BMC rows** (Type `bmc` or Profile `bmc-openbmc`) also read the **HMC**
  behind the BMC (**HMC IP** field, empty = 172.31.13.251, `none` = skip),
  through the BMC with Redfish GET only: health, firmware, event logs, dump
  list, CPER (decoded with NVCPERTool when installed, into `cper_hmc/`) and the
  BMC's copy of the HMC console. Flags cover only events that are new since
  the previous check (by event Id); the full history is in the report's
  `hmc` section and `raw/hmc_redfish.txt`.
- **Stage** ES/QS/PS/MP, **Expected GPUs / PCIe / NVLink / FW**: compared in
  the flags (FW not yet).
- Results shown on the page: Status, Last check, Flags, CPER (record count; decoding is
  not automated yet), Summary (yours, via `note`).

## Workflow when asked to debug the systems

The user prefers **log and journal surveys** on these systems: read what the
logs already say (journal, kernel log, BMC copies of the host/HMC consoles, RAS
logs, HMC event logs) and do not start extra tests unless asked.

1. `fleet.py list`, then `fleet.py check` (all, or the ones asked about).
2. For every system that is not OK, and every row with a Problem filled in,
   read `reports/<name>/latest.txt` and look into the cause with more
   **read-only** commands via `fleet.py run` (logs, `systemctl status`,
   `journalctl -u`, config files, `dmesg`, `ip`, `smartctl -H` with --sudo...).
   Use `collect` when you need an output in full (long logs, hex dumps).
3. Write a one-line conclusion per system with `fleet.py note`, e.g.
   "nginx failed: port 80 taken by apache2; fix: disable one of them".
4. Give the user a short table: system, status, cause, suggested fix.

## Rules

- **Never print, cat, grep or copy passwords**: do not open systems.json with
  other tools or print its password fields; `fleet.py` reads it for you. Systems
  are added and changed by the user on the /fleet page, not by you.
- **Read-only by default.** Anything that changes a system (restart a service,
  edit a file, install, reboot, kill a process, delete logs) needs the user's
  explicit OK in this chat first, per system and per command. Say exactly what
  you will run and on which system.
- These are test systems on the bench network; do not scan or connect to
  addresses that are not in the list.
- UNREACHABLE / AUTH FAILED rows: report them, don't guess passwords or ports.
- HOST KEY CHANGED: ask the user whether the system was replaced before
  running `fleet.py forget`.
- CONSOLE LOGIN: the console shows a login prompt. Never type credentials
  into a console; tell the user.

## GPU servers (Profile gpu-vera / bmc-openbmc)

- Read-only applies to firmware too: never start a firmware update, BIOS
  change, driver install/removal, GPU reset, or BMC/HMC reboot without the
  user's explicit OK per system and per command.
- Console-access hosts: always go through `fleet.py run/collect` (one command
  at a time, waits for the end marker); never type credentials into a console.
- Always confirm GPU health on the host (nvidia-smi, kern_gpu) before calling a
  GPU bad. BMC/HMC GPU telemetry null while nvidia-smi on the host is healthy
  points at the telemetry path (HMC / SMA / sensor), not a dead GPU.
- **Read `~/fleet/gpu-notes.md` first** (on the Pi only, never in git): the
  known false alarms and firmware rules for these trays, taken from NVIDIA
  documents. Keep its "not yet verified" labels when you quote it, and add
  what you learn there, not in this file.
- Hardware Error lines (CPER flag): decode `raw/hwerr.txt` on the Pi with
  `python3 -I ~/fleet/mkcper.py reports/<name>/raw/hwerr.txt reports/<name>/cper/`
  and then NVCPERTool (`~/nvcpertool/`, top-level `catalog_rules.json`).
  Quote the action code: HW- = hardware suspect, SW-X = no rule, send the raw
  CPER to NVIDIA, RPT-/IGNORE- = informational. mkcper.py synthesizes the
  record header (timestamp/platform ID zero): say so.
- HMC (behind the BMC): GET requests only, from the BMC shell (`fleet.py check`
  does this for BMC rows; for more, `fleet.py run <bmc> -- 'curl -s http://172.31.13.251/redfish/v1/...'`).
  Never POST/PATCH/DELETE (no dump creation, resets or log clearing).
  HMC SSH credentials are unknown: never guess them.
- NVOnline answers can cite pages they didn't return: label unverified claims
  as such. NVOnline downloads need the user to accept the EULA in chat.
- NVOnline documents and NVCPERTool are NVIDIA confidential: never put them,
  or text copied from them, into the system list, git, or any external service.

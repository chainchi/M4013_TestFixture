# M4013 test fixture Pi

This is the Raspberry Pi 5 of the M4013 test fixture (pi-tester web service in
/opt/pi-tester, port 80; source repo on the developer's PC).

- **Fleet debugging** (systems listed on the /fleet page, checked over
  SSH): read `~/fleet/CLAUDE.md` first and use `~/fleet/fleet.py`. Never print
  or open the passwords in `~/fleet/systems.json` yourself.
- The terminal you run in is the tmux session `shared`; Claude Code on the
  developer PC may type messages into it (they say so). Other tabs t1, t2, ...
  are separate shells.

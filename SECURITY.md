# Security

This integration can open a door, so security reports are taken seriously.

**Please do not open a public issue for a vulnerability.** Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/kienxux/ezviz_instant_bell/security/advisories/new).
Describe what you found and how to reproduce it; do not include your EZVIZ password or keys.

What is helpful to know about the design:

- The EZVIZ login is stored by Home Assistant like for any integration.
- Opening the door is always a deliberate action (button, action or card) and is recorded with the Home Assistant user.
- The live-video relay listens on `127.0.0.1` by default; making it reachable by other hosts is an explicit option and has no password.
- Actions that change video encryption or fetch its key need a Home Assistant administrator.

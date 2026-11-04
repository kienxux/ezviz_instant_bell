# Contributing

Bug reports, ideas and pull requests are welcome.

## Reporting a bug

Use the bug report form. It asks for the version, your device model and the **diagnostics download**
(*Settings > Devices & services > EZVIZ HP7 / CP7 Video Intercom > three dots > Download diagnostics*), which contains no
passwords, account names, serial numbers, addresses or keys. Debug logs help too; remove secrets before pasting.

## Proposing something

Look at [ROADMAP.md](ROADMAP.md) and the open issues first. Describe the situation you want to solve, not only the
solution.

## Working on the code

```bash
python -m pip install pytest-homeassistant-custom-component pyezvizapi PyTurboJPEG
python -m pytest -q tests
```

- Tests use a fake EZVIZ client, so no account or device is needed. Put new behaviour under a test.
- The parts that need no Home Assistant (`parsing`, `history`, `recordings`, `visitors`, `keys`) are tested without it.
- Keep user-visible text in `strings.json` and `translations/en.json` (they must match), and add it to the other languages; `tests/test_translations.py` checks that.
- Code that has only been run against a fake client should say so in the pull request. Real-device results are what turn a guess into a fact.

"""Which entries are motion or person events, and where their pictures go (no Home Assistant needed)."""

from datetime import date, datetime

from alarms import is_detection
from detection_pictures import FOLDER, picture_path, prune, slug, write_picture


def test_motion_and_person_entries_are_detections():
    assert is_detection("Motion Detection Alarm", 2401)
    assert is_detection("Human detected", None)
    assert is_detection("Bewegung erkannt", 12345)  # any language: only rings and openings are known


def test_rings_and_openings_are_not():
    assert not is_detection("Your doorbell is ringing", 2701)
    assert not is_detection("Some new text", 2701)  # a ring by its subtype alone
    assert not is_detection("Your doorbell is ringing", None)  # a ring by its title alone
    assert not is_detection("EZVIZ app unlock the lock", 0)
    assert not is_detection("Monitor open the gate", None)
    assert not is_detection("", 10243)


def test_slug_is_safe_for_any_file_system():
    assert slug("Motion Detection Alarm") == "motion_detection_alarm"
    assert slug("Tor / Haustür") == "tor_haust_r"
    assert slug("") == slug("!!!") == "event"
    assert len(slug("x" * 100)) == 40


def test_picture_goes_into_a_folder_per_door_station_and_day(tmp_path):
    when = datetime(2026, 11, 5, 14, 3, 12)
    path = picture_path(tmp_path, "Front door", when, "Motion Detection Alarm")
    assert path == tmp_path / FOLDER / "front_door" / "2026-11-05" / "14-03-12_motion_detection_alarm.jpg"
    write_picture(path, b"JPEG")
    assert path.read_bytes() == b"JPEG"
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no .part file left
    second = picture_path(tmp_path, "Front door", when, "Motion Detection Alarm")
    assert second.name == "14-03-12_motion_detection_alarm_2.jpg"


def test_prune_removes_only_old_day_folders(tmp_path):
    device = tmp_path / "front_door"
    for name in ("2026-11-05", "2026-11-04", "2026-11-03", "2026-10-01", "notes"):
        (device / name).mkdir(parents=True)
        (device / name / "a.jpg").write_bytes(b"x")
    (device / "2026-01-01.jpg").write_bytes(b"x")
    removed = prune(device, date(2026, 11, 5), 2)
    assert sorted(p.name for p in removed) == ["2026-10-01", "2026-11-03"]
    assert sorted(p.name for p in device.iterdir()) == ["2026-01-01.jpg", "2026-11-04", "2026-11-05", "notes"]


def test_prune_of_a_missing_folder_does_nothing(tmp_path):
    assert prune(tmp_path / "missing", date(2026, 11, 5), 30) == []

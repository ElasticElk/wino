from pathlib import Path


def test_export_tracks_per_container_since():
    src = (Path(__file__).resolve().parents[1] / "scripts/export_scanner_logs.sh").read_text()
    assert ".$c.last_run" in src
    assert 'docker logs --timestamps --since "$since" "$c" > "$tmp" 2>&1' in src

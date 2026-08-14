"""Radar configuration loading, and the shipped document.

The shipped document is tested rather than merely present because it is the file a bench session
starts from, and a typo in it would be discovered at the worst possible moment — with the
hardware on a tripod and the operator wondering why nothing decodes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.common.config import ConfigError
from sensors.radar.config import (
    DEFAULT_RADAR_CONFIG_PATH,
    MountConfig,
    RadarConfig,
    RawLogConfig,
    SerialLinkConfig,
    available_radar_configs,
    load_radar_config,
)


class TestShippedDocument:
    def test_the_testbed_document_loads(self) -> None:
        config = load_radar_config("testbed_radar")

        assert config.sensor_id == "testbed_radar"
        assert config.link.port

    def test_it_is_listed(self) -> None:
        assert "testbed_radar" in available_radar_configs()

    def test_every_document_in_the_tree_loads(self) -> None:
        """A document that does not load is a deployment that will not start, and the failure
        would appear at startup rather than in review."""
        for name in available_radar_configs():
            load_radar_config(name)

    def test_raw_logging_is_on_for_the_testbed(self) -> None:
        """Required before any detection logic built on this unit is trusted — the protocol
        constants are unverified, so the bytes are the only evidence of what went wrong."""
        assert load_radar_config("testbed_radar").raw_log.enabled is True

    def test_the_declared_max_range_does_not_exceed_what_the_hardware_accepts(self) -> None:
        config = load_radar_config("testbed_radar")

        assert config.max_range_m >= config.tuning.max_distance_m


class TestLoading:
    def test_a_missing_document_names_what_is_available(self, tmp_path: Path) -> None:
        (tmp_path / "one.json").write_text("{}")

        with pytest.raises(ConfigError, match="no such radar configuration"):
            load_radar_config("absent", tmp_path)

    def test_a_document_whose_id_disagrees_with_its_filename_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A file copied and edited but not renamed is indistinguishable from the unit it claims
        to be, and that mistake puts one radar's mount tilt on another radar's returns."""
        source = json.loads((DEFAULT_RADAR_CONFIG_PATH / "testbed_radar.json").read_text())
        (tmp_path / "second_radar.json").write_text(json.dumps(source))

        with pytest.raises(ConfigError, match="must agree"):
            load_radar_config("second_radar", tmp_path)

    def test_an_unknown_field_is_rejected_rather_than_ignored(self, tmp_path: Path) -> None:
        """A setting nobody reads is a setting an operator believes is in effect."""
        source = json.loads((DEFAULT_RADAR_CONFIG_PATH / "testbed_radar.json").read_text())
        source["sensitivty"] = 3
        (tmp_path / "testbed_radar.json").write_text(json.dumps(source))

        with pytest.raises(ConfigError):
            load_radar_config("testbed_radar", tmp_path)

    def test_an_empty_directory_lists_nothing_rather_than_failing(self, tmp_path: Path) -> None:
        assert available_radar_configs(tmp_path) == []


class TestValidation:
    def test_an_inverted_range_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_range_m"):
            RadarConfig(
                sensor_id="x",
                model="y",
                description="z",
                link=SerialLinkConfig(port="/dev/null"),
                min_range_m=100.0,
                max_range_m=10.0,
            )

    def test_inverted_backoff_bounds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="reconnect_initial_delay_s"):
            SerialLinkConfig(
                port="/dev/null", reconnect_initial_delay_s=30.0, reconnect_max_delay_s=1.0
            )

    def test_probing_without_candidates_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="probe_baud_rates"):
            SerialLinkConfig(port="/dev/null", probe_on_connect=True, probe_baud_rates=[])

    def test_raw_logging_without_a_path_is_rejected(self) -> None:
        """Enabling a log that writes nowhere would give a session a false sense of having
        recorded itself."""
        with pytest.raises(ValueError, match="no path"):
            RawLogConfig(enabled=True)

    def test_a_beam_cannot_have_zero_width(self) -> None:
        """The elevation extent is the direct source of the vertical uncertainty the adapter
        reports, so a zero here would claim the unit knows an altitude it cannot measure."""
        with pytest.raises(ValueError):
            MountConfig(elevation_extent_deg=0.0)

    def test_tilt_is_bounded_to_physically_meaningful_angles(self) -> None:
        with pytest.raises(ValueError):
            MountConfig(tilt_deg=120.0)

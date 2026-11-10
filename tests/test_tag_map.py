from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from model.tag_model import TagMapFile, identification_placements

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tag_map.example.json"
LIVE = ROOT / "tag_map.json"


def _tag(tag_id: str = "robot1.power") -> dict:
    return {
        "tag_id": tag_id,
        "enabled": True,
        "source": {"kind": "robot", "device_key": "robot-1", "addr": "S0004"},
        "target": {"path": "Factory/Line01/HH7_01", "name": "PowerYesno", "data_type": "Boolean"},
    }


def _header(**overrides: object) -> dict:
    data: dict[str, object] = {
        "kind": "robot",
        "device_key": "robot-1",
        "aas_id": "urn:example:aas:hyundai:hh7:pickplace:01",
        "global_asset_id": "urn:example:asset:hyundai:hh7:pickplace:01",
    }
    data.update(overrides)
    return data


class CollectorHeaderTest(unittest.TestCase):
    def test_legacy_tags_without_collectors_still_load(self) -> None:
        tag_map = TagMapFile.model_validate({"tags": [_tag()]})
        self.assertEqual(tag_map.collectors, [])
        self.assertEqual(identification_placements(tag_map.collectors, "Collectors"), [])

    def test_header_is_per_collector_not_per_sample(self) -> None:
        tag_map = TagMapFile.model_validate({"collectors": [_header()], "tags": [_tag()]})
        placements = identification_placements(tag_map.collectors, "Collectors")
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0][0], ("Collectors", "robot", "robot-1"))
        self.assertEqual(placements[0][1].aas_id, "urn:example:aas:hyundai:hh7:pickplace:01")

    def test_header_without_aas_ids_has_no_identification(self) -> None:
        tag_map = TagMapFile.model_validate({"collectors": [_header(aas_id="", global_asset_id="")]})
        self.assertEqual(identification_placements(tag_map.collectors, "Collectors"), [])

    def test_duplicate_collector_is_error(self) -> None:
        with self.assertRaises(ValidationError):
            TagMapFile.model_validate({"collectors": [_header(), _header(aas_id="other")]})

    def test_example_tag_map_validates(self) -> None:
        tag_map = TagMapFile.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue(any(c.key == ("robot", "robot-1") for c in tag_map.collectors))
        self.assertTrue({1737, 1703}.issubset({tag.node_id for tag in tag_map.tags if tag.node_id is not None}))
        self.assertEqual(
            {tag.node_id for tag in tag_map.tags if tag.target.path.endswith("JointSpeed")},
            {1160, 1161, 1162, 1163, 1164, 1165},
        )
        self.assertTrue(
            any(t.source.socket == "hw" and t.source.field == "cpu_temperature" for t in tag_map.tags)
        )
        self.assertTrue(
            any(t.source.socket == "jv1" and t.source.field == "value_0" for t in tag_map.tags)
        )

    def test_socket_source_requires_field(self) -> None:
        with self.assertRaises(ValidationError):
            TagMapFile.model_validate(
                {"tags": [_tag() | {"source": {"kind": "robot", "device_key": "robot-1", "socket": "hw"}}]}
            )

    def test_addr_and_socket_together_is_error(self) -> None:
        with self.assertRaises(ValidationError):
            TagMapFile.model_validate(
                {
                    "tags": [
                        _tag()
                        | {
                            "source": {
                                "kind": "robot",
                                "device_key": "robot-1",
                                "addr": "S0004",
                                "socket": "hw",
                                "field": "cpu_temperature",
                            }
                        }
                    ]
                }
            )

    def test_duplicate_node_id_is_error(self) -> None:
        with self.assertRaises(ValidationError):
            TagMapFile.model_validate(
                {"tags": [_tag("1739") | {"node_id": 1739}, _tag("1737") | {"node_id": 1739}]}
            )

    def test_string_node_id_is_accepted(self) -> None:
        tag_map = TagMapFile.model_validate(
            {
                "tags": [
                    _tag("jp1.value_0")
                    | {"node_id": "ns=2;s=HH7_AAS_Template_v0.2/HH7/RobotKinematicState/JointPosition/J1"}
                ]
            }
        )
        self.assertEqual(
            tag_map.tags[0].node_id,
            "ns=2;s=HH7_AAS_Template_v0.2/HH7/RobotKinematicState/JointPosition/J1",
        )

    def test_duplicate_string_node_id_is_error(self) -> None:
        node_id = "ns=2;s=HH7_AAS_Template_v0.2/HH7/RobotKinematicState/JointPosition/J1"
        with self.assertRaises(ValidationError):
            TagMapFile.model_validate(
                {"tags": [_tag("jp1.value_0") | {"node_id": node_id}, _tag("jp1.value_1") | {"node_id": node_id}]}
            )

    def test_live_tag_map_validates(self) -> None:
        tag_map = TagMapFile.model_validate_json(LIVE.read_text(encoding="utf-8"))
        self.assertTrue(
            any(
                t.node_id == "ns=1;s=Hyundai_HH7_AAS_Template_v0.3/HH7/RobotKinematicState/JointPosition/J1"
                for t in tag_map.tags
            )
        )


if __name__ == "__main__":
    unittest.main()

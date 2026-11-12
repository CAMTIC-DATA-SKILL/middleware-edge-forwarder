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

    def test_nested_tags_expand_under_collector_roots(self) -> None:
        tag_map = TagMapFile.model_validate(
            {
                "collectors": [
                    {
                        "kind": "robot",
                        "device_key": "m1509",
                        "aas_id": "urn:aas:m1509",
                        "browse_root": "Doosan_M1509",
                        "node_root": "ns=1;s=Doosan_M1509_AAS_Template_v0.3/Doosan_M1509",
                        "tags": [
                            {
                                "id": "m1509.j1_position",
                                "addr": "270",
                                "node": "RobotKinematicState/JointPosition/J1",
                                "type": "Double",
                            }
                        ],
                    }
                ]
            }
        )
        tag = tag_map.tags[0]
        self.assertEqual(tag.source.device_key, "m1509")
        self.assertEqual(tag.source.addr, "270")
        self.assertEqual(tag.target.path, "Doosan_M1509/RobotKinematicState/JointPosition")
        self.assertEqual(tag.target.name, "J1")
        self.assertEqual(
            tag.node_id,
            "ns=1;s=Doosan_M1509_AAS_Template_v0.3/Doosan_M1509/RobotKinematicState/JointPosition/J1",
        )
        self.assertEqual(tag_map.collectors[0].aas_id, "urn:aas:m1509")

    def test_same_relative_node_is_distinct_per_browse_root(self) -> None:
        def block(device: str, root: str, node_root: str) -> dict:
            return {
                "kind": "robot",
                "device_key": device,
                "browse_root": root,
                "node_root": node_root,
                "tags": [
                    {
                        "id": f"{device}.j1",
                        "addr": "1",
                        "node": "RobotKinematicState/JointPosition/J1",
                        "type": "Double",
                    }
                ],
            }

        tag_map = TagMapFile.model_validate(
            {
                "collectors": [
                    block("hh7", "", "ns=1;s=Hyundai_HH7_AAS_Template_v0.3/HH7"),
                    block("m1509", "Doosan_M1509", "ns=1;s=Doosan_M1509_AAS_Template_v0.3/Doosan_M1509"),
                ]
            }
        )
        self.assertEqual(
            [tag.target.path for tag in tag_map.tags],
            [
                "RobotKinematicState/JointPosition",
                "Doosan_M1509/RobotKinematicState/JointPosition",
            ],
        )

    def test_same_browse_path_after_expand_is_error(self) -> None:
        def block(device: str, node_root: str) -> dict:
            return {
                "kind": "robot",
                "device_key": device,
                "browse_root": "Shared",
                "node_root": node_root,
                "tags": [
                    {
                        "id": f"{device}.j1",
                        "addr": "1",
                        "node": "RobotKinematicState/JointPosition/J1",
                        "type": "Double",
                    }
                ],
            }

        with self.assertRaises(ValidationError):
            TagMapFile.model_validate(
                {
                    "collectors": [
                        block("a", "ns=1;s=A"),
                        block("b", "ns=1;s=B"),
                    ]
                }
            )

    def test_link_false_keeps_browse_path_without_node_id(self) -> None:
        tag_map = TagMapFile.model_validate(
            {
                "collectors": [
                    {
                        "kind": "robot",
                        "device_key": "robot-1",
                        "node_root": "ns=1;s=Hyundai_HH7_AAS_Template_v0.3/HH7",
                        "tags": [
                            {
                                "id": "1737",
                                "addr": "S0041",
                                "node": "OperationalData/EquipmentState/OperationType",
                                "type": "String",
                                "link": False,
                                "description": "robot status",
                            }
                        ],
                    }
                ]
            }
        )
        tag = tag_map.tags[0]
        self.assertIsNone(tag.node_id)
        self.assertEqual(tag.target.path, "OperationalData/EquipmentState")
        self.assertEqual(tag.target.name, "OperationType")
        self.assertEqual(tag.target.description, "robot status")

    def test_live_tag_map_validates(self) -> None:
        tag_map = TagMapFile.model_validate_json(LIVE.read_text(encoding="utf-8"))
        by_id = {tag.tag_id: tag for tag in tag_map.tags}
        self.assertEqual(len(tag_map.tags), 114)
        self.assertEqual(
            by_id["jp1.value_0"].node_id,
            "ns=1;s=Hyundai_HH7_AAS_Template_v0.3/HH7/RobotKinematicState/JointPosition/J1",
        )
        self.assertEqual(by_id["jp1.value_0"].target.path, "RobotKinematicState/JointPosition")
        self.assertIsNone(by_id["1737"].node_id)
        self.assertEqual(
            by_id["m1509.j1_position"].node_id,
            "ns=1;s=Doosan_M1509_AAS_Template_v0.3/Doosan_M1509/RobotKinematicState/JointPosition/J1",
        )
        self.assertEqual(
            by_id["m1509.j1_position"].target.path,
            "Doosan_M1509/RobotKinematicState/JointPosition",
        )
        self.assertEqual(
            by_id["ls6.rz"].node_id,
            "ns=1;s=Epson_LS6_602_AAS_Template_v0.1/Epson_LS6_602/RobotKinematicState/TCPOrientation/Component1",
        )
        self.assertEqual(
            by_id["dr0001.j1_position"].node_id,
            "ns=1;s=Igus_DLE_DR_0001_AAS_Template_v0.1/Igus_DLE_DR_0001/RobotKinematicState/JointPosition/J1",
        )


if __name__ == "__main__":
    unittest.main()

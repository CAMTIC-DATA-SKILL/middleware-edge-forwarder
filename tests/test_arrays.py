"""1차원 배열 샘플이 OPC UA 노드로 왕복하는지 확인한다."""
from __future__ import annotations

import unittest

from asyncua import Client, ua

from model.app_model import OpcUaServerSettings
from opcua_server.server import NorthboundServer, TagNodeSpec
from opcua_server.values import coerce_array, node_type


class NodeTypeTest(unittest.TestCase):
    def test_scalar_stays_scalar(self) -> None:
        self.assertEqual(node_type("f64", 1.5), (ua.VariantType.Double, False))
        self.assertEqual(node_type(None, 3), (ua.VariantType.Int64, False))

    def test_declared_array_without_value(self) -> None:
        self.assertEqual(node_type("f64[]", None), (ua.VariantType.Double, True))

    def test_numeric_list_promotes_to_double(self) -> None:
        self.assertEqual(node_type(None, [1, 2.5]), (ua.VariantType.Double, True))

    def test_empty_list_waits(self) -> None:
        self.assertIsNone(node_type(None, []))

    def test_nested_list_rejected(self) -> None:
        self.assertIsNone(node_type(None, [[1.0, 2.0]]))

    def test_coerce_array(self) -> None:
        self.assertEqual(coerce_array([1, 2.5], ua.VariantType.Double), [1.0, 2.5])


class ArrayNodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_double_array_roundtrip(self) -> None:
        endpoint = "opc.tcp://127.0.0.1:14841/edge-array-test/"
        server = NorthboundServer(
            OpcUaServerSettings(endpoint=endpoint, server_name="array-test", application_uri="urn:test:array")
        )
        await server.start()
        try:
            added = await server.add_tag(
                TagNodeSpec(
                    tag_id="robot.amr.scan_ranges",
                    segments=("Collectors", "robot", "amr"),
                    name="scan_ranges",
                    vtype=ua.VariantType.Double,
                    source_id="robot/amr",
                    is_array=True,
                )
            )
            self.assertTrue(added)
            await server.add_tag(
                TagNodeSpec(
                    tag_id="robot.amr.odom_x",
                    segments=("Collectors", "robot", "amr"),
                    name="odom_x",
                    vtype=ua.VariantType.Double,
                    source_id="robot/amr",
                )
            )
            await server.update("robot.amr.scan_ranges", [0.2, 1, 12.0])
            await server.update("robot.amr.odom_x", 1.25)

            async with Client(url=endpoint) as client:
                ns = server._ns
                ranges = client.get_node(ua.NodeId("robot.amr.scan_ranges", ns))
                pose = client.get_node(ua.NodeId("robot.amr.odom_x", ns))
                self.assertEqual(await ranges.read_value_rank(), ua.ValueRank.OneDimension)
                self.assertEqual(await ranges.read_value(), [0.2, 1.0, 12.0])
                self.assertEqual(await pose.read_value_rank(), ua.ValueRank.Scalar)
                self.assertEqual(await pose.read_value(), 1.25)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup_script = (ROOT / "eMSN_ENV/setup_env.sh").read_text(
            encoding="utf-8"
        )

    def _block(self, start_marker, end_marker):
        start = self.setup_script.index(start_marker)
        end = self.setup_script.index(end_marker, start)
        return self.setup_script[start:end]

    def test_emitter_advertises_resolvable_flowblocker_peer_name(self):
        ryu_block = self._block("# Ryu Core", "# SimpleSwitch REST")
        flowblocker_block = self._block("# FlowBlocker", "# Connect FB")

        self.assertIn('-e FB_PEER_ID="flow-blocker-$i"', ryu_block)
        # FB_PEER_ID is consumed by the emitter inside ryu-core, not by the
        # FlowBlocker process itself.
        self.assertNotIn("FB_PEER_ID", flowblocker_block)

    def test_all_old_etcd_nodes_are_removed_before_first_new_node_starts(self):
        cluster_block = self._block(
            'log "Removing previous ETCD nodes"',
            "# Wait for at least one endpoint",
        )
        remove_call = cluster_block.index('docker_rm_if "etcd${j}"')
        removal_loop_end = cluster_block.index("done", remove_call)
        first_docker_run = cluster_block.index("sudo docker run")

        self.assertLess(removal_loop_end, first_docker_run)


if __name__ == "__main__":
    unittest.main()

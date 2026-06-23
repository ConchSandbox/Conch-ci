from pathlib import Path
import unittest


class SetupLocalRegistryActionTest(unittest.TestCase):
    def test_recreates_registry_container_with_bind_mount_storage(self):
        repo_root = Path(__file__).resolve().parents[2]
        action = repo_root.joinpath(".github/actions/setup-local-registry/action.yml").read_text()

        self.assertIn('sudo -n "$runtime" rm -fv "$CONTAINER_NAME"', action)
        self.assertIn('sudo -n "$runtime" volume prune -f || true', action)
        self.assertIn('storage_args=(-v "$STORAGE_PATH:/var/lib/registry")', action)
        self.assertNotIn("inspect", action)
        self.assertNotIn("registry_storage", action)


if __name__ == "__main__":
    unittest.main()

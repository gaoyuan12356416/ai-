import os
import subprocess
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.gpu_affinity import bind_prepare_process, discover_devices


class GPUAffinityTests(unittest.TestCase):
    def test_even_slots_split_and_replacement_retains_device(self):
        config = {"backend": "gpu", "gpu_devices": ["GPU-one", "GPU-two"]}
        assigned = [bind_prepare_process(config, "prepare", i, {}) for i in range(32)]
        self.assertEqual(assigned.count("GPU-one"), 16)
        self.assertEqual(assigned.count("GPU-two"), 16)
        self.assertEqual(bind_prepare_process(config, "prepare", 7, {}), assigned[7])

    def test_visibility_is_inherited_by_renderer_subprocess(self):
        import sys
        env = dict(os.environ)
        bind_prepare_process({"backend": "gpu", "gpu_devices": ["GPU-one", "GPU-two"]}, "prepare", 1, env)
        child = subprocess.check_output([sys.executable, "-c", "import os;print(os.environ['CUDA_VISIBLE_DEVICES'])"], env=env, text=True)
        self.assertEqual(child.strip(), "GPU-two")

    def test_existing_visibility_and_non_gpu_roles(self):
        self.assertEqual(discover_devices({"CUDA_VISIBLE_DEVICES": "GPU-b,GPU-a"}), ["GPU-b", "GPU-a"])
        for backend, stage in [("cpu", "prepare"), ("gpu", "publish")]:
            env = {"CUDA_VISIBLE_DEVICES": "GPU-original"}
            self.assertIsNone(bind_prepare_process({"backend": backend}, stage, 0, env))
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "GPU-original")
        with self.assertRaises(RuntimeError):
            discover_devices({"CUDA_VISIBLE_DEVICES": ""})

    @patch("features.pipeline_dispatch.gpu_affinity.subprocess.run")
    def test_discovery_uses_stable_uuids(self, run):
        run.return_value.stdout = "GPU-one\nGPU-two\n"
        self.assertEqual(discover_devices({}), ["GPU-one", "GPU-two"])


if __name__ == "__main__":
    unittest.main()

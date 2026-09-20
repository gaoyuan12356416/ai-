"""Offline contracts for the optional topmost, synchronous source-video layer.

Real CUDA/OpenCL execution and pixel comparisons are release preflight checks;
these tests exercise frozen recipes, strict validation, routing and receipts.
"""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis import core, gpu, gpu_compositor, native_gpu
from features.drama_synthesis.composition import (
    compile_random_overlay_spec, composition_sha256, plan_chunks,
    selected_asset_identities, validate_composition_spec,
)
from features.drama_synthesis.local_checkpoint import file_fingerprint
from scripts.test_drama_gpu_compositor_v2 import CompositorFixture, canonical
from scripts.test_drama_synthesis_upgrade import JOB_ID, catalog, recipe


OVERLAY = {"version": 1, "opacity_bp": 350, "scale_bp": 13000}


def rehash(value):
    unsigned = {key: item for key, item in value.items() if key != "recipe_sha256"}
    return {**unsigned, "recipe_sha256": hashlib.sha256(canonical(unsigned).encode()).hexdigest()}


def with_overlay(value, overlay=OVERLAY):
    return rehash({**value, "source_overlay": deepcopy(overlay)})


def malformed_overlays():
    yield from (None, True, False, 1, 1.0, "legacy", [], {}, {"version": 1})
    for key in OVERLAY:
        yield {name: value for name, value in OVERLAY.items() if name != key}
        for value in (None, True, False, "1", 1.0, [], {}):
            yield {**OVERLAY, key: value}
    yield {**OVERLAY, "unexpected": 0}
    for key, values in (("version", (0, 2)), ("opacity_bp", (199, 501)), ("scale_bp", (10999, 15001))):
        for value in values:
            yield {**OVERLAY, key: value}


class SourceOverlayRecipeTests(unittest.TestCase):
    def test_new_auto_and_manual_recipes_use_existing_identity_and_stable_labels(self):
        automatic = recipe()
        manual = recipe(mode="manual", layers={key: rows[0]["name"] for key, rows in catalog()["categories"].items()})
        expected = {"version": 1, "opacity_bp": 239, "scale_bp": 14796}
        self.assertEqual(automatic["source_overlay"], expected)
        self.assertEqual(manual["source_overlay"], expected)
        self.assertEqual(automatic, recipe())
        self.assertEqual(core.RECIPE_VERSION, 1)
        self.assertEqual(core.RECIPE_PROFILE, "drama-random-overlay-h264-720x1280-v1")
        self.assertEqual(automatic["recipe_sha256"], rehash(automatic)["recipe_sha256"])

    def test_existing_geometry_assets_and_unsigned_legacy_hash_are_unchanged(self):
        value = recipe()
        value.pop("source_overlay")
        self.assertEqual(rehash(value)["recipe_sha256"],
                         "d90649cb83dbd5cc522dd375e855a8ec0e711884029caa06a91be0a36cf9269b")

    def test_opacity_and_scale_generation_include_both_boundary_values(self):
        original = core._stable_int
        for boundary in ("minimum", "maximum"):
            def choose(identity, label, low, high):
                return (low if boundary == "minimum" else high) if label.startswith("source-overlay-") else original(identity, label, low, high)
            with self.subTest(boundary=boundary), mock.patch.object(core, "_stable_int", side_effect=choose):
                value = recipe()["source_overlay"]
                self.assertEqual(value, {"version": 1, "opacity_bp": 200 if boundary == "minimum" else 500,
                                         "scale_bp": 11000 if boundary == "minimum" else 15000})

    def test_source_choice_is_frozen_and_new_values_are_in_range(self):
        seen = set()
        for number in range(128):
            for source in ("concat_video", "no_bgm_video"):
                value = core.freeze_random_recipe(job_id="%032x" % number, content_id="content",
                    request={"mode": "auto", "source": source}, catalog=catalog())
                overlay = core.validate_source_overlay(value)
                self.assertEqual(value["source"], source)
                self.assertTrue(200 <= overlay["opacity_bp"] <= 500)
                self.assertTrue(11000 <= overlay["scale_bp"] <= 15000)
                seen.add(tuple(overlay.values()))
        self.assertGreater(len(seen), 200)

    def test_missing_is_legacy_but_all_malformed_present_objects_fail(self):
        self.assertIsNone(core.validate_source_overlay({}))
        for overlay in malformed_overlays():
            with self.subTest(overlay=overlay), self.assertRaises(core.DramaSynthesisError) as caught:
                core.validate_source_overlay({"source_overlay": overlay})
            self.assertEqual(caught.exception.code, "drama_source_overlay_invalid")

    def test_cpu_store_rejects_malformed_objects_even_with_recomputed_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.DramaSynthesisStore(Path(directory) / "jobs.sqlite3")
            store.ensure_storage()
            for overlay in malformed_overlays():
                with self.subTest(overlay=overlay), self.assertRaises(core.DramaSynthesisError):
                    store.freeze_recipe(JOB_ID, with_overlay(recipe(), overlay))
            self.assertIsNone(store.recipe(JOB_ID))

    def test_legacy_store_replay_stays_byte_identical_and_cannot_be_upgraded(self):
        old = recipe()
        old.pop("source_overlay")
        old = rehash(old)
        before = canonical(old)
        with tempfile.TemporaryDirectory() as directory:
            store = core.DramaSynthesisStore(Path(directory) / "jobs.sqlite3")
            store.ensure_storage()
            first = store.freeze_recipe(JOB_ID, old)
            second = store.freeze_recipe(JOB_ID, deepcopy(old))
            self.assertEqual(first["recipe_json"], before)
            self.assertEqual(first, second)
            self.assertEqual(store.recipe(JOB_ID)["recipe"], old)
            self.assertNotIn("source_overlay", store.recipe(JOB_ID)["recipe"])
            with self.assertRaises(core.DramaSynthesisError) as caught:
                store.freeze_recipe(JOB_ID, with_overlay(old))
            self.assertEqual(caught.exception.code, "drama_recipe_immutable_conflict")

    def test_new_store_replay_preserves_both_parameters_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.DramaSynthesisStore(Path(directory) / "jobs.sqlite3")
            store.ensure_storage()
            value = recipe()
            store.freeze_recipe(JOB_ID, value)
            self.assertEqual(store.recipe(JOB_ID)["recipe"], value)
            changed = deepcopy(value)
            changed["source_overlay"]["opacity_bp"] += 1
            with self.assertRaises(core.DramaSynthesisError) as caught:
                store.freeze_recipe("b" * 32, changed)
            self.assertEqual(caught.exception.code, "drama_recipe_hash_invalid")


class SourceOverlayFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = CompositorFixture(Path(directory.name))
        self.info = self.fixture.probe("ffprobe", self.fixture.source)
        self.old = compile_random_overlay_spec(self.fixture.recipe, self.info)
        self.new = compile_random_overlay_spec(with_overlay(self.fixture.recipe), self.info)


class SourceOverlaySceneTests(SourceOverlayFixture):
    def test_legacy_scene_and_opencl_kernel_hashes_are_unchanged(self):
        self.assertEqual(len(self.old["layers"]), 6)
        self.assertEqual(composition_sha256(self.old), "ae01aa0f511581db45326846b3f16d46944f291e92e23d250ca882cf472ff15d")
        self.assertEqual(gpu_compositor.compile_opencl_kernel(self.old)["sha256"],
                         "35c869b8e7b3df07eaff1aaa3be27748707362c07ef6eb14d08e01296e36bd1d")
        old_cuda = gpu_compositor.kernel_template_path(self.old, cuda=True).read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(old_cuda.encode()).hexdigest(),
                         "493e54f48e20f35f64edb4dcd104ff9add8bc8bef4e8b2eb1f769b06b4c42b8c")

    def test_new_layer_is_topmost_unrotated_and_uses_same_source_and_timeline(self):
        self.assertEqual(self.new["layers"][:6], self.old["layers"])
        self.assertEqual(self.new["timeline"], self.old["timeline"])
        self.assertEqual(self.new["audio"], self.old["audio"])
        self.assertEqual(self.new["layers"][6], {
            "id": "source-overlay", "kind": "video", "source": "input", "fit": "cover",
            "opacity_bp": 350, "transform": {"rotation_millidegrees": 0, "scale_bp": 13000}, "z_index": 6,
        })
        self.assertEqual(len(list(selected_asset_identities(self.new))), 4)
        self.assertNotEqual(composition_sha256(self.new), composition_sha256(self.old))

    def test_scene_rejects_unexpected_layer_or_overlay_semantics(self):
        changes = (("source", "composited"), ("fit", "contain"), ("kind", "image"),
                   ("z_index", 5), ("id", "asset-overlay"), ("opacity_bp", 199),
                   ("opacity_bp", 501), ("opacity_bp", True), ("opacity_bp", 350.0))
        for key, value in changes:
            scene = deepcopy(self.new)
            scene["layers"][6][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(core.DramaSynthesisError):
                validate_composition_spec(scene)
        for key, value in (("rotation_millidegrees", 1), ("rotation_millidegrees", False),
                           ("scale_bp", 10999), ("scale_bp", 15001), ("scale_bp", 13000.0)):
            scene = deepcopy(self.new)
            scene["layers"][6]["transform"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(core.DramaSynthesisError):
                gpu_compositor.compile_opencl_kernel(scene)
        for modify in (lambda rows: rows.append(deepcopy(rows[-1])), lambda rows: rows.reverse()):
            scene = deepcopy(self.new)
            modify(scene["layers"])
            with self.assertRaises(core.DramaSynthesisError):
                validate_composition_spec(scene)

    def test_both_boundaries_compile_and_affect_scene_and_kernel_identity(self):
        identities = set()
        for opacity, scale in ((200, 11000), (200, 15000), (500, 11000), (500, 15000)):
            scene = compile_random_overlay_spec(with_overlay(self.fixture.recipe,
                {"version": 1, "opacity_bp": opacity, "scale_bp": scale}), self.info)
            kernel = gpu_compositor.compile_opencl_kernel(scene)
            identities.add((composition_sha256(scene), kernel["sha256"]))
            self.assertIn("#define SCENE_SOURCE_OVERLAY_SCALE %.9ff" % (scale / 10000), kernel["source"])
            self.assertIn("#define SCENE_SOURCE_OVERLAY_OPACITY %.9ff" % (opacity / 10000), kernel["source"])
        self.assertEqual(len(identities), 4)

    def test_new_kernel_templates_sample_original_source_after_corners(self):
        cl = gpu_compositor.compile_opencl_kernel(self.new)["source"]
        cu = gpu_compositor.kernel_template_path(self.new, cuda=True).read_text(encoding="utf-8")
        self.assertNotEqual(gpu_compositor.kernel_template_path(self.old), gpu_compositor.kernel_template_path(self.new))
        self.assertNotEqual(gpu_compositor.kernel_template_path(self.old, cuda=True), gpu_compositor.kernel_template_path(self.new, cuda=True))
        self.assertLess(cl.index("read_imagef(corners, linear_clear, uv)"), cl.index("sample_cover(source, overlay_uv)"))
        self.assertLess(cu.index("pixel(corners,SCENE_WIDTH,SCENE_HEIGHT,x,y)"), cu.index("cover(src,w,h,overlay_u,overlay_v)"))
        self.assertIn("(uv - (float2)(0.5f, 0.5f)) / SCENE_SOURCE_OVERLAY_SCALE", cl)
        self.assertIn("(u-.5f)/SCENE_SOURCE_OVERLAY_SCALE+.5f", cu)
        self.assertIn("(v-.5f)/SCENE_SOURCE_OVERLAY_SCALE+.5f", cu)
        self.assertIn("source_overlay.w = SCENE_SOURCE_OVERLAY_OPACITY", cl)
        self.assertIn("source_overlay.w=SCENE_SOURCE_OVERLAY_OPACITY", cu)

    def test_opencl_inputs_remain_five_and_audio_is_only_muxed_once(self):
        command = gpu_compositor.build_opencl_chunk_command(ffmpeg="ffmpeg", source=self.fixture.source,
            output=self.fixture.output, spec=self.new, assets=self.fixture.paths,
            asset_media_types={key: row["media_type"] for key, row in self.fixture.rows.items()},
            asset_durations={"opacity_video": 10.0, "corners": 60.0},
            chunk=plan_chunks(7500)[0], kernel_path=self.fixture.root / "new.cl")
        self.assertEqual(command.count(str(self.fixture.source)), 1)
        self.assertEqual(command.count("-i"), 5)
        self.assertIn("-an", command)
        self.assertIn("program_opencl=inputs=5:", command[command.index("-filter_complex") + 1])
        for has_audio in (True, False):
            mux = gpu_compositor.build_audio_mux_command("ffmpeg", self.fixture.root / "joined.mp4",
                self.fixture.source, self.fixture.output, has_audio=has_audio, duration_seconds=250.0)
            self.assertEqual(mux.count("-map"), 2)
            self.assertEqual(mux.count("1:a:0"), 1)
            self.assertNotIn("amix", " ".join(mux))


class SourceOverlayGPUContractTests(SourceOverlayFixture):
    def test_gpu_validation_forwards_exact_overlay_and_keeps_missing_key_absent(self):
        for value in (self.fixture.recipe, with_overlay(self.fixture.recipe)):
            with mock.patch.object(gpu_compositor, "load_asset_set", return_value=self.fixture.asset_set), \
                    mock.patch.object(gpu_compositor, "validate_recipe") as validate:
                sha, _, fb = gpu_compositor._validate_recipe_and_assets(value, self.fixture.root, "b" * 64)
            self.assertEqual(sha, value["recipe_sha256"])
            self.assertEqual(validate.call_args.args[0], fb)
            self.assertEqual("source_overlay" in value, "source_overlay" in fb)
            if "source_overlay" in value:
                self.assertEqual(fb["source_overlay"], value["source_overlay"])

    def test_both_gpu_entrypoints_reject_malformed_before_loading_assets(self):
        for overlay in malformed_overlays():
            value = with_overlay(self.fixture.recipe, overlay)
            with self.subTest(overlay=overlay), mock.patch.object(gpu_compositor, "load_asset_set") as load:
                with self.assertRaises(core.DramaSynthesisError) as caught:
                    gpu_compositor._validate_recipe_and_assets(value, self.fixture.root, "b" * 64)
                self.assertEqual(caught.exception.code, "drama_source_overlay_invalid")
                load.assert_not_called()
            with self.subTest(legacy=overlay), mock.patch.object(gpu, "load_asset_set") as load:
                with self.assertRaises(core.DramaSynthesisError) as caught:
                    gpu._render_random_output_legacy(source=self.fixture.source, output=self.fixture.output,
                        recipe=value, asset_root=self.fixture.root, manifest_sha256="b" * 64)
                self.assertEqual(caught.exception.code, "drama_source_overlay_invalid")
                load.assert_not_called()

    def test_legacy_entrypoint_does_not_drop_the_overlay(self):
        stop = RuntimeError("stop before decoding")
        with mock.patch.object(gpu, "load_asset_set", return_value=self.fixture.asset_set), \
                mock.patch.object(gpu, "validate_recipe") as validate, \
                mock.patch.object(gpu, "_probe", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "stop before decoding"):
                gpu._render_random_output_legacy(source=self.fixture.source, output=self.fixture.output,
                    recipe=with_overlay(self.fixture.recipe), asset_root=self.fixture.root, manifest_sha256="b" * 64)
        self.assertEqual(validate.call_args.args[0]["source_overlay"], OVERLAY)

    def test_tampered_overlay_cannot_reuse_receipt_with_old_hash(self):
        value = with_overlay(self.fixture.recipe)
        value["source_overlay"]["scale_bp"] += 1
        with mock.patch.object(gpu_compositor, "load_asset_set") as load:
            with self.assertRaises(core.DramaSynthesisError) as caught:
                gpu_compositor._validate_recipe_and_assets(value, self.fixture.root, "b" * 64)
        self.assertEqual(caught.exception.code, "drama_recipe_hash_invalid")
        load.assert_not_called()

    def test_new_cuda_receipts_bind_new_kernel_and_recipe_without_reusing_old_chunks(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return self.fixture.runner(command, **kwargs)
        def command(**kwargs):
            return ["fake-native", str(kwargs["output"])]
        with mock.patch.object(gpu_compositor, "frame_pipeline", return_value="cuda"), \
                mock.patch.object(gpu_compositor, "build_native_chunk_command", side_effect=command):
            old_result = self.fixture.render(runner)
            self.assertEqual(len(calls), 5)
            self.fixture.output = self.fixture.root / "new-result.mp4"
            self.fixture.recipe = with_overlay(self.fixture.recipe)
            calls.clear()
            new_result = self.fixture.render(runner)
            self.assertEqual(len(calls), 5)
            self.assertNotEqual(old_result["recipe_sha256"], new_result["recipe_sha256"])
            calls.clear()
            self.assertEqual(self.fixture.render(runner), new_result)
            self.assertEqual(calls, [])
        records = [json.loads(path.read_text(encoding="utf-8")) for path in (self.fixture.root / "cache").glob("*/*/final.json")]
        identities = {row["identity"]["recipe_sha256"]: row["identity"] for row in records}
        self.assertEqual(len(identities), 2)
        new_id = identities[new_result["recipe_sha256"]]
        old_id = identities[old_result["recipe_sha256"]]
        self.assertEqual(new_id["cuda_kernel"], file_fingerprint(gpu_compositor.kernel_template_path(self.new, cuda=True)))
        self.assertEqual(old_id["cuda_kernel"], file_fingerprint(gpu_compositor.kernel_template_path(self.old, cuda=True)))
        self.assertNotEqual(new_id["composition_sha256"], old_id["composition_sha256"])
        self.assertNotEqual(new_id["kernel_sha256"], old_id["kernel_sha256"])

    def test_native_runtime_compiles_selected_template_and_decodes_source_once(self):
        compositions, compiled = [], []
        class Module:
            def __init__(self, code, **_kwargs): compiled.append(code)
            def get_function(self, name):
                return (lambda _grid, _block, args: compositions.append(args)) if name == "compose" else (lambda *_args: None)
        encoder = SimpleNamespace(Encode=lambda _tensor: b"frame", EndEncode=lambda: b"end")
        cp = SimpleNamespace(RawModule=Module, uint8="uint8", asarray=lambda array: array,
            empty=lambda shape, **_kwargs: SimpleNamespace(shape=shape),
            cuda=SimpleNamespace(Device=lambda _index: SimpleNamespace(use=lambda: None),
                get_current_stream=lambda: SimpleNamespace(synchronize=lambda: None)))
        torch = SimpleNamespace(cuda=SimpleNamespace(set_device=lambda _index: None),
            set_num_threads=lambda _count: None, from_dlpack=lambda array: array)
        modules = {"av": SimpleNamespace(), "numpy": SimpleNamespace(), "cupy": cp,
                   "torch": torch, "PyNvVideoCodec": SimpleNamespace(CreateEncoder=lambda *_args, **_kwargs: encoder)}
        frames = [SimpleNamespace(shape=(1080, 1920, 4)) for _ in range(2)]
        def assets(*_args):
            return iter((Fraction(i, 30), object()) for i in range(2))
        plan = {"spec": self.new, "source": str(self.fixture.source), "output": str(self.fixture.output),
                "ffmpeg": "ffmpeg", "chunk": {"start_seconds": 0.0, "frame_count": 2},
                "assets": self.fixture.rows, "asset_durations": {}, "asset_cache_root": str(self.fixture.root)}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(native_gpu, "_main_frames", return_value=iter((Fraction(i, 30), frame) for i, frame in enumerate(frames))) as decode, \
                mock.patch.object(native_gpu, "_asset_frames", side_effect=assets), \
                mock.patch.object(native_gpu, "verified_entry", return_value={}), \
                mock.patch.object(native_gpu.subprocess, "run") as mux:
            native_gpu.render(plan)
        decode.assert_called_once()
        self.assertEqual([args[0] for args in compositions], frames)
        self.assertTrue(all(len(args) == 8 for args in compositions))
        self.assertIn("#define SCENE_SOURCE_OVERLAY_SCALE 1.300000000f", compiled[0])
        self.assertIn("cover(src,w,h,overlay_u,overlay_v)", compiled[0])
        self.assertIn("-an", mux.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

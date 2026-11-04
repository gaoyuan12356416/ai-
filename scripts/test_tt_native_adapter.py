"""Frozen TT media contracts and native runner routing, without a GPU."""
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.tt_gpu import worker


class TTNativeAdapter(unittest.TestCase):
    def config(self,encoder="hevc_nvenc",mode=worker.RANDOM_OVERLAY_MEDIA_MODE):
        return SimpleNamespace(video_encoder=encoder,media_mode=mode,
                               compositor_backend=worker.BACKEND,ffmpeg_bin="/tt/ffmpeg")

    def test_native_contract_keeps_each_frozen_codec_and_audio_bitrate(self):
        info={"duration":3,"has_audio":True,"video":{"time_base":"1/15360"},
              "format":{"start_time":"0.25","duration":"3"}}
        recipe={"rotation_millidegrees":0,"scale_bp":10000,"tint_opacity_bp":1000}
        assets={"border":"b.png","opacity_video":"o.webm","corners":"c.webm","tint":"t.png"}
        with patch.dict(os.environ,{"RANDOM_GPU_NATIVE":"1","RANDOM_GPU_ASSET_CACHE_ROOT":"/verified-cache"}):
            for encoder,codec,rate,maximum,vbv in (("h264_nvenc","h264",1500000,2200000,3000000),
                                                  ("hevc_nvenc","hevc",900000,1350000,1800000)):
                command=worker.build_random_overlay_command(self.config(encoder),"source.mp4","output.mp4",info,2.5,recipe,assets)
                self.assertIn("features.random_gpu.native_pipeline",command)
                spec=json.loads(command[command.index("--spec")+1]);options=spec["encoder_options"]
                self.assertEqual((options["codec"],options["bitrate"],options["maxbitrate"],options["vbvbufsize"]),(codec,rate,maximum,vbv))
                self.assertEqual((options["preset"],options["tuning_info"],options["rc"],options["multipass"]),("P6","high_quality","vbr","fullres"))
                self.assertEqual((options["lookahead"],options["aq"],options["temporalaq"],options["bf"]),(32,8,1,3))
                self.assertEqual(spec["audio_bitrate"],128000)
                self.assertEqual(spec["ffmpeg"],"/tt/ffmpeg")
                self.assertEqual(spec["info"]["duration"],2.5)
                self.assertEqual(spec["info"]["format"],info["format"])
                self.assertEqual(spec["recipe"],recipe)
                self.assertNotIn("cq",options)
                self.assertNotIn("colorspace",options)

    def test_cpu_profile_never_switches_to_native(self):
        recipe={"rotation_millidegrees":0,"scale_bp":10000,"tint_opacity_bp":1000}
        assets={"border":"b.png","opacity_video":"o.webm","corners":"c.webm","tint":"t.png"}
        config=self.config("libx264");config.compositor_backend=worker.LEGACY
        with patch.dict(os.environ,{"RANDOM_GPU_NATIVE":"1"}):
            command=worker.build_random_overlay_command(config,"source.mp4","output.mp4",{"has_audio":True},2,recipe,assets)
        self.assertEqual([command[i+1] for i,value in enumerate(command) if value=="-c:v"][-1],"libx264")
        self.assertNotIn("features.random_gpu.native_pipeline",command)

    def test_native_runner_stays_inside_observed_runner(self):
        command=["python","-m","features.random_gpu.native_pipeline","--spec","{}","output.mp4"]
        calls=[]
        def observed(cmd,**kwargs):
            calls.append(cmd)
            return worker.media_runner(cmd,**kwargs)
        with patch("features.fb_gpu.render_process.run_render",return_value=subprocess.CompletedProcess(command,0)) as render:
            worker._run_command(observed,command,25,"render_failed")
        self.assertEqual(calls,[command])
        self.assertEqual(render.call_args.kwargs,{"capture_output":True,"text":True,"timeout":25,"check":False})

    def test_probe_runner_preserves_stdout_and_original_behavior(self):
        command=["ffprobe","-of","json","source.mp4"]
        result=subprocess.CompletedProcess(command,0,stdout="{}",stderr="")
        with patch.object(worker.subprocess,"run",return_value=result) as run:
            self.assertIs(worker.media_runner(command,stdout=subprocess.PIPE,text=True),result)
        run.assert_called_once_with(command,stdout=subprocess.PIPE,text=True)

    def test_probe_clock_metadata_survives(self):
        info=worker.inspect_input({"format":{"duration":"2","start_time":"0.125"},"streams":[
            {"codec_type":"video","width":720,"height":1280,"time_base":"1/15360"}]},60)
        self.assertEqual(info["format"]["start_time"],"0.125")
        self.assertEqual(info["video"]["time_base"],"1/15360")


if __name__=="__main__":unittest.main()

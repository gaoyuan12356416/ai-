import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
HTML = (HERE / "report.html").read_text(encoding="utf-8")


class FrontendChannelUnionTests(unittest.TestCase):
    def test_play_duration_stays_seconds_internally_and_displays_minutes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        scripts = re.findall(r"<script>(.*?)</script>", HTML, flags=re.DOTALL | re.IGNORECASE)
        self.assertEqual(len(scripts), 1)
        app = scripts[0].replace("bind();loadManifest().catch(showError);", "")
        scenario = r"""
selectedDims=[];
const grouped=aggregate([{
  play_total_seconds:1098.31,play_weight_installs:1
}],[],"conversion");
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
assert(grouped[0].avg_play_duration_seconds===1098.31,"internal duration must stay in seconds");
assert(formatValue("avg_play_duration_seconds",grouped[0].avg_play_duration_seconds)==="18.31 min","display duration must use minutes");
assert(exportLabel("avg_play_duration_seconds")==="平均游戏时长(min)","CSV duration header must declare minutes");
assert(exportValue("avg_play_duration_seconds",grouped[0].avg_play_duration_seconds)==="18.31","CSV duration value must use minutes");
console.log("duration_minutes=PASS");
"""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as handle:
            handle.write(app)
            handle.write(scenario)
            script = Path(handle.name)
        try:
            result = subprocess.run(
                [node, str(script)], capture_output=True, text=True, timeout=30
            )
        finally:
            script.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("duration_minutes=PASS", result.stdout)

    def test_channel_dimension_unions_both_facts_without_double_counting(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        scripts = re.findall(r"<script>(.*?)</script>", HTML, flags=re.DOTALL | re.IGNORECASE)
        self.assertEqual(len(scripts), 1)
        app = scripts[0].replace("bind();loadManifest().catch(showError);", "")
        scenario = r"""
selectedDims=["channel"];
const source=(channel,spend,installs)=>channelFact({
  dt:"2026-08-24",channel,mapping_status:"mapped",source_country:"AIG-WW",
  source_spend:spend,source_installs:installs,source_impressions:1000,
  source_clicks:100,source_row_count:1
},"delivery");
const manual=(channel,cost,installs,d1)=>channelFact({
  dt:"2026-08-24",channel,conversion_country:"US",manual_cost:cost,
  manual_installs:installs,d1_retained:d1,play_total_seconds:installs*10,
  play_weight_installs:installs,day0_revenue:0,day1_revenue:0,manual_row_count:1
},"conversion");
const facts=[
  source("googleadwords_int",100,25),
  source("tiktokglobal_int",20,5),
  manual("googleadwords_int",40,6,2),
  manual("tiktokglobal_int",19,4,1),
  manual("unityads_int",25,4,1),
  manual("organic",0,10,3),
  manual("restricted",0,2,1),
  manual("Facebook Ads",0,1,0)
];
const grouped=aggregate(facts,["channel"],"delivery");
const byChannel=Object.fromEntries(grouped.map(row=>[row.channel,row]));
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
assert(grouped.length===6,"expected all six channels");
assert(byChannel.googleadwords_int.source_spend===100,"google source spend changed");
assert(byChannel.googleadwords_int.manual_cost===40,"google manual cost missing");
assert(byChannel.googleadwords_int.effective_spend===100,"google effective spend double counted");
assert(byChannel.unityads_int.effective_spend===25,"unity fallback missing");
assert(byChannel.organic.effective_spend===0,"organic must not gain spend");
assert(grouped.reduce((sum,row)=>sum+row.source_row_count,0)===2,"source row count mismatch");
assert(grouped.reduce((sum,row)=>sum+row.manual_row_count,0)===6,"manual row count mismatch");
console.log("channel_union=PASS");
"""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as handle:
            handle.write(app)
            handle.write(scenario)
            script = Path(handle.name)
        try:
            result = subprocess.run(
                [node, str(script)], capture_output=True, text=True, timeout=30
            )
        finally:
            script.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("channel_union=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()

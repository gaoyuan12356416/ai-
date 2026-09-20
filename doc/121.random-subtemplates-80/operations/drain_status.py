import json,os,subprocess
from pathlib import Path
def run(args):return subprocess.check_output(args).decode().strip()
out={}
if Path('/data/drama-synthesis-gpu/current').exists():
    for unit in ['drama-synthesis-gpu-worker.service','tt-gpu-publisher.service','fb-page-random-overlay-gpu.service']:
        cg=run(['systemctl','show',unit,'-p','ControlGroup','--value'])
        rows=[]
        for pid in (Path('/sys/fs/cgroup/systemd')/cg.lstrip('/')/'cgroup.procs').read_text().split():
            row={'pid':pid,'comm':Path('/proc/'+pid+'/comm').read_text().strip()}
            if row['comm']=='ffmpeg':
                args=Path('/proc/'+pid+'/cmdline').read_bytes().decode().split('\0')
                row['output']=Path(args[-2]).name
                row['elapsed']=run(['ps','-p',pid,'-o','etime='])
                source=args[args.index('-i')+1]
                if source.startswith('/var/lib/fb-page-random-overlay/'):
                    data=json.loads(run(['/data/drama-synthesis-gpu/runtime/bin/ffprobe','-v','error','-show_entries','format=duration','-of','json',source]))
                    row['source_seconds']=float(data['format']['duration'])
                    row['output_megabytes']=round(Path(args[-2]).stat().st_size/1048576,1)
            rows.append(row)
        out[unit]=rows
else:
    for unit in ['tt-post-prepare.service','tt-post-runner.service','tt-auto-post-runner.service','fb-auto-post-prepare.service']:
        out[unit]=run(['systemctl','show',unit,'-p','ActiveState,SubState,MainPID'])
    out['fb_prepare_entry']=run(['systemctl','show','fb-auto-post-prepare.service','-p','ExecStart'])
print(json.dumps(out,indent=2))

"""Read-only PCM quality and synchronization checks for existing TT fixtures."""
from pathlib import Path
import argparse
import json
import subprocess
import numpy as np


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True)
    parser.add_argument('--folder',required=True);parser.add_argument('--duration',type=float,default=2)
    args=parser.parse_args();folder=Path(args.folder)
    ffmpeg='/data/pipeline-dispatch/tools/tt/ffmpeg'
    def pcm(path,source=False):
        command=[ffmpeg,'-v','error','-i',str(path),'-vn']
        if source:command+=['-af','aresample=48000:async=1:first_pts=0,apad','-t',str(args.duration)]
        command+=['-ar','48000','-ac','2','-f','f32le','pipe:1']
        return np.frombuffer(subprocess.check_output(command),dtype='<f4').reshape(-1,2).astype(np.float64)
    reference=pcm(args.source,True);rows={}
    for label in ('baseline','candidate'):
        decoded=pcm(folder/(label+'.mp4'));n=min(len(reference),len(decoded));a=reference[:n];b=decoded[:n]
        error=a-b;power=np.mean(a*a);mse=np.mean(error*error)
        # FFT correlation measures sample displacement over the whole clip;
        # a separate zero-lag waveform score cannot hide an inserted delay.
        x=a.mean(axis=1);y=b.mean(axis=1);length=1<<(2*n-1).bit_length()
        corr=np.fft.irfft(np.fft.rfft(x,length)*np.conj(np.fft.rfft(y,length)),length)
        peak=int(np.argmax(corr));lag=peak if peak<n else peak-length
        rows[label]={'decoded_samples':len(decoded),'effective_samples':n,
            'snr_vs_source_pcm_db':float(10*np.log10((power+1e-30)/(mse+1e-30))),
            'correlation_lag_samples':lag,'max_abs_error':float(np.max(np.abs(error))),
            'head_100ms_error_rms':float(np.sqrt(np.mean(error[:4800]**2))),
            'tail_100ms_error_rms':float(np.sqrt(np.mean(error[-4800:]**2)))}
    delta=rows['candidate']['snr_vs_source_pcm_db']-rows['baseline']['snr_vs_source_pcm_db']
    result={'scope':'finite same-source waveform regression; not subjective listening proof',
            'checks':rows,'candidate_minus_baseline_snr_db':delta,
            'passed':delta>=-.5 and rows['baseline']['correlation_lag_samples']==rows['candidate']['correlation_lag_samples']==0
                     and rows['baseline']['decoded_samples']==rows['candidate']['decoded_samples']}
    (folder/'audio-quality.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

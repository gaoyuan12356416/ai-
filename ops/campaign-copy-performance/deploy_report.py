"""Deploy the exact pushed report revision; rollback code without old cache data."""
import argparse,json,os,shutil,subprocess
from pathlib import Path

def run(*args):return subprocess.check_output(args,text=True).strip()
def main():
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['apply','rollback']);parser.add_argument('--evidence-dir',type=Path,required=True);args=parser.parse_args()
 root=args.evidence_dir.resolve();manifest=root/'deployment-final.json';link=Path('/opt/campaign-copy-performance/current')
 if args.action=='rollback':
  saved=json.loads(manifest.read_text());target=Path(saved['previous'])
 else:
  source=Path(__file__).resolve().parents[2];commit=run('git','-C',str(source),'rev-parse','HEAD')
  assert not run('git','-C',str(source),'status','--porcelain')
  assert run('git','-C',str(source),'branch','-r','--contains',commit)
  assert run('findmnt','-rn','-o','TARGET','--mountpoint','/mnt/data-disk')=='/mnt/data-disk'
  # Dependency and source preflight precede the narrow service restart.
  import mysql.connector
  assert mysql.connector.HAVE_CEXT
  target=Path('/mnt/data-disk/campaign-copy-performance/releases')/commit
  shutil.copytree(source/'ops/campaign-copy-performance',target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  subprocess.run(['python3',str(target/'service.py'),'--self-test'],check=True)
  root.mkdir(exist_ok=True);backup=root/'backup';backup.mkdir(exist_ok=True)
  shutil.copy2('/mnt/data-disk/campaign-copy-performance/cache/report.json',backup/'report.before-final.json')
  saved=dict(commit=commit,previous=str(link.resolve()),release=str(target))
  manifest.write_text(json.dumps(saved,indent=2))
 nextlink=link.with_name('current.report-next');nextlink.symlink_to(target);os.replace(nextlink,link)
 subprocess.run(['systemctl','restart','campaign-copy-performance.service'],check=True)
 print(json.dumps(saved))
if __name__=='__main__':main()

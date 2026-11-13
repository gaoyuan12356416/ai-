#!/usr/bin/env python3
"""Import/verify catalogs or refresh from an explicitly supplied read-only query."""
import argparse
import datetime as dt
import json
from pathlib import Path

from consumer import validate_legacy
from store import activate, import_json, refresh_source


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='/root/.codex/plugins/feishu/config.json')
    commands=parser.add_subparsers(dest='command',required=True)
    for name,argument in [('import-json','path'),('refresh-source','source_sql')]:
        command=commands.add_parser(name);command.add_argument(argument)
        command.add_argument('--generated-at',required=True);command.add_argument('--activate')
    command=commands.add_parser('validate');command.add_argument('workdir');command.add_argument('generation')
    command=commands.add_parser('activate');command.add_argument('report_key');command.add_argument('generation')
    args=parser.parse_args()
    if args.command=='validate':result=validate_legacy(args.workdir,args.config,args.generation)
    elif args.command=='activate':
        activate(args.config,args.report_key,args.generation);result={'activated':args.generation,'report_key':args.report_key}
    else:
        result=import_json(args.config,args.path,args.generated_at) if args.command=='import-json' else refresh_source(args.config,args.source_sql,args.generated_at)
        if args.activate:activate(args.config,args.activate,result['generation_id'])
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

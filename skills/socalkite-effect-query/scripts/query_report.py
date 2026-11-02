#!/usr/bin/env python3
"""Read the published SocialKit report through the saved CPU SSH helper."""
import argparse
import datetime
import shlex
import subprocess
import sys
from pathlib import Path


def iso_date(value):
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use YYYY-MM-DD') from exc


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start', type=iso_date)
    p.add_argument('--end', type=iso_date)
    p.add_argument('--view', choices=('overview', 'effects', 'publications'), default='overview')
    p.add_argument('--group-by')
    p.add_argument('--filter', action='append', default=[], metavar='KEY=VALUE')
    p.add_argument('--format', choices=('json', 'csv'), default='json')
    p.add_argument('--version')
    p.add_argument('--page', type=int, default=1)
    p.add_argument('--page-size', type=int, choices=(20, 50, 100), default=50)
    p.add_argument('--ssh-helper', type=Path, default=Path.home() / 'codex-personal-skills' / 'skills' / 'server-43-166-187-96' / 'scripts' / 'run_ssh.py')
    p.add_argument('--data-root', default='/mnt/data-disk/socialkit-analytics')
    p.add_argument('--code-root', default='/root/drama_material_service')
    return p


def remote_command(args):
    command = ['python3', '-m', 'features.socialkit_analytics.query', '--data-root', args.data_root,
               '--view', args.view, '--format', args.format, '--page', str(args.page),
               '--page-size', str(args.page_size)]
    for name in ('start', 'end', 'group_by', 'version'):
        value = getattr(args, name)
        if value is not None:
            command.extend(['--' + name.replace('_', '-'), value])
    for value in args.filter:
        command.extend(['--filter', value])
    return 'cd ' + shlex.quote(args.code_root) + ' && ' + ' '.join(shlex.quote(v) for v in command)


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if bool(args.start) != bool(args.end):
        p.error('--start and --end must be supplied together')
    if args.start and args.start > args.end:
        p.error('--start must not be later than --end')
    if args.page < 1:
        p.error('--page must be positive')
    for value in args.filter:
        if '=' not in value or not value.split('=', 1)[0]:
            p.error('--filter expects KEY=VALUE')
    if not args.ssh_helper.is_file():
        p.error('Saved CPU SSH helper was not found; use --ssh-helper with its installed path')
    # Values are shell quoted once. Credentials remain inside the saved helper.
    result = subprocess.run([sys.executable, str(args.ssh_helper), '--stdin', '--timeout', '60'],
                            input=remote_command(args) + '\n', text=True, encoding='utf-8')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())

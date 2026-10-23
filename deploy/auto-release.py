import base64
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

def call(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=60)

def aws(*args):
    p = call(['aws', *args, '--region', 'ap-northeast-2',
              '--output', 'json', '--no-cli-pager'])
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)

image = os.environ['PUBLISHED_IMAGE']
pattern = r'527673188566\.dkr\.ecr\.ap-northeast-2\.amazonaws\.com/schutz/management-worker:(sha-([0-9a-f]{40})-[0-9]+-[0-9]+)'
match = re.fullmatch(pattern, image)
if not match or match.group(2) != os.environ['GITHUB_SHA']:
    raise SystemExit('INVALID_CI_IMAGE')

head = call([
    'gh', 'api',
    'repos/' + os.environ['GITHUB_REPOSITORY'] + '/git/ref/heads/main',
    '--jq', '.object.sha'
])
if head.returncode or head.stdout.strip() != os.environ['GITHUB_SHA']:
    raise SystemExit('OUTDATED_MAIN_RUN: deployment stopped')

encoded = base64.b64encode(
    Path('deploy/service-release.sh').read_bytes()
).decode()
command = (
    'printf %s ' + shlex.quote(encoded) +
    ' | base64 -d | bash -s -- ' + shlex.quote(match.group(1)) + ' management-worker'
)
params = {
    'commands': ['set -eu', command],
    'executionTimeout': ['1200']
}
instance_id = 'i-066c8eee143b5cdff'
result = aws(
    'ssm', 'send-command',
    '--instance-ids', instance_id,
    '--document-name', 'AWS-RunShellScript',
    '--parameters', json.dumps(params)
)
command_id = result['Command']['CommandId']
print('SSM_COMMAND_ID=' + command_id, flush=True)

with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write(
        '\nImage: `' + image +
        '`\nSSM command: `' + command_id + '`\n'
    )

deadline = time.monotonic() + 1260
while time.monotonic() < deadline:
    p = call([
        'aws', 'ssm', 'get-command-invocation',
        '--command-id', command_id,
        '--instance-id', instance_id,
        '--region', 'ap-northeast-2',
        '--output', 'json', '--no-cli-pager'
    ])
    if p.returncode:
        if 'InvocationDoesNotExist' not in p.stderr:
            raise SystemExit(p.stderr)
    else:
        data = json.loads(p.stdout)
        status = data['Status']
        print('SSM_STATUS=' + status, flush=True)
        if status not in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
            output = data.get('StandardOutputContent', '')
            print(output)
            print(data.get('StandardErrorContent', ''))
            ok = (
                status == 'Success' and data['ResponseCode'] == 0 and
                ('MANAGEMENT_SERVICE_RELEASE_SUCCESS' in output or
                 'ALREADY_DEPLOYED' in output)
            )
            raise SystemExit(0 if ok else 1)
    time.sleep(10)

raise SystemExit(
    'SSM_RESULT_TIMEOUT: inspect ' + command_id + ' before retrying'
)

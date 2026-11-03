#!/usr/bin/env python3
"""Run on the Mac using its existing SSH aliases; read-only on both hosts.

Verify native sample IDs in ntfy's SQLite message cache without reading credentials.
No VPS configuration, resource agent, networking or monitor policy is changed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PI_CHECK = r'''
import json,time,subprocess
from pathlib import Path

def state(unit,property):
    return subprocess.check_output(['systemctl','show',unit,'--property='+property,'--value'],text=True).strip()
try:
    proof=json.loads(Path('/var/lib/piwatch-ntfy-native/evidence.json').read_text())
    heartbeat=json.loads(Path('/run/piwatch-heartbeat/status.json').read_text())
    assert proof['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'Proof belongs to an earlier Pi boot; rerun an observation before accepting it'
    assert state('piwatch-ntfy','ActiveState')=='inactive','Legacy publisher is active'
    assert state('piwatch-ntfy','UnitFileState')=='disabled','Legacy bridge can restart on reboot'
    assert state('piwatch-heartbeat','ActiveState')=='active','Native heartbeat is not running'
    assert state('piwatch-heartbeat','UnitFileState')=='enabled','Native heartbeat is not enabled at boot'
    assert heartbeat['state']=='UP' and heartbeat['piwatch_fresh'] is True,'Native monitor evidence is stale or unavailable'
    assert time.time()-heartbeat['last_accepted_at']<=3 and not heartbeat.get('failed_samples'),'Recent heartbeat acceptance failed'
    assert len(proof['samples'])>=80 and len({sample['id'] for sample in proof['samples']})>=80,'Eighty distinct native samples are required'
    assert proof['samples'][0]['accepted_at']>proof['stopped_bridge_at'],'Parallel bridge samples cannot qualify'
    assert proof['samples'][-1]['accepted_at']-proof['samples'][0]['accepted_at']>=78,'Observation did not cover the burst-limit window'
    proof['current_heartbeat_healthy']=True
    print(json.dumps({'ok':True,'proof':proof}))
except Exception as exc:
    print(json.dumps({'ok':False,'reason':str(exc) if isinstance(exc,AssertionError) else type(exc).__name__}))
'''

VPS_CHECK = r'''
import ast,hashlib,json,re,sqlite3,subprocess,time
from pathlib import Path
from contextlib import closing

def property(unit,name):
    return subprocess.check_output(['systemctl','show',unit,'--property='+name,'--value'],text=True).strip()

def main(proof):
    try:
        for unit in ('ntfy-serverwatch','serverwatch-monitor'):
            assert property(unit,'ActiveState')=='active',unit+' is not active'
        pid=property('ntfy-serverwatch','MainPID')
        args=Path('/proc/'+pid+'/cmdline').read_bytes().split(bytes([0]))
        args=[arg.decode() for arg in args if arg]
        wanted={'NTFY_CONFIG_FILE','NTFY_CACHE_FILE','NTFY_VISITOR_REQUEST_LIMIT_BURST','NTFY_VISITOR_REQUEST_LIMIT_REPLENISH','NTFY_VISITOR_REQUEST_LIMIT_EXEMPT_HOSTS'}
        env={}
        for entry in Path('/proc/'+pid+'/environ').read_bytes().split(bytes([0])):
            key,sep,value=entry.partition(b'=')
            if sep and key.decode(errors='replace') in wanted:env[key.decode()]=value.decode()
        def argument(flag):
            for index,value in enumerate(args):
                if value==flag and index+1<len(args):return args[index+1]
                if value.startswith(flag+'='):return value.split('=',1)[1]
            return None
        config=Path(argument('--config') or argument('-c') or env.get('NTFY_CONFIG_FILE','/etc/ntfy/server.yml'))
        selected={}
        keys={'cache-file','visitor-request-limit-burst','visitor-request-limit-replenish','visitor-request-limit-exempt-hosts'}
        if config.is_file():
            for line in config.read_text().splitlines():
                if not line or line[0].isspace() or line.lstrip().startswith('#'):continue
                key,sep,value=line.partition(':')
                if sep and key in keys:selected[key]=value.split(' #',1)[0].strip().strip(chr(34)+chr(39))
        def option(key):return argument('--'+key) or env.get('NTFY_'+key.upper().replace('-','_')) or selected.get(key)
        cache=option('cache-file') or argument('-C')
        assert cache,'ntfy cache is not on disk; historical samples cannot be verified by this script'
        cache=Path(cache)
        if not cache.is_absolute():cache=Path(property('ntfy-serverwatch','WorkingDirectory'))/cache
        assert option('visitor-request-limit-burst')=='120','Unexpected ntfy burst limit; inspect without changing it'
        assert option('visitor-request-limit-replenish')=='250ms','Unexpected ntfy replenish interval'
        exempt=option('visitor-request-limit-exempt-hosts') or ''
        assert '127.0.0.1' in exempt and '::1' in exempt,'Local monitor is not exempt from request limits'
        with closing(sqlite3.connect(cache.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            columns={row[1] for row in db.execute('PRAGMA table_info(messages)')}
            identifier=next((name for name in ('mid','message_id','id') if name in columns),None)
            assert identifier and {'time','topic','message'}<=columns,'Unexpected ntfy cache schema'
            # Identifier is selected from constants, not interpolated from input.
            rows=db.execute('SELECT '+identifier+',time,message FROM messages WHERE topic=? ORDER BY time DESC,rowid DESC LIMIT 300',
                            (proof['heartbeat_topic'],)).fetchall()
            by_id={row[0]:row for row in rows}
            ids={sample['id'] for sample in proof['samples']}
            placeholders=','.join('?' for _ in ids)
            historical=db.execute('SELECT '+identifier+',time,message FROM messages WHERE topic=? AND '+identifier+' IN ('+placeholders+')',
                                  (proof['heartbeat_topic'],*ids)).fetchall()
        def fresh_body(message):
            body=json.loads(message)
            if isinstance(body,str):body=json.loads(body)
            return isinstance(body,dict) and body.get('piwatch_fresh') is True
        assert len(historical)==len(ids),'Some native accepted IDs are absent from the VPS cache'
        accepted={sample['id']:sample['accepted_at'] for sample in proof['samples']}
        for message_id,stamp,message in historical:
            assert fresh_body(message),'A native sample contains false/malformed freshness evidence'
            assert abs(stamp-accepted[message_id])<=3,'Pi/VPS timestamps differ beyond the consumer tolerance'
        assert rows and -1<=time.time()-rows[0][1]<=3 and fresh_body(rows[0][2]),'Latest VPS heartbeat is stale or invalid'
        monitor_pid=property('serverwatch-monitor','MainPID')
        monitor_binary=Path('/proc/'+monitor_pid+'/exe').resolve()
        monitor_hash=hashlib.sha256(monitor_binary.read_bytes()).hexdigest()
        journal=subprocess.run(['journalctl','-u','serverwatch-monitor','--since','@'+str(int(proof['samples'][0]['accepted_at'])),
                                '--output=json','--no-pager'],capture_output=True,text=True)
        assert journal.returncode==0,'Cannot inspect VPS monitor diagnostics'
        suspect=0
        for line in journal.stdout.splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            message=entry.get('MESSAGE','')
            if isinstance(message,str) and (int(entry.get('PRIORITY',6))<=3 or re.search(r'(?i)heartbeat.*(?:missing|unavailable|stale)|HTTP.?429|pi.*(?:offline|unreachable)',message)):
                suspect+=1
        assert suspect==0,'VPS monitor logged possible heartbeat failures; inspect locally before accepting cutover'
        print(json.dumps({'ok':True,'native_samples_verified':len(historical),'latest_age_seconds':round(time.time()-rows[0][1],2),
                          'rate_limits_preserved':True,'monitor_binary_sha256':monitor_hash,
                          'phone_receipt_confirmed':proof.get('phone_receipt_confirmed',False)}))
    except Exception as exc:
        print(json.dumps({'ok':False,'reason':str(exc) if isinstance(exc,AssertionError) else type(exc).__name__}))
'''


def remote(host, code):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             host, 'python3', '-'], input=code, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('SSH/read-only check failed for '+host+'; inspect access in your terminal')
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('Unexpected verification output from '+host) from None
    if not value.get('ok'):
        raise RuntimeError(host+': '+value.get('reason','verification failed'))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi', default='watchdog-pi')
    parser.add_argument('--vps', default='bj-vps')
    args = parser.parse_args()
    for host in (args.pi,args.vps):
        if host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@-]+',host):parser.error('Use an SSH alias or ordinary user@host')
    try:
        pi=remote(args.pi, PI_CHECK)
        code=VPS_CHECK+'\nmain(json.loads('+repr(json.dumps(pi['proof']))+'))\n'
        vps=remote(args.vps,code)
        print('PASS: native Pi publisher only; boot enablement verified; '+str(vps['native_samples_verified'])+' distinct fresh native samples verified on VPS.')
        print('PASS: current heartbeat fresh, VPS monitor active, rate limits preserved, no matching failure diagnostics.')
        print('Deployed VPS monitor SHA256: '+vps['monitor_binary_sha256'])
        print('Phone receipt: '+('confirmed by user' if vps['phone_receipt_confirmed'] else 'still needs your confirmation'))
    except RuntimeError as exc:
        print('Verification incomplete: '+str(exc),file=sys.stderr)
        print('The VPS SSH account needs read access to its own ntfy cache/config and monitor journal. No settings were changed.',file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

import sys
import os
import signal
import select
import time
import argparse
from subprocess import Popen, PIPE

python = ['python3']

def single_test (config, client_conf=None):
    client_args = python + ['shadowsocks/local.py', '-v']
    if client_conf:
        client_args.extend(['-c', client_conf])
    p1 = Popen(client_args, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
    p3 = None
    p4 = None
    stage = 1

    try:
        local_ready = False
        fdset = [p1.stdout, p1.stderr]
        while True:
            r, w, e = select.select(fdset, [], fdset)
            if e:
                break

            for fd in r:
                line = fd.readline()
                if not line:
                    if stage == 2 and fd == p3.stdout:
                        stage = 3
                    if stage == 4 and fd == p4.stdout:
                        stage = 5
                if bytes != str:
                    line = str(line, 'utf8')
                sys.stderr.write(line)
                if line.find('starting local') >= 0:
                    local_ready = True
            
            if stage == 1 and local_ready:
                time.sleep(1)
                p3 = Popen(['curl', 'https://api.ipify.org?format=json',
                        '-x', 'socks5h://localhost:8088',
                        '-m', '15', '--connect-timeout', '10'],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
                if p3 is not None:
                    fdset.append(p3.stdout)
                    fdset.append(p3.stderr)
                    stage = 2
                else:
                    sys.exit(1)      

            if stage == 3 and p3 is not None:
                fdset.remove(p3.stdout)
                fdset.remove(p3.stderr)
                r = p3.wait()
                if config.should_fail:
                    if r == 0:
                        sys.exit(1)
                else:
                    if r != 0:
                        sys.exit(1)
                p4 = Popen(['curl', 'http://ip-api.com/json',
                            '-x', 'socks5h://localhost:8088',
                            '-m', '15', '--connect-timeout', '10'],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
                if p4 is not None:
                    fdset.append(p4.stdout)
                    fdset.append(p4.stderr)
                    stage = 4
                else:
                    sys.exit(1)  

            if stage == 5:
                r = p4.wait()
                if config.should_fail:
                    if r == 0:
                        sys.exit(1)
                    print('test passed (expecting failure)')
                else:
                    if r != 0:
                        sys.exit(1)
                    print('test passed')
                break
    finally:
        for p in [p1]:
            try:
                os.kill(p.pid, signal.SIGINT)
                os.waitpid(p.pid, 0)
            except OSError:
                pass

def main():
    parser = argparse.ArgumentParser(description='test local')
    parser.add_argument('-c', '--client-conf', type=str, default=None)
    parser.add_argument('--should-fail', action='store_true', default=None)
    parser.add_argument('--tcp-only', action='store_true', default=None)
    config = parser.parse_args()
    single_test(config, config.client_conf)

if __name__ == '__main__':
    main()

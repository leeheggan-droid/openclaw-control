import json
import subprocess

WRAPPER_DIR = "/opt/control/bin"

def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def status(service_id):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-status", service_id])
    if rc != 0:
        raise RuntimeError(err or out)
    return json.loads(out)

def start(service_id):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-start", service_id])
    if rc != 0:
        raise RuntimeError(err or out)
    return json.loads(out)

def stop(service_id):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-stop", service_id])
    if rc != 0:
        raise RuntimeError(err or out)
    return json.loads(out)

def restart(service_id):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-restart", service_id])
    if rc != 0:
        raise RuntimeError(err or out)
    return json.loads(out)

def run_once(service_id):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-run", service_id])
    if rc != 0:
        raise RuntimeError(err or out)
    return json.loads(out)

def logs(service_id, lines=200):
    rc, out, err = _run([f"{WRAPPER_DIR}/svc-logs", service_id, str(lines)])
    if rc != 0:
        raise RuntimeError(err or out)
    return out

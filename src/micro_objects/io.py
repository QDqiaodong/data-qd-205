import json
import os
from pathlib import Path
import tempfile

def pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError(f'duplicate JSON key: {key}')
        result[key]=value
    return result

def constant(value):raise ValueError('nonfinite JSON value')

def load(path):
    with open(path,'rb') as f:raw=f.read(8000001)
    if len(raw)>8000000:raise ValueError('input over 8MB')
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)

def publish(path,result):
    payload=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.publish-',delete=False) as f:
            temporary=Path(f.name);f.write(payload);f.flush();os.fsync(f.fileno())
        os.link(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def save_delta(path,payload):
    """Atomically write a self-contained increment document as canonical JSON."""
    from .delta import canonical_bytes
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.delta-',delete=False) as f:
            temporary=Path(f.name);f.write(canonical_bytes(payload)+b'\n');f.flush();os.fsync(f.fileno())
        os.replace(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def load_delta(path):
    """Read an increment document; content validation happens in parse_delta."""
    with open(path,'rb') as f:raw=f.read(8000001)
    if len(raw)>8000000:raise ValueError('delta over 8MB')
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)

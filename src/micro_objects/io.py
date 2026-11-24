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


def save_calibration(path, record):
    """Atomically write a self-contained calibration record as canonical JSON."""
    from .dataset import parse_calibration
    payload = parse_calibration(record)
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.calibration-',delete=False) as f:
            temporary=Path(f.name);f.write((json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8'));f.flush();os.fsync(f.fileno())
        os.replace(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def load_calibration(path):
    """Read a calibration record; content validation happens in parse_calibration."""
    with open(path,'rb') as f:raw=f.read(8000001)
    if len(raw)>8000000:raise ValueError('calibration over 8MB')
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)


def _save_canonical(path, payload):
    from .delta import canonical_bytes
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.delivery-',delete=False) as f:
            temporary=Path(f.name);f.write(canonical_bytes(payload)+b'\n');f.flush();os.fsync(f.fileno())
        os.replace(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def save_grade_delivery(path, document):
    """Atomically write the consumable grade table as canonical JSON."""
    from .correspondence import parse_grade_delivery
    _save_canonical(path, parse_grade_delivery(document))


def save_group_delivery(path, document):
    """Atomically write the consumable group-total table as canonical JSON."""
    from .correspondence import parse_group_delivery
    _save_canonical(path, parse_group_delivery(document))


def save_region_delivery(path, delivery):
    """Atomically write a verified region batch delivery as canonical JSON."""
    from .region import verify_region_delivery
    verify_region_delivery(delivery, raise_on_error=True)
    _save_canonical(path, delivery)


def load_region_delivery(path):
    """Read and conservation-check a saved region batch delivery."""
    from .region import verify_region_delivery
    document = _load_delivery(path)
    if not verify_region_delivery(document):
        raise ValueError('invalid region delivery conservation totals')
    return document


def _load_delivery(path):
    with open(path,'rb') as f:raw=f.read(8000001)
    if len(raw)>8000000:raise ValueError('delivery document over 8MB')
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)


def load_grade_delivery(path):
    """Read and strictly validate a grade table without masks or evidence."""
    from .correspondence import parse_grade_delivery
    return parse_grade_delivery(_load_delivery(path))


def load_group_delivery(path):
    """Read and strictly validate a group-total table without masks or evidence."""
    from .correspondence import parse_group_delivery
    return parse_group_delivery(_load_delivery(path))

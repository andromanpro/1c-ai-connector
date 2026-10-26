"""Archive old successful wire evidence, retaining receipts and tool continuations."""
import hashlib
import json
from pathlib import Path
import time
import uuid


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def maintain_receipts(store, older_than_days=90, apply_plan_sha256=None, now=None):
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 1:
        raise ValueError('Receipt archive age must be a positive whole number of days')
    directory = Path(store.directory).resolve(strict=True)
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    items = []
    inventory = {'request_receipts': 0, 'unknown_or_incomplete': 0, 'raw_files': 0, 'raw_bytes': 0}
    for path in sorted(directory.glob('request-*.dpapi')):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Receipt must be a regular owned file')
        request_id = path.stem.removeprefix('request-')
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError('Receipt filename must contain a canonical request UUID')
        receipt = store.read(path.stem)
        inventory['request_receipts'] += 1
        complete = receipt.get('outcome') == 'received' and bool(receipt.get('completion'))
        if not complete:
            inventory['unknown_or_incomplete'] += 1
        for name in ('upstream-' + request_id + '.dpapi', 'wire-' + request_id + '.events.dpapi'):
            raw = directory / name
            if not raw.exists():
                continue
            if raw.is_symlink() or not raw.is_file():
                raise ValueError('Wire evidence must be a regular owned file')
            stat = raw.stat()
            inventory['raw_files'] += 1
            inventory['raw_bytes'] += stat.st_size
            if complete and stat.st_mtime <= cutoff:
                items.append({'request_id': request_id, 'file': name, 'bytes': stat.st_size,
                    'sha256': file_digest(raw), 'receipt_sha256': file_digest(path)})
    plan = {'schema_version': 1, 'older_than_days': older_than_days, 'files': items}
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode('utf-8')).hexdigest()
    result = {'plan': plan, 'plan_sha256': plan_hash, 'inventory': inventory,
              'archive_files': len(items), 'archive_bytes': sum(item['bytes'] for item in items),
              'moved': 0, 'deleted': 0, 'receipts_changed': 0}
    if apply_plan_sha256 is None:
        return result
    if apply_plan_sha256 != plan_hash:
        raise ValueError('Archive plan changed; inspect a fresh report before applying')
    archive = directory / 'raw-archive'
    if archive.is_symlink() or (archive.exists() and not archive.is_dir()):
        raise ValueError('Archive must be an owned directory')
    targets = [(directory / item['file'], archive / item['file']) for item in items]
    if any(target.exists() for _, target in targets):
        raise ValueError('Archive already contains an evidence file; nothing was overwritten')
    if targets:
        archive.mkdir(exist_ok=True)
    for source, target in targets:
        # Rename within the protected store; Windows refuses an existing target.
        # An interrupted run can be resumed by inspecting the remaining plan.
        source.rename(target)
        result['moved'] += 1
    return result

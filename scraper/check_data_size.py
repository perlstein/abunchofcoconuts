"""Fail before committing files that are approaching GitHub's 100 MiB limit."""
from pathlib import Path


def check(root):
    oversized = [str(p.relative_to(root)) for p in root.rglob('*.json')
                 if p.stat().st_size >= 95 * 1024 * 1024]
    if oversized:
        raise ValueError('Data files need splitting before publication: ' + ', '.join(oversized))


if __name__ == '__main__':
    check(Path(__file__).resolve().parent.parent / 'data')

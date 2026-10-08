"""Check the Git publication manifest, not private files ignored by Git."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES={'.db','.sqlite','.sqlite3','.pem','.key','.p12','.wav','.mp3','.m4a','.mp4','.zip','.gz','.log'}
PATTERNS={
    'private key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'OpenAI key':r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}',
    'GitHub token':r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
    'Telegram token':r'\b\d{6,12}:[A-Za-z0-9_-]{30,}',
    'Twilio account ID':r'\bAC[0-9a-fA-F]{32}\b',
    'personal path':r'/Users/(?!example\b)[^/\s]+/|C:\\\\Users\\\\',
    'production hosting':r'https?://(?:kspa|footprint)[\w.-]*\.up\.railway\.app',
}


def main():
    result=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,capture_output=True,check=True)
    files=sorted(set(filter(None,result.stdout.decode().split('\0'))))
    findings=[]
    for name in files:
        path=ROOT/name
        if path.is_symlink():
            findings.append((name,'symlink not allowed in public snapshot'));continue
        if not path.is_file():continue
        if path.suffix in FORBIDDEN_SUFFIXES or (path.name.startswith('.env') and path.name!='.env.example') or any(p in {'data','reports','private'} for p in path.relative_to(ROOT).parts):
            findings.append((name,'private artifact filename'));continue
        if path.suffix in {'.pyc','.pyo'}:
            findings.append((name,'compiled Python artifact'));continue
        try:text=path.read_text()
        except UnicodeDecodeError:
            findings.append((name,'unreviewed binary'));continue
        if name=='scripts/check_public_files.py':continue
        for label,pattern in PATTERNS.items():
            if re.search(pattern,text):findings.append((name,label))
        # NANP examples use the reserved 202-555-01xx block.
        for match in re.finditer(r'(?<!\d)\+1\d{10}(?!\d)',text):
            if not re.fullmatch(r'\+120255501\d{2}',match[0]):findings.append((name,'non-example phone number'))
    if findings:
        for name,kind in findings:print(f'{name}: {kind}')
        return 1
    print(f'Public-file checks passed for {len(files)} files. Run a dedicated secret scanner as well.')
    return 0


if __name__=='__main__':sys.exit(main())

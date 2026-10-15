"""Apply the optional read-only WDA geometry extension to a local WDA checkout."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    parser.add_argument('--check', action='store_true', help='Validate without editing')
    args = parser.parse_args()
    patch = Path(__file__).parent / 'wda' / 'native_geometry.patch'
    root = args.checkout.resolve()
    if not (root / 'WebDriverAgentLib/Commands/FBDebugCommands.m').is_file():
        parser.error('Expected a WebDriverAgent checkout')
    command = ['git', 'apply', '--check', str(patch)]
    checked = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if checked.returncode:
        reverse = subprocess.run(['git','apply','--reverse','--check',str(patch)],
                                 cwd=root, capture_output=True, text=True)
        if reverse.returncode == 0:
            print('WDA native geometry extension already applied.')
            return
        raise SystemExit('Patch does not match this WDA source; no files changed.\n'+checked.stderr)
    if args.check:
        print('WDA native geometry patch can be applied; no files changed.')
        return
    subprocess.run(['git','apply',str(patch)], cwd=root, check=True)
    print('Read-only WDA geometry extension applied. Build and sign WDA normally; SDK does not install it.')


if __name__ == '__main__':
    main()

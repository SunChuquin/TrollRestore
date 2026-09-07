import subprocess
import time
import sys

pm = r'c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe'
trollstore_url = 'apple-magnifier://install?url=https://github.com/SunChuquin/Kline/releases/download/trollstore-temp-20260904105902/Kline.ipa'

# Step 1: Launch example.com with proper stdin handling
print('Step 1: Launching example.com...')
proc = subprocess.Popen(
    [pm, 'webinspector', 'launch', '--timeout', '5', 'https://example.com'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# Wait for the page to load
time.sleep(5)

# Read any available output
# Just send ENTER and kill
try:
    proc.communicate(input='\n', timeout=3)
except:
    proc.kill()
    proc.communicate()
print('Step 1 done. example.com should be loaded.')

# Step 2: Use js-shell with --automation to run JavaScript
print('Step 2: Running js-shell with --automation...')
js_code = f"window.location.href='{trollstore_url}'"
print(f'JS: {js_code}')

proc2 = subprocess.Popen(
    [pm, 'webinspector', 'js-shell', '--automation', '--url', 'https://example.com', '--timeout', '10'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# Send the JavaScript command and exit
try:
    stdout, stderr = proc2.communicate(
        input=js_code + '\nexit()\n',
        timeout=15
    )
    print(f'STDOUT: {stdout}')
    print(f'STDERR: {stderr}')
    print(f'Return code: {proc2.returncode}')
except subprocess.TimeoutExpired:
    print('js-shell timed out, killing...')
    proc2.kill()
    stdout, stderr = proc2.communicate()
    print(f'STDOUT: {stdout}')
    print(f'STDERR: {stderr}')
except Exception as e:
    print(f'Error: {e}')
    proc2.kill()
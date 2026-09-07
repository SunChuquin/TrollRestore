import subprocess
import sys
import time

pm = r'c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe'
url = 'apple-magnifier://install?url=https://github.com/SunChuquin/Kline/releases/download/trollstore-temp-20260904105902/Kline.ipa'

# Try using js-shell approach: first launch example.com, then run JS
# Step 1: Launch example.com in background
print('Launching example.com...')
proc = subprocess.Popen(
    [pm, 'webinspector', 'launch', 'https://example.com'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# Wait for the page to load
time.sleep(5)

# Step 2: Send ENTER to close the launch command
try:
    proc.stdin.write('\n')
    proc.stdin.flush()
except:
    pass

# Kill the process (URL already sent)
proc.kill()
print('example.com launched. Now trying js-shell...')

# Step 3: Use js-shell to execute JavaScript
print('Running js-shell with redirect...')
js_cmd = f"window.location.href='{url}'"
proc2 = subprocess.Popen(
    [pm, 'webinspector', 'js-shell'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# Send the JavaScript command
try:
    proc2.stdin.write(js_cmd + '\n')
    proc2.stdin.flush()
    time.sleep(2)
    proc2.stdin.write('exit()\n')
    proc2.stdin.flush()
except Exception as e:
    print(f'Error: {e}')

# Wait for output
try:
    stdout, stderr = proc2.communicate(timeout=10)
    print(f'STDOUT: {stdout}')
    print(f'STDERR: {stderr}')
except:
    proc2.kill()
    print('js-shell timed out')
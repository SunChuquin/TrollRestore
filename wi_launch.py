import sys
import time
import threading
from pymobiledevice3.webinspector import WebInspectorService

url = 'apple-magnifier://install?url=https://github.com/SunChuquin/Kline/releases/download/trollstore-temp-20260904105902/Kline.ipa'
print(f'Connecting to webinspector...')
try:
    service = WebInspectorService()
    print('Connected! Opening URL...')
    
    # Try to launch the URL
    tab = service.open_tab(url)
    print(f'Tab opened: {tab}')
    
    time.sleep(3)
    print('Done!')
    
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
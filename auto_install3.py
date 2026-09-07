"""Auto-install IPA via TrollStore URL scheme using pymobiledevice3 WebDriver API."""
import asyncio

async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.webinspector import WebinspectorService
    from pymobiledevice3.services.web_protocol.driver import WebDriver

    trollstore_url = (
        'apple-magnifier://install?url=https://github.com/SunChuquin/Kline/releases/download/trollstore-temp-20260904105902/Kline.ipa'
    )

    print('Connecting to device...')
    lockdown = await create_using_usbmux()
    print(f'Device: iOS {lockdown.product_version}')

    print('Starting WebInspector service...')
    inspector = WebinspectorService(lockdown=lockdown)
    await inspector.connect()

    try:
        async with inspector:
            print('Opening Safari...')
            application = await inspector.open_app('com.apple.mobilesafari')
            print(f'Safari opened: {application}')

            print('Creating automation session...')
            automation_session = await inspector.automation_session(application)

            driver = WebDriver(automation_session)
            print('Starting WebDriver session...')
            await driver.start_session()
            print('Session started!')

            # Navigate to a regular page first
            print('Navigating to example.com...')
            await driver.get('https://example.com')
            await asyncio.sleep(2)

            # Execute JavaScript to redirect to TrollStore URL scheme
            js = f"window.location.href = '{trollstore_url}'"
            print(f'Executing: {js}')
            await driver.execute_script(js)

            await asyncio.sleep(3)
            print('Done! Check iPad - TrollStore should be downloading the IPA.')

            await automation_session.stop_session()
    finally:
        await inspector.close()
        lockdown.close()

if __name__ == '__main__':
    asyncio.run(main())

"""Push IPA to Kline's Documents directory via house_arrest service."""
import asyncio
import os

IPA_PATH = r'c:\Users\sunck\home\projects\ios\artifacts\Kline-unsigned-ipa\Kline.ipa'
BUNDLE_ID = 'com.sunck.Kline'

async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    print(f'Connecting to device...')
    lockdown = await create_using_usbmux()
    print(f'Device: iOS {lockdown.product_version}')

    print(f'Creating house_arrest service for {BUNDLE_ID}...')
    afc = await HouseArrestService.create(lockdown=lockdown, bundle_id=BUNDLE_ID, documents_only=False)
    print('HouseArrest service created!')

    # List current Documents
    try:
        docs = await afc.listdir('/Documents')
        print(f'Current /Documents: {docs}')
    except Exception:
        print('/Documents is empty or does not exist')
        await afc.makedirs('/Documents')

    # Push the IPA file
    print(f'Pushing {os.path.basename(IPA_PATH)} ({os.path.getsize(IPA_PATH)} bytes) to /Documents/...')
    await afc.set_file_contents('/Documents/Kline.ipa', open(IPA_PATH, 'rb').read())
    print('IPA pushed successfully!')

    # Verify
    docs = await afc.listdir('/Documents')
    print(f'Documents contents: {docs}')
    print(f'\nDone! Now open Files app on iPad:')
    print(f'  On My iPad -> Kline -> Kline.ipa')
    print(f'  Long press -> Share -> Open in TrollStore')

    await afc.aclose()

if __name__ == '__main__':
    asyncio.run(main())

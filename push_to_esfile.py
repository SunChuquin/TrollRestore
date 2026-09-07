"""Push ad-hoc signed IPA to ES文件浏览器's Documents directory via house_arrest service."""
import asyncio
import os

IPA_PATH = r'c:\Users\sunck\home\projects\ios\artifacts\Kline.ipa'
BUNDLE_ID = 'com.doglobal.ESFileExplorer'

async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    if not os.path.exists(IPA_PATH):
        raise FileNotFoundError(f'IPA not found: {IPA_PATH}')

    print(f'IPA: {IPA_PATH} ({os.path.getsize(IPA_PATH):,} bytes)')
    print(f'Connecting to device...')
    lockdown = await create_using_usbmux()
    print(f'Device: iOS {lockdown.product_version}')

    print(f'Creating house_arrest service for {BUNDLE_ID}...')
    afc = await HouseArrestService.create(lockdown=lockdown, bundle_id=BUNDLE_ID, documents_only=False)
    print('HouseArrest service created!')

    # List current Documents
    try:
        docs = await afc.listdir('/Documents')
        print(f'Current /Documents ({len(docs)} items): {docs[:10]}{"..." if len(docs) > 10 else ""}')
    except Exception:
        print('/Documents does not exist, creating...')
        await afc.makedirs('/Documents')

    # Push the IPA file
    print(f'Pushing Kline.ipa to /Documents/Kline.ipa ...')
    with open(IPA_PATH, 'rb') as f:
        data = f.read()
    await afc.set_file_contents('/Documents/Kline.ipa', data)
    print(f'Pushed {len(data):,} bytes successfully!')

    # Verify
    docs = await afc.listdir('/Documents')
    if 'Kline.ipa' in docs:
        print('Verification: Kline.ipa is in /Documents ✅')
    else:
        print(f'VERIFY FAILED: Kline.ipa not found. Contents: {docs}')

    print(f'\nDone! On your iPad:')
    print(f'  1. Open "ES文件浏览器" app')
    print(f'  2. Navigate to Local (本地) -> Documents')
    print(f'  3. Tap Kline.ipa -> Share (分享) -> Open in TrollStore')
    print(f'     (Or long-press Kline.ipa -> Open with TrollStore)')

    await afc.aclose()

if __name__ == '__main__':
    asyncio.run(main())

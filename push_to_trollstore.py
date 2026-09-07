"""Push IPA to TrollStore's Documents directory via house_arrest service."""
import asyncio
import os

IPA_PATH = r'c:\Users\sunck\home\projects\ios\artifacts\Kline-unsigned-ipa\Kline.ipa'
BUNDLE_ID = 'com.opa334.TrollStore'

async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    print(f'Connecting to device...')
    lockdown = await create_using_usbmux()
    print(f'Device: iOS {lockdown.product_version}')

    print(f'Creating house_arrest service for {BUNDLE_ID}...')
    try:
        afc = await HouseArrestService.create(lockdown=lockdown, bundle_id=BUNDLE_ID, documents_only=False)
        print('HouseArrest service created!')
    except Exception as e:
        print(f'Failed: {e}')
        import traceback
        traceback.print_exc()
        return

    # Create Documents directory if needed
    try:
        afc.makedirs('/Documents')
        print('Created /Documents')
    except Exception:
        print('/Documents already exists')

    # Push the IPA file
    print(f'Pushing {os.path.basename(IPA_PATH)} to /Documents/...')
    try:
        afc.set_file_contents('/Documents/Kline.ipa', open(IPA_PATH, 'rb').read())
        print('IPA pushed successfully!')
    except Exception as e:
        print(f'set_file_contents failed: {e}, trying push instead...')
        try:
            afc.push(IPA_PATH, '/Documents/Kline.ipa')
            print('IPA pushed via push()!')
        except Exception as e2:
            print(f'push also failed: {e2}')
            import traceback
            traceback.print_exc()
            return

    # Verify
    files = afc.listdir('/Documents')
    print(f'Documents contents: {files}')
    print(f'\nDone! Open Files app on iPad -> On My iPad -> TrollStore -> Kline.ipa')
    print(f'Then long-press -> Share -> Open with TrollStore')

    afc.close()

if __name__ == '__main__':
    asyncio.run(main())

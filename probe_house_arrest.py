"""Probe which apps allow HouseArrestService among UIFileSharingEnabled ones."""
import asyncio

async def probe(bid, name):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService
    try:
        lockdown = await create_using_usbmux()
        afc = await HouseArrestService.create(lockdown=lockdown, bundle_id=bid, documents_only=False)
        docs = await afc.listdir('/Documents')
        try:
            await afc.aclose()
        except Exception:
            pass
        return (name, bid, 'OK', f'/Documents has {len(docs)} items')
    except Exception as e:
        return (name, bid, 'FAIL', repr(e)[:200])

async def main():
    targets = [
        ('com.ownbook.notes',         '爱思全能版'),
        ('net.huanci.huashijie',      '画世界'),
        ('com.doglobal.ESFileExplorer', 'ES文件浏览器'),
        ('com.chucklefish.stardewvalley', 'Stardew Valley'),
    ]
    results = await asyncio.gather(*[probe(bid, name) for bid, name in targets])
    for name, bid, status, msg in results:
        print(f'{name:15s} {bid:40s} -> {status}: {msg}')

if __name__ == '__main__':
    asyncio.run(main())

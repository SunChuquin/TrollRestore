"""Open a URL on iOS device using raw lockdown service communication."""
import plistlib
from pymobiledevice3.lockdown import create_using_usbmux


def open_url_on_device(url):
    """Send OpenURL command via com.apple.mobile.iTunes service."""
    lockdown = create_using_usbmux()
    print(f"Connected to device: {lockdown.product_version}")

    # Get the service connection for com.apple.mobile.iTunes
    try:
        # Use start_lockdown_service which returns a service connection
        service = lockdown.start_lockdown_service("com.apple.mobile.iTunes")
        print(f"Service started: {service}")
    except Exception as e:
        print(f"start_lockdown_service failed: {e}")
        # Try alternative service names
        for svc_name in ["com.apple.mobile.iTunes", "com.apple.springboard", "com.apple.mobile.application"]:
            try:
                service = lockdown.start_lockdown_service(svc_name)
                print(f"Service '{svc_name}' started!")
                break
            except Exception as e2:
                print(f"  {svc_name}: {e2}")
        else:
            print("No service could be started for URL opening.")
            return False

    # Send the OpenURL plist command
    try:
        msg = plistlib.dumps({
            "Command": "OpenURL",
            "URL": url
        }, fmt=plistlib.FMT_BINARY)

        # Write to service
        if hasattr(service, 'send'):
            service.send(msg)
        elif hasattr(service, 'sendall'):
            service.sendall(msg)
        elif hasattr(service, 'write'):
            service.write(msg)

        print("OpenURL command sent!")

        # Read response
        if hasattr(service, 'recv'):
            response = service.recv(4096)
        elif hasattr(service, 'read'):
            response = service.read(4096)
        else:
            response = b""

        if response:
            try:
                result = plistlib.loads(response)
                print(f"Response: {result}")
            except:
                print(f"Raw response: {response}")

        return True
    except Exception as e:
        print(f"Failed to send OpenURL: {e}")
        return False
    finally:
        try:
            service.close()
        except:
            pass

if __name__ == "__main__":
    ipa_path = "/var/mobile/Media/Downloads/Kline.ipa"
    url = f"apple-magnifier://install?path={ipa_path}"
    print(f"Opening URL: {url}")
    open_url_on_device(url)

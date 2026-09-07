"""Open a URL on the device using the com.apple.mobile.iTunes lockdown service."""
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.diagnostics import DiagnosticsService
import plistlib

def open_url(service_provider, url):
    """Use the com.apple.mobile.iTunes service to open a URL on the device."""
    # Try the AFC service to see if we can access it
    try:
        conn = service_provider.create_service_connection("com.apple.mobile.iTunes")
    except Exception:
        try:
            conn = service_provider.service("com.apple.mobile.iTunes")
        except Exception as e:
            print(f"Cannot create service connection: {e}")
            # Try alternative: use the openURL lockdown request
            try:
                # Some iOS versions support openURL through lockdown
                from pymobiledevice3.lockdown import LockdownClient
                response = service_provider.query_service("com.apple.mobile.iTunes",
                                                          {"Command": "OpenURL", "URL": url})
                print(f"Query service response: {response}")
                return True
            except Exception as e2:
                print(f"Alternative method also failed: {e2}")
                return False

    # Send the open URL command
    try:
        msg = plistlib.dumps({"Command": "OpenURL", "URL": url}, fmt=plistlib.FMT_BINARY)
        conn.sendall(msg)
        response = conn.recvall()
        print(f"Response: {response}")
        return True
    except Exception as e:
        print(f"Failed to send OpenURL: {e}")
        return False
    finally:
        try:
            conn.close()
        except:
            pass

if __name__ == "__main__":
    ipa_path = "/var/mobile/Media/Kline.ipa"
    url = f"apple-magnifier://install?path={ipa_path}"

    print(f"Device: connecting...")
    service_provider = create_using_usbmux()
    print(f"Connected: {service_provider.product_version}")

    # First, list available services
    try:
        services = service_provider.get_service_names()
        relevant = [s for s in services if any(k in s.lower() for k in ['url', 'open', 'itunes', 'assist'])]
        print(f"Available relevant services: {relevant}")
    except Exception as e:
        print(f"Could not list services: {e}")

    print(f"\nAttempting to open URL: {url}")
    success = open_url(service_provider, url)

    if not success:
        print("\nFailed to open URL automatically.")
        print("The IPA is at /var/mobile/Media/Kline.ipa on the device.")
        print("Manual options:")
        print("1. Open Files app → find Kline.ipa → long press → Share → TrollStore")
        print("2. Open TrollStore → tap + → browse to the IPA")

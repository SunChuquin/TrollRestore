"""Trigger TrollStore to install an IPA from the device's Media directory."""
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobile_activation import MobileActivationService
import sys

# TrollStore URL scheme: apple-magnifier://install?path=<path>
# The IPA was pushed to /var/mobile/Media/Kline.ipa
ipa_path = "/var/mobile/Media/Kline.ipa"
url = f"apple-magnifier://install?path={ipa_path}"

service_provider = create_using_usbmux()
print(f"Device: {service_provider.product_version}")
print(f"Opening URL: {url}")

try:
    # Use the lockdown open_url service (com.apple.mobile.iTunes)
    service_provider.start_service("com.apple.mobile.iTunes")
    print("Service started, sending open URL request...")
    # Actually, let's try using the inseparables service for opening URLs
    from pymobiledevice3.services.information_assistant import InformationAssistantService
    ia = InformationAssistantService(service_provider=service_provider)
    ia.open_url(url)
    print("URL sent successfully! Check your iPad.")
except Exception as e:
    print(f"InformationAssistantService failed: {e}")
    print("Trying alternative method...")
    try:
        # Try using the raw service
        from pymobiledevice3.services.house_arrest import HouseArrestService
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        # Alternative: use SpringBoard's open URL via developer service
        from pymobiledevice3.services.dvt_secure_socket_proxy import DvtSecureSocketProxyService
        dvt = DvtSecureSocketProxyService(service_provider=service_provider)
        dvt.connect()
        # Launch TrollStore directly
        trollstore_path = None
        apps = InstallationProxyService(service_provider=service_provider).get_apps()
        for key, value in apps.items():
            if 'trollstore' in key.lower():
                trollstore_path = value.get('Path', '')
                print(f"Found TrollStore at: {trollstore_path}")
                break
        if trollstore_path:
            dvt.launch_application(trollstore_path)
            print("TrollStore launched! Open the IPA manually from the Files app.")
        dvt.close()
    except Exception as e2:
        print(f"Developer service also failed: {e2}")
        print("Manual method: Open TrollStore on iPad, tap '+', browse to the IPA file.")

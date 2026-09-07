"""List all available lockdown services on the device."""
from pymobiledevice3.lockdown import create_using_usbmux

lockdown = create_using_usbmux()
print(f"Device: {lockdown.product_version}")

# Get all available services
try:
    # Try the newer API
    services = lockdown.get_value(key="DeviceServices")
    if services:
        print("\n=== Device Services ===")
        for s in sorted(services):
            print(f"  {s}")
except Exception:
    pass

# Try listing from the lockdown record
try:
    record = lockdown.record
    if hasattr(record, 'services'):
        print("\n=== Services from record ===")
        for s in sorted(record.services):
            print(f"  {s}")
except Exception as e:
    print(f"Record services: {e}")

# Try the value query approach
try:
    import plistlib
    # Query all lockdown keys
    info = lockdown.all_values
    print(f"\n=== Lockdown values keys ===")
    for key in sorted(info.keys()):
        val = info[key]
        if isinstance(val, str) and len(val) < 100:
            print(f"  {key}: {val}")
        elif isinstance(val, (list, dict)):
            print(f"  {key}: ({type(val).__name__})")
        else:
            print(f"  {key}: {val}")
except Exception as e:
    print(f"Error listing values: {e}")

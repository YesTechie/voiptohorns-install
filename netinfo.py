"""Small network helpers shared by the bridge and the dashboard."""
import socket


def detect_my_ip(target=None):
    """LAN IP this host uses to reach ``target`` (the UniFi console).

    UDP connect() sends no packets; it only asks the kernel which source
    address it would use. Returns "" if nothing usable is found.
    """
    probes = [t for t in (target, "1.1.1.1") if t]
    for host in probes:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((host, 9))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
        except OSError:
            pass
        finally:
            if s is not None:
                s.close()
    return ""

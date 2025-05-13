import socket
import netifaces
import logging
import platform
import struct
import fcntl
from typing import List, Tuple, Optional, Dict, Callable
import ipaddress
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class NetworkInterface:
    name: str
    ip: str
    netmask: str
    broadcast: str
    mtu: int
    is_up: bool
    is_loopback: bool
    is_wireless: bool

def get_local_ips() -> List[Tuple[str, str, str]]:
    """
    Get all local IP addresses suitable for LAN communication.
    Returns a list of tuples: (interface_name, ip_address, broadcast_address)
    """
    local_ips = []
    
    try:
        interfaces = netifaces.interfaces()
        for iface in interfaces:
            # Skip loopback and virtual interfaces
            if iface.startswith(('lo', 'docker', 'veth', 'br-', 'vnet')):
                continue
                
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if 'addr' not in addr or 'netmask' not in addr:
                        continue
                        
                    ip = addr['addr']
                    # Skip localhost, docker, and VPN addresses
                    if ip.startswith(('127.', '172.', '10.')):
                        continue
                        
                    try:
                        network = ipaddress.IPv4Network(f"{ip}/{addr['netmask']}", strict=False)
                        broadcast = str(network.broadcast_address)
                        local_ips.append((iface, ip, broadcast))
                        logger.debug(f"Found interface {iface}: IP={ip}, broadcast={broadcast}")
                    except Exception as e:
                        logger.warning(f"Error calculating broadcast for {iface}: {e}")
                        
    except Exception as e:
        logger.error(f"Error getting network interfaces: {e}")
    
    return local_ips

def is_port_available(port: int, bind_ip: str = "0.0.0.0") -> bool:
    """
    Check if a port is available on the specified IP.
    Returns True if port is available, False otherwise.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # Linux
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind((bind_ip, port))
        return True
    except OSError:
        return False
    finally:
        if sock:
            sock.close()

def find_available_port(start_port: int, end_port: int, bind_ip: str = "0.0.0.0") -> Optional[int]:
    """
    Find an available port in the specified range.
    Returns the port number if found, None otherwise.
    """
    for port in range(start_port, end_port + 1):
        if is_port_available(port, bind_ip):
            return port
    return None

def get_interface_info(ifname: str) -> Optional[NetworkInterface]:
    """Get detailed information about a network interface on Linux."""
    try:
        # Get socket for making ioctl calls
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        # SIOCGIFFLAGS - get interface flags
        flags = struct.unpack('H', fcntl.ioctl(
            sock.fileno(),
            0x8913,  # SIOCGIFFLAGS
            struct.pack('256s', ifname[:15].encode())
        )[16:18])[0]
        
        # Check flags
        is_up = bool(flags & 1)  # IFF_UP
        is_loopback = bool(flags & 8)  # IFF_LOOPBACK
        
        # Get interface addresses
        addrs = netifaces.ifaddresses(ifname)
        if netifaces.AF_INET not in addrs:
            return None
            
        addr = addrs[netifaces.AF_INET][0]
        ip = addr.get('addr')
        netmask = addr.get('netmask')
        broadcast = addr.get('broadcast')
        
        if not (ip and netmask):
            return None
            
        # Get MTU - SIOCGIFMTU
        mtu = struct.unpack('I', fcntl.ioctl(
            sock.fileno(),
            0x8921,  # SIOCGIFMTU
            struct.pack('256s', ifname[:15].encode())
        )[16:20])[0]
        
        # Check if wireless (not perfect but works for most cases)
        is_wireless = False
        try:
            fcntl.ioctl(sock.fileno(), 0x8B01, struct.pack('256s', ifname[:15].encode()))
            is_wireless = True
        except IOError:
            pass
            
        return NetworkInterface(
            name=ifname,
            ip=ip,
            netmask=netmask,
            broadcast=broadcast or str(ipaddress.IPv4Network(f"{ip}/{netmask}", False).broadcast_address),
            mtu=mtu,
            is_up=is_up,
            is_loopback=is_loopback,
            is_wireless=is_wireless
        )
            
    except Exception as e:
        logger.error(f"Error getting interface info for {ifname}: {e}")
        return None
    finally:
        try:
            sock.close()
        except:
            pass

def configure_socket_linux_specific(sock: socket.socket) -> None:
    """Apply Linux-specific socket optimizations."""
    try:
        # Enable port reuse
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            
        # Set socket buffers
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)  # 256 KB receive buffer
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)  # 256 KB send buffer
        
        # Set close-on-exec flag
        sock.set_inheritable(False)
        
        # Enable non-blocking mode for better timeout handling
        sock.setblocking(False)
        
        # Set keepalive options if TCP
        if sock.type == socket.SOCK_STREAM:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
                
    except Exception as e:
        logger.warning(f"Could not apply all Linux socket optimizations: {e}")

def get_network_namespaces() -> List[str]:
    """Get list of network namespaces (Linux only)."""
    try:
        import os
        if not os.path.exists('/var/run/netns'):
            return []
        return [f for f in os.listdir('/var/run/netns') if not f.startswith('.')]
    except Exception as e:
        logger.error(f"Error getting network namespaces: {e}")
        return []

def get_default_route_info() -> Optional[Tuple[str, str]]:
    """Get the default network interface and gateway IP on Linux."""
    try:
        import subprocess
        output = subprocess.check_output(['ip', 'route', 'show', 'default']).decode()
        if output:
            parts = output.split()
            if len(parts) >= 5:
                return (parts[4], parts[2])  # (interface_name, gateway_ip)
        return None
    except Exception as e:
        logger.error(f"Error getting default route: {e}")
        return None

def monitor_interface_changes(callback: Callable[[str, bool], None]) -> None:
    """
    Monitor network interface changes using system files.
    callback(interface_name, is_up) will be called when interface state changes.
    """
    import os
    import select
    import time
    
    def check_interface_state(ifname: str) -> bool:
        try:
            with open(f"/sys/class/net/{ifname}/operstate", "r") as f:
                return f.read().strip().lower() == "up"
        except Exception:
            return False
    
    try:
        # Initial state
        interfaces = {}
        for ifname in os.listdir("/sys/class/net"):
            if not ifname.startswith(("lo", "docker", "veth", "br-", "vnet")):
                interfaces[ifname] = check_interface_state(ifname)
        
        while True:
            # Check for changes
            for ifname in list(interfaces.keys()):
                current_state = check_interface_state(ifname)
                if current_state != interfaces.get(ifname):
                    interfaces[ifname] = current_state
                    callback(ifname, current_state)
            
            # Check for new interfaces
            current_ifaces = set(os.listdir("/sys/class/net"))
            for ifname in current_ifaces - set(interfaces.keys()):
                if not ifname.startswith(("lo", "docker", "veth", "br-", "vnet")):
                    state = check_interface_state(ifname)
                    interfaces[ifname] = state
                    callback(ifname, state)
            
            time.sleep(1)
    except Exception as e:
        logger.error(f"Error monitoring interfaces: {e}")

def get_interface_speed(ifname: str) -> Optional[int]:
    """Get the speed of a network interface in Mbps."""
    try:
        with open(f"/sys/class/net/{ifname}/speed", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def detect_firewall_blocking() -> bool:
    """Check if the firewall might be blocking our ports."""
    try:
        # Check iptables for relevant rules
        import subprocess
        output = subprocess.check_output(["iptables", "-L", "INPUT", "-n"]).decode()
        return "DROP" in output or "REJECT" in output
    except Exception:
        return False  # Assume no blocking if we can't check

def setup_multicast_socket(multicast_addr: str, port: int) -> Optional[socket.socket]:
    """Setup a socket for multicast communication."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        
        # Allow multicast packets to loop back
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        
        # Set multicast TTL
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        
        # Bind to port
        sock.bind(("0.0.0.0", port))
        
        # Join multicast group on all interfaces
        for iface in get_local_ips():
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(multicast_addr),
                                socket.inet_aton(iface[1]))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except Exception as e:
                logger.debug(f"Could not add multicast membership for {iface[1]}: {e}")
        
        return sock
        
    except Exception as e:
        logger.error(f"Error setting up multicast socket: {e}")
        if 'sock' in locals():
            sock.close()
        return None

def diagnose_network_issues() -> List[str]:
    """
    Diagnose common network issues that might affect LAN discovery.
    Returns a list of potential issues found.
    """
    issues = []
    
    # Check firewall status
    if detect_firewall_blocking():
        issues.append("Firewall may be blocking discovery ports")
    
    # Check network interfaces
    working_interfaces = 0
    for iface, ip, _ in get_local_ips():
        speed = get_interface_speed(iface)
        if speed is not None and speed == 0:
            issues.append(f"Interface {iface} ({ip}) has no link")
        elif speed is not None:
            working_interfaces += 1
    
    if working_interfaces == 0:
        issues.append("No working network interfaces found")
    
    # Check route
    default_route = get_default_route_info()
    if not default_route:
        issues.append("No default network route found")
    
    # Check for common Linux networking issues
    try:
        with open("/proc/sys/net/ipv4/icmp_echo_ignore_broadcasts", "r") as f:
            if f.read().strip() == "1":
                issues.append("ICMP broadcast responses are disabled")
    except:
        pass
        
    try:
        with open("/proc/sys/net/ipv4/conf/all/rp_filter", "r") as f:
            if f.read().strip() == "1":
                issues.append("Strict reverse path filtering may affect discovery")
    except:
        pass
    
    return issues

def test_broadcast_connectivity(port: int, timeout: float = 2.0) -> Dict[str, bool]:
    """
    Test broadcast connectivity on all interfaces.
    Returns a dict mapping interface names to connectivity status.
    """
    results = {}
    test_data = b"LAN_MESSENGER_CONNECTIVITY_TEST"
    
    # Create receiver socket
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    recv_sock.settimeout(timeout)
    
    try:
        recv_sock.bind(("0.0.0.0", port))
        
        # Test each interface
        for iface, ip, broadcast in get_local_ips():
            # Create sender socket
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            try:
                send_sock.bind((ip, 0))
                send_sock.sendto(test_data, (broadcast, port))
                
                try:
                    data, addr = recv_sock.recvfrom(1024)
                    results[iface] = (data == test_data)
                except socket.timeout:
                    results[iface] = False
                    
            except Exception as e:
                logger.debug(f"Broadcast test failed on {iface}: {e}")
                results[iface] = False
            finally:
                send_sock.close()
                
    except Exception as e:
        logger.error(f"Broadcast connectivity test failed: {e}")
    finally:
        recv_sock.close()
        
    return results

def create_robust_udp_socket(bind_addr: str, port: int, broadcast: bool = False) -> Optional[socket.socket]:
    """
    Create a UDP socket with robust error handling and Linux optimizations.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        
        # Set basic options
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Apply Linux optimizations
        configure_socket_linux_specific(sock)
        
        # Try binding
        retries = 3
        while retries > 0:
            try:
                sock.bind((bind_addr, port))
                return sock
            except OSError as e:
                if e.errno == 98:  # Address already in use
                    time.sleep(0.1)
                    retries -= 1
                else:
                    raise
        
        if retries == 0:
            logger.error(f"Could not bind to {bind_addr}:{port} after retries")
            sock.close()
            return None
            
    except Exception as e:
        logger.error(f"Error creating UDP socket: {e}")
        if sock:
            sock.close()
        return None

import socket
import threading
import time
import json
import logging
from typing import Dict, Set, Optional, Tuple, Callable
from dataclasses import dataclass
from .network_utils import (
    get_local_ips,
    configure_socket_linux_specific,
    is_port_available,
    find_available_port
)

logger = logging.getLogger(__name__)

@dataclass
class PeerInfo:
    username: str
    last_seen: float  # timestamp of last heartbeat
    addresses: Set[str]  # potentially multiple IPs for multi-homed peers
    extra_data: Dict = None  # for future extensibility

class DiscoveryService:
    def __init__(self, username: str, discovery_port: int = 50000, broadcast_interval: float = 5.0,
                 peer_timeout: float = 15.0, retry_interval: float = 1.0, max_retries: int = 3):
        """
        Initialize the discovery service.
        
        Args:
            username: The username to broadcast
            discovery_port: Port for discovery broadcasts (will fallback if unavailable)
            broadcast_interval: How often to broadcast presence (seconds)
            peer_timeout: How long before a peer is considered lost (seconds)
            retry_interval: How long to wait between retries (seconds)
            max_retries: Maximum number of retries for operations
        """
        self.username = username
        self.broadcast_interval = broadcast_interval
        self.peer_timeout = peer_timeout
        self.retry_interval = retry_interval
        self.max_retries = max_retries
        
        # Initialize state
        self.running = False
        self.known_peers: Dict[str, PeerInfo] = {}  # ip -> PeerInfo
        self.local_interfaces = []  # List of (iface, ip, broadcast) tuples
        self.broadcast_socket = None
        self.listener_socket = None
        
        # Threads
        self.listener_thread = None
        self.broadcaster_thread = None
        self.cleanup_thread = None
        
        # Callbacks
        self.on_user_discovered: Optional[Callable] = None
        self.on_user_lost: Optional[Callable] = None
        
        # Find available port
        self.discovery_port = find_available_port(discovery_port, discovery_port + 10)
        if not self.discovery_port:
            raise RuntimeError(f"Could not find available port in range {discovery_port}-{discovery_port + 10}")
            
        # Get network interfaces
        self._refresh_network_interfaces()
        logger.info(f"Discovery service initialized with port {self.discovery_port}")

    def _refresh_network_interfaces(self) -> None:
        """Refresh the list of network interfaces and their broadcast addresses."""
        try:
            old_interfaces = set((iface, ip, broadcast) for iface, ip, broadcast in self.local_interfaces)
            new_interfaces = set(get_local_ips())
            
            if old_interfaces != new_interfaces:
                added = new_interfaces - old_interfaces
                removed = old_interfaces - new_interfaces
                
                if added:
                    logger.info(f"New network interfaces detected: {[iface for iface,_,_ in added]}")
                if removed:
                    logger.info(f"Network interfaces removed: {[iface for iface,_,_ in removed]}")
            
            self.local_interfaces = list(new_interfaces)
            logger.info(f"Current network interfaces: {[iface for iface,_,_ in self.local_interfaces]}")
        except Exception as e:
            logger.error(f"Error refreshing network interfaces: {e}")
            # Keep existing interfaces if refresh fails
            if not self.local_interfaces:
                self.local_interfaces = get_local_ips()

    def _setup_broadcast_socket(self) -> Optional[socket.socket]:
        """Set up the broadcast socket with proper configuration."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Apply Linux optimizations
            configure_socket_linux_specific(sock)
            
            # Try binding to each interface specifically
            bound = False
            for _, ip, _ in self.local_interfaces:
                try:
                    sock.bind((ip, 0))
                    bound = True
                    logger.info(f"Bound broadcast socket to {ip}")
                    break
                except Exception as e:
                    logger.debug(f"Could not bind to {ip}: {e}")
            
            # Fallback to binding to all interfaces
            if not bound:
                try:
                    sock.bind(("0.0.0.0", 0))
                    logger.info("Bound broadcast socket to all interfaces")
                    bound = True
                except Exception as e:
                    logger.error(f"Could not bind to all interfaces: {e}")
            
            if bound:
                return sock
            else:
                sock.close()
                return None
            
        except Exception as e:
            logger.error(f"Error setting up broadcast socket: {e}")
            if 'sock' in locals():
                sock.close()
            return None

    def _setup_listener_socket(self) -> Optional[socket.socket]:
        """Set up the discovery listener socket with proper configuration."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Apply Linux optimizations
            configure_socket_linux_specific(sock)
            
            # First try binding to specific interfaces
            bound = False
            for _, ip, _ in self.local_interfaces:
                try:
                    sock.bind((ip, self.discovery_port))
                    logger.info(f"Bound listener socket to {ip}:{self.discovery_port}")
                    bound = True
                    break
                except Exception as e:
                    logger.debug(f"Could not bind listener to {ip}: {e}")
            
            # If specific binding fails, try binding to all interfaces
            if not bound:
                retries = 0
                while retries < self.max_retries:
                    try:
                        sock.bind(("0.0.0.0", self.discovery_port))
                        logger.info(f"Bound listener socket to all interfaces on port {self.discovery_port}")
                        return sock
                    except OSError as e:
                        retries += 1
                        logger.warning(f"Bind attempt {retries} failed: {e}")
                        if retries < self.max_retries:
                            time.sleep(self.retry_interval)
                        else:
                            logger.error("All bind attempts failed")
                            sock.close()
                            return None
            
            return sock if bound else None
            
        except Exception as e:
            logger.error(f"Error setting up listener socket: {e}")
            if 'sock' in locals():
                sock.close()
            return None

    def _recover_broadcast_socket(self) -> bool:
        """Attempt to recover the broadcast socket if it fails."""
        if self.broadcast_socket:
            try:
                self.broadcast_socket.close()
            except:
                pass
            self.broadcast_socket = None
            
        retries = 0
        while retries < self.max_retries and self.running:
            logger.info(f"Attempting to recover broadcast socket (attempt {retries + 1}/{self.max_retries})")
            self.broadcast_socket = self._setup_broadcast_socket()
            if self.broadcast_socket:
                logger.info("Successfully recovered broadcast socket")
                return True
            retries += 1
            time.sleep(self.retry_interval)
            
        logger.error("Failed to recover broadcast socket")
        return False

    def _recover_listener_socket(self) -> bool:
        """Attempt to recover the listener socket if it fails."""
        if self.listener_socket:
            try:
                self.listener_socket.close()
            except:
                pass
            self.listener_socket = None
            
        retries = 0
        while retries < self.max_retries and self.running:
            logger.info(f"Attempting to recover listener socket (attempt {retries + 1}/{self.max_retries})")
            self.listener_socket = self._setup_listener_socket()
            if self.listener_socket:
                logger.info("Successfully recovered listener socket")
                return True
            retries += 1
            time.sleep(self.retry_interval)
            
        logger.error("Failed to recover listener socket")
        return False

    def _check_socket_health(self, sock: socket.socket) -> bool:
        """Check if a socket is still healthy and usable."""
        if not sock:
            return False
        try:
            # Try to get socket options as a basic health check
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
            return True
        except socket.error:
            return False

    def _refresh_broadcast_addresses(self) -> None:
        """
        Refresh network interfaces and their broadcast addresses.
        This is called periodically to handle network changes.
        """
        try:
            old_interfaces = set((iface, ip, broadcast) for iface, ip, broadcast in self.local_interfaces)
            new_interfaces = set(get_local_ips())
            
            if old_interfaces != new_interfaces:
                added = new_interfaces - old_interfaces
                removed = old_interfaces - new_interfaces
                
                if added:
                    logger.info(f"New network interfaces detected: {[iface for iface,_,_ in added]}")
                if removed:
                    logger.info(f"Network interfaces removed: {[iface for iface,_,_ in removed]}")
                    
                self.local_interfaces = list(new_interfaces)
        except Exception as e:
            logger.error(f"Error refreshing network interfaces: {e}")

    def _broadcast_presence(self):
        """Broadcast presence on all network interfaces."""
        if not self._recover_broadcast_socket():
            logger.error("Failed to create or recover broadcast socket")
            return

        message_base = {
            "type": "presence",
            "username": self.username,
            "uptime": time.time(),
            "port": self.discovery_port,
            "version": "1.0"  # For future compatibility
        }

        while self.running:
            try:
                # Refresh interfaces periodically
                if time.time() % 60 < self.broadcast_interval:
                    self._refresh_network_interfaces()

                # Check socket health
                if not self._check_socket_health(self.broadcast_socket):
                    if not self._recover_broadcast_socket():
                        time.sleep(self.retry_interval)
                        continue

                # Prepare broadcast message
                message = {**message_base, "timestamp": time.time()}
                data = json.dumps(message).encode("utf-8")

                # Broadcast on each interface
                for iface, ip, broadcast in self.local_interfaces:
                    retries = 0
                    while retries < self.max_retries and self.running:
                        try:
                            # Try interface-specific broadcast
                            self.broadcast_socket.sendto(data, (broadcast, self.discovery_port))
                            logger.debug(f"Broadcast sent on {iface} ({ip}) to {broadcast}")
                            break
                        except Exception as e:
                            retries += 1
                            logger.warning(f"Broadcast attempt {retries} failed on {iface}: {e}")
                            
                            if retries < self.max_retries:
                                # Try global broadcast as fallback
                                try:
                                    self.broadcast_socket.sendto(data, ("255.255.255.255", self.discovery_port))
                                    logger.debug(f"Fallback broadcast sent on {iface}")
                                    break
                                except Exception as e2:
                                    logger.error(f"Fallback broadcast failed on {iface}: {e2}")
                                    
                            if not self._recover_broadcast_socket():
                                break
                            time.sleep(self.retry_interval)

                # Sleep between broadcast cycles
                time.sleep(self.broadcast_interval)

            except Exception as e:
                logger.error(f"Error in broadcast thread: {e}")
                time.sleep(self.retry_interval)

        # Clean up
        if self.broadcast_socket:
            try:
                self.broadcast_socket.close()
            except:
                pass
            self.broadcast_socket = None

    def _listen_for_presence(self):
        """Listen for presence broadcasts and maintain peer list."""
        if not self.listener_socket and not self._recover_listener_socket():
            logger.error("Failed to create or recover listener socket")
            return

        consecutive_errors = 0
        max_consecutive_errors = self.max_retries * 2  # Allow more retries for listener

        while self.running:
            try:
                data, addr = self.listener_socket.recvfrom(8192)
                peer_ip = addr[0]
                
                try:
                    message = json.loads(data.decode("utf-8"))
                    msg_type = message.get("type")
                    username = message.get("username")
                    timestamp = message.get("timestamp", time.time())

                    if msg_type == "presence" and username:
                        current_time = time.time()
                        
                        # Update or add peer info
                        if peer_ip in self.known_peers:
                            peer = self.known_peers[peer_ip]
                            old_username = peer.username
                            peer.last_seen = current_time
                            peer.username = username
                            peer.addresses.add(peer_ip)
                            
                            if old_username != username:
                                logger.info(f"Peer {peer_ip} changed username from {old_username} to {username}")
                                if self.on_user_discovered:
                                    self.on_user_discovered(username, peer_ip)
                                    
                        else:
                            # New peer discovered
                            self.known_peers[peer_ip] = PeerInfo(
                                username=username,
                                last_seen=current_time,
                                addresses={peer_ip}
                            )
                            logger.info(f"Discovered new peer: {username} at {peer_ip}")
                            if self.on_user_discovered:
                                self.on_user_discovered(username, peer_ip)
                                
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from {peer_ip}: {e}")
                except UnicodeDecodeError as e:
                    logger.warning(f"Invalid UTF-8 from {peer_ip}: {e}")
                except Exception as e:
                    logger.error(f"Error processing message from {peer_ip}: {e}")

            except socket.timeout:
                consecutive_errors = 0  # Reset error counter on timeout
                continue
            except (socket.error, OSError) as e:
                logger.error(f"Socket error in listener thread: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many consecutive errors, attempting socket recovery")
                    if not self._recover_listener_socket():
                        logger.error("Failed to recover listener socket, stopping listener thread")
                        return
                    consecutive_errors = 0
                time.sleep(self.retry_interval)
            except Exception as e:
                logger.error(f"Unexpected error in listener thread: {e}")
                time.sleep(self.retry_interval)

        # Clean up
        if self.listener_socket:
            self.listener_socket.close()
            self.listener_socket = None

    def _cleanup_peers(self):
        """Clean up stale peers that haven't been seen recently."""
        while self.running:
            try:
                current_time = time.time()
                stale_peers = []
                
                # Find stale peers
                for ip, peer in list(self.known_peers.items()):
                    if current_time - peer.last_seen > self.peer_timeout:
                        stale_peers.append((ip, peer))
                
                # Remove stale peers
                for ip, peer in stale_peers:
                    logger.info(f"Peer {peer.username} ({ip}) timed out after {self.peer_timeout}s")
                    del self.known_peers[ip]
                    if self.on_user_lost:
                        self.on_user_lost(peer.username, ip)
                
                time.sleep(self.broadcast_interval)
                
            except Exception as e:
                logger.error(f"Error in cleanup thread: {e}")
                time.sleep(self.retry_interval)

    def _diagnose_network_issues(self) -> None:
        """Check for common network issues that could affect discovery."""
        from .network_utils import diagnose_network_issues, test_broadcast_connectivity
        
        issues = diagnose_network_issues()
        if issues:
            logger.warning("Network diagnostic results:")
            for issue in issues:
                logger.warning(f"- {issue}")
                
        # Test broadcast connectivity
        connectivity = test_broadcast_connectivity(self.discovery_port + 1)  # Use port+1 for test
        if connectivity:
            logger.info("Broadcast connectivity test results:")
            for iface, status in connectivity.items():
                logger.info(f"- {iface}: {'OK' if status else 'Failed'}")
                
        # If no working interfaces, try to recover
        if not any(connectivity.values()):
            logger.error("No working broadcast interfaces found, attempting recovery")
            self._recover_network()

    def _recover_network(self) -> bool:
        """Attempt to recover network functionality."""
        try:
            # Close existing sockets
            if self.broadcast_socket:
                try:
                    self.broadcast_socket.close()
                except:
                    pass
                self.broadcast_socket = None
                
            if self.listener_socket:
                try:
                    self.listener_socket.close()
                except:
                    pass
                self.listener_socket = None
            
            # Refresh network interfaces
            self._refresh_network_interfaces()
            
            # Try to recreate sockets with new settings
            if self._recover_broadcast_socket() and self._recover_listener_socket():
                logger.info("Network recovery successful")
                return True
            else:
                logger.error("Network recovery failed")
                return False
                
        except Exception as e:
            logger.error(f"Error during network recovery: {e}")
            return False

    def _monitor_network(self) -> None:
        """Monitor network interface changes."""
        from .network_utils import monitor_interface_changes
        
        def handle_interface_change(ifname: str, is_up: bool):
            logger.info(f"Network interface {ifname} is {'up' if is_up else 'down'}")
            if is_up:
                # Interface came up, try to use it
                self._refresh_network_interfaces()
                if not self._check_socket_health(self.broadcast_socket):
                    self._recover_broadcast_socket()
                if not self._check_socket_health(self.listener_socket):
                    self._recover_listener_socket()
            else:
                # Interface went down, refresh interfaces
                self._refresh_network_interfaces()
        
        try:
            monitor_interface_changes(handle_interface_change)
        except Exception as e:
            logger.error(f"Network monitoring error: {e}")

    def start(self):
        """Start the discovery service."""
        if self.running:
            logger.warning("Discovery service already running")
            return

        self.running = True
        
        # Check for network issues before starting
        self._diagnose_network_issues()
        
        # Start threads
        self.listener_thread = threading.Thread(target=self._listen_for_presence, daemon=True)
        self.broadcaster_thread = threading.Thread(target=self._broadcast_presence, daemon=True)
        self.cleanup_thread = threading.Thread(target=self._cleanup_peers, daemon=True)
        self.monitor_thread = threading.Thread(target=self._monitor_network, daemon=True)
        
        try:
            self.listener_thread.start()
            self.broadcaster_thread.start()
            self.cleanup_thread.start()
            self.monitor_thread.start()
            logger.info(f"Discovery service started for user '{self.username}' on port {self.discovery_port}")
        except Exception as e:
            logger.error(f"Failed to start discovery service: {e}")
            self.running = False
            raise

    def stop(self):
        """Stop the discovery service."""
        if not self.running:
            return
            
        logger.info("Stopping discovery service...")
        self.running = False
        
        # Close sockets to interrupt blocking operations
        if self.broadcast_socket:
            try:
                self.broadcast_socket.close()
            except:
                pass
            self.broadcast_socket = None
            
        if self.listener_socket:
            try:
                self.listener_socket.close()
            except:
                pass
            self.listener_socket = None
        
        # Wait for threads to finish
        threads = [
            (self.listener_thread, "Listener"),
            (self.broadcaster_thread, "Broadcaster"),
            (self.cleanup_thread, "Cleanup"),
            (self.monitor_thread, "Monitor")
        ]
        
        for thread, name in threads:
            if thread and thread.is_alive():
                logger.debug(f"Waiting for {name} thread to stop...")
                thread.join(timeout=2.0)
                if thread.is_alive():
                    logger.warning(f"{name} thread did not stop cleanly")
            
        self.known_peers.clear()
        logger.info("Discovery service stopped")

    def get_known_peers(self) -> Dict[str, PeerInfo]:
        """Get a copy of the known peers dictionary."""
        return dict(self.known_peers)

# Example Usage (for testing purposes, will be integrated into the main app)
if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO)
    current_username = os.getlogin() if hasattr(os, 'getlogin') else "TestUser"

    def user_discovered(username, ip_address):
        logger.info(f"[CALLBACK] Discovered: {username} at {ip_address}")

    def user_lost(username, ip_address):
        logger.info(f"[CALLBACK] Lost: {username} at {ip_address}")

    service = DiscoveryService(username=current_username)
    service.on_user_discovered = user_discovered
    service.on_user_lost = user_lost
    service.start()

    try:
        while True:
            time.sleep(10)
            logger.info(f"Known users: {service.known_peers}")
    except KeyboardInterrupt:
        logger.info("Stopping discovery service...")
    finally:
        service.stop()


import socket
import threading
import time
import json
import logging

# Initialize logger
logger = logging.getLogger(__name__)

class DiscoveryService:
    def __init__(self, username, discovery_port=50000, broadcast_interval=5):
        self.username = username
        self.discovery_port = discovery_port
        self.broadcast_interval = broadcast_interval
        self.running = False
        self.known_users = {} # Store discovered users: {("ip", port): "username"}
        self.listener_thread = None
        self.broadcaster_thread = None
        self.on_user_discovered = None # Callback for GUI updates
        self.on_user_lost = None # Callback for GUI updates
        self.logger = logging.getLogger("DiscoveryService")
        self.logger.setLevel(logging.DEBUG)

    def _broadcast_presence(self):
        """Broadcast the presence of this user."""
        try:
            broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow reuse of the address
            broadcast_socket.bind(("", 0))  # Bind to all interfaces
            while self.running:
                message = json.dumps({"username": self.username}).encode("utf-8")
                broadcast_socket.sendto(message, ("<broadcast>", self.discovery_port))
                self.logger.debug(f"Broadcasting presence: {message}")
                time.sleep(self.broadcast_interval)
        except socket.error as e:
            self.logger.error(f"Socket error during broadcast: {e}")
        finally:
            broadcast_socket.close()

    def _listen_for_presence(self):
        """Listen for presence broadcasts from other users."""
        try:
            listener_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener_socket.bind(("", self.discovery_port))  # Bind to all interfaces
            self.logger.debug(f"Listening for presence on port {self.discovery_port}")
            while self.running:
                try:
                    data, addr = listener_socket.recvfrom(1024)
                    message = json.loads(data.decode("utf-8"))
                    username = message.get("username")
                    if username and addr not in self.known_users:
        
        listener_socket.settimeout(1.0) # Timeout to allow checking self.running

        while self.running:
            try:
                data, addr = listener_socket.recvfrom(1024) # buffer size is 1024 bytes
                message = json.loads(data.decode("utf-8"))
                peer_username = message.get("username")
                action = message.get("action")

                try:
                    my_ip = socket.gethostbyname(socket.gethostname())
                except socket.gaierror:
                    my_ip = "127.0.0.1" # Fallback

                if addr[0] == my_ip and peer_username == self.username:
                    continue # Skip self-discovery

                if action == "discover_ping" and peer_username:
                    if (addr[0], addr[1]) not in self.known_users or self.known_users[(addr[0], addr[1])] != peer_username:
                        self.logger.info(f"Discovered user: {peer_username} at {addr}")
                        self.known_users[(addr[0], addr[1])] = {"username": peer_username, "last_seen": time.time()}
                        if self.on_user_discovered:
                            self.on_user_discovered(peer_username, addr[0]) # Notify GUI
                elif action == "discover_pong" and peer_username: # If we implement active probing
                    pass
                elif action == "goodbye" and peer_username: # Graceful disconnect
                    if (addr[0], addr[1]) in self.known_users:
                        self.logger.info(f"User {peer_username} at {addr} went offline (goodbye message).")
                        del self.known_users[(addr[0], addr[1])]
                        if self.on_user_lost:
                            self.on_user_lost(peer_username, addr[0])

                current_time = time.time()
                stale_users = []
                for user_addr, user_info in list(self.known_users.items()):
                    if current_time - user_info["last_seen"] > self.broadcast_interval * 3:
                        stale_users.append(user_addr)
                
                for user_addr in stale_users:
                    lost_user_info = self.known_users.pop(user_addr)
                    self.logger.info(f"User {lost_user_info['username']} at {user_addr} timed out.")
                    if self.on_user_lost:
                        self.on_user_lost(lost_user_info['username'], user_addr[0])

            except socket.timeout:
                current_time = time.time()
                stale_users = []
                for user_addr, user_info in list(self.known_users.items()):
                    if current_time - user_info["last_seen"] > self.broadcast_interval * 3:
                        stale_users.append(user_addr)
                
                for user_addr in stale_users:
                    lost_user_info = self.known_users.pop(user_addr)
                    self.logger.info(f"User {lost_user_info['username']} at {user_addr} timed out.")
                    if self.on_user_lost:
                        self.on_user_lost(lost_user_info['username'], user_addr[0])
                continue
            except json.JSONDecodeError as e:
                self.logger.warning(f"Failed to decode message from {addr}: {e}")
            except Exception as e:
                self.logger.error(f"Error listening for peers: {e}")
        listener_socket.close()

    def start(self):
        if self.running:
            self.logger.warning("Discovery service already running.")
            return
        self.running = True
        try:
            self.listener_thread = threading.Thread(target=self._listen_for_peers, daemon=True)
            self.broadcaster_thread = threading.Thread(target=self._broadcast_presence, daemon=True)
            self.listener_thread.start()
            self.broadcaster_thread.start()
            self.logger.info(f"Discovery service started for user '{self.username}' on port {self.discovery_port}.")
        except Exception as e:
            self.logger.error(f"Error starting discovery service: {e}")
            self.running = False

    def stop(self):
        if not self.running:
            self.logger.warning("Discovery service not running.")
            return
        self.running = False
        goodbye_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        goodbye_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        message = json.dumps({"username": self.username, "action": "goodbye"}).encode("utf-8")
        try:
            goodbye_socket.sendto(message, ("<broadcast>", self.discovery_port))
        except Exception as e:
            self.logger.error(f"Error sending goodbye message: {e}")
        finally:
            goodbye_socket.close()

        if self.broadcaster_thread and self.broadcaster_thread.is_alive():
            self.broadcaster_thread.join(timeout=self.broadcast_interval + 1)
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)
        self.logger.info("Discovery service stopped.")

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
            logger.info(f"Known users: {service.known_users}")
    except KeyboardInterrupt:
        logger.info("Stopping discovery service...")
    finally:
        service.stop()


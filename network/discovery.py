import socket
import threading
import time
import json

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

    def _broadcast_presence(self):
        broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        broadcast_socket.settimeout(0.2) # Set a timeout so the socket does not block indefinitely
        message = json.dumps({"username": self.username, "action": "discover_ping"}).encode("utf-8")
        
        # Get local IP to include (optional, receiver can get it from packet)
        # For simplicity, receiver will use sender's IP from packet

        while self.running:
            try:
                broadcast_socket.sendto(message, ("<broadcast>", self.discovery_port))
                # print(f"Sent presence broadcast: {self.username}") # For debugging
            except Exception as e:
                print(f"Error broadcasting presence: {e}")
            time.sleep(self.broadcast_interval)
        broadcast_socket.close()

    def _listen_for_peers(self):
        listener_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener_socket.bind(("", self.discovery_port)) # Listen on all interfaces
        except OSError as e:
            print(f"Error binding listener socket: {e}. Port {self.discovery_port} might be in use.")
            self.running = False # Stop if binding fails
            return
        
        listener_socket.settimeout(1.0) # Timeout to allow checking self.running

        while self.running:
            try:
                data, addr = listener_socket.recvfrom(1024) # buffer size is 1024 bytes
                message = json.loads(data.decode("utf-8"))
                peer_username = message.get("username")
                action = message.get("action")

                # Avoid discovering self if broadcast is received by own listener
                # This check might need refinement based on how local IP is handled
                # For now, assume any message from a different (IP, Port) or different username is a peer
                # A more robust way is to assign a unique ID to each instance.
                
                # Get our own IP to compare (can be tricky with multiple interfaces)
                # For now, we'll assume if the username is different, it's a different user.
                # Or if the address is different.
                
                # A simple check: if it's not from our own IP (if we knew it reliably) or if username is different
                # For now, we'll just add them if they are not us by username (assuming unique usernames for simplicity)
                # A better approach would be to ignore packets from our own IP address.
                
                # Let's get our own IP to try and filter out self-discovery
                try:
                    my_ip = socket.gethostbyname(socket.gethostname())
                except socket.gaierror:
                    my_ip = "127.0.0.1" # Fallback

                if addr[0] == my_ip and peer_username == self.username:
                    continue # Skip self-discovery

                if action == "discover_ping" and peer_username:
                    if (addr[0], addr[1]) not in self.known_users or self.known_users[(addr[0], addr[1])] != peer_username:
                        print(f"Discovered user: {peer_username} at {addr}")
                        self.known_users[(addr[0], addr[1])] = {"username": peer_username, "last_seen": time.time()}
                        if self.on_user_discovered:
                            self.on_user_discovered(peer_username, addr[0]) # Notify GUI
                elif action == "discover_pong" and peer_username: # If we implement active probing
                    pass
                elif action == "goodbye" and peer_username: # Graceful disconnect
                    if (addr[0], addr[1]) in self.known_users:
                        print(f"User {peer_username} at {addr} went offline (goodbye message).")
                        del self.known_users[(addr[0], addr[1])]
                        if self.on_user_lost:
                            self.on_user_lost(peer_username, addr[0])

                # Periodically check for stale users
                current_time = time.time()
                stale_users = []
                for user_addr, user_info in list(self.known_users.items()):
                    # If user hasn't been seen for 3 * broadcast_interval, consider them offline
                    if current_time - user_info["last_seen"] > self.broadcast_interval * 3:
                        stale_users.append(user_addr)
                
                for user_addr in stale_users:
                    lost_user_info = self.known_users.pop(user_addr)
                    print(f"User {lost_user_info['username']} at {user_addr} timed out.")
                    if self.on_user_lost:
                        self.on_user_lost(lost_user_info['username'], user_addr[0])

            except socket.timeout:
                # Periodically check for stale users even on timeout
                current_time = time.time()
                stale_users = []
                for user_addr, user_info in list(self.known_users.items()):
                    if current_time - user_info["last_seen"] > self.broadcast_interval * 3:
                        stale_users.append(user_addr)
                
                for user_addr in stale_users:
                    lost_user_info = self.known_users.pop(user_addr)
                    print(f"User {lost_user_info['username']} at {user_addr} timed out.")
                    if self.on_user_lost:
                        self.on_user_lost(lost_user_info['username'], user_addr[0])
                continue # Expected timeout, continue loop
            except json.JSONDecodeError:
                # print(f"Received invalid JSON message from {addr}")
                pass # Ignore malformed packets
            except Exception as e:
                print(f"Error listening for peers: {e}")
        listener_socket.close()

    def start(self):
        if self.running:
            print("Discovery service already running.")
            return
        self.running = True
        self.listener_thread = threading.Thread(target=self._listen_for_peers, daemon=True)
        self.broadcaster_thread = threading.Thread(target=self._broadcast_presence, daemon=True)
        self.listener_thread.start()
        self.broadcaster_thread.start()
        print(f"Discovery service started for user '{self.username}' on port {self.discovery_port}.")

    def stop(self):
        if not self.running:
            print("Discovery service not running.")
            return
        self.running = False
        # Send a goodbye message
        goodbye_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        goodbye_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        message = json.dumps({"username": self.username, "action": "goodbye"}).encode("utf-8")
        try:
            goodbye_socket.sendto(message, ("<broadcast>", self.discovery_port))
        except Exception as e:
            print(f"Error sending goodbye message: {e}")
        finally:
            goodbye_socket.close()

        if self.broadcaster_thread and self.broadcaster_thread.is_alive():
            self.broadcaster_thread.join(timeout=self.broadcast_interval + 1)
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0) # Listener has 1s timeout
        print("Discovery service stopped.")

# Example Usage (for testing purposes, will be integrated into the main app)
if __name__ == "__main__":
    import os
    # A simple way to get a username, replace with a better method in the actual app
    current_username = os.getlogin() if hasattr(os, 'getlogin') else "TestUser"
    
    def user_discovered(username, ip_address):
        print(f"[CALLBACK] Discovered: {username} at {ip_address}")

    def user_lost(username, ip_address):
        print(f"[CALLBACK] Lost: {username} at {ip_address}")

    service = DiscoveryService(username=current_username)
    service.on_user_discovered = user_discovered
    service.on_user_lost = user_lost
    service.start()

    try:
        while True:
            time.sleep(10)
            print(f"Known users: {service.known_users}")
    except KeyboardInterrupt:
        print("Stopping discovery service...")
    finally:
        service.stop()

